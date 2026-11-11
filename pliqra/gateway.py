from .protocol import PROTOCOL_VERSION
import asyncio
import hashlib
import json
import time
import threading
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .connectors import ollama_result, ollama_calls, sse, upstream_request, bounded_lines, response_json, strict_json, sse_data
from .inference_schema import checked_completion, requests_tools, valid_completion, validate_tool_choice, INFERENCE_CONTRACT_REVISION
from .db import encode, now, uid
from .access import service_allowed, require_project, current_principal
from .policies import PolicyEngine, reservation_units
from .cluster import Cluster
from .secrets import SecretResolver
from .topology import Placement,placement_reason,runtime_health,observe_deployment,model_headers


CAPABILITIES = {"chat", "streaming", "tools", "structured_output", "vision", "embeddings", "audio_speech", "audio_transcription"}


@dataclass(frozen=True)
class AdmissionSnapshot:
    service_fingerprint: str
    generation: int
    required: tuple = ()
    deployments: tuple = ()


def decoded_port(row):
    if not row:
        return None
    return {**row, "deployments": json.loads(row["deployments"]), "requirements": json.loads(row["requirements"]), "enabled": bool(row["enabled"]),"placement":Placement.model_validate(json.loads(row.get("placement") or "{}")).model_dump()}


def decoded_deployment(row):
    return {**row, "capabilities": json.loads(row["capabilities"]), "verified": json.loads(row["verified"]), "enabled": bool(row["enabled"])}


def cost_micros(usage, deployment):
    if usage[0] is None or usage[1] is None or deployment["input_price"] is None or deployment["output_price"] is None:
        return None
    amount = Decimal(str(deployment["input_price"])) * usage[0] + Decimal(str(deployment["output_price"])) * usage[1]
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def usage_of(data):
    usage = data.get("usage") if isinstance(data,dict) else None
    if not isinstance(usage,dict):return None,None
    values = (usage.get("prompt_tokens", usage.get("input_tokens")), usage.get("completion_tokens", usage.get("output_tokens")))
    return tuple(value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None for value in values)


class Gateway:
    def __init__(self, store, vault, client):
        self.store, self.vault, self.client = store, vault, client
        self.active=0
        self.checks=0
        self.state_lock=threading.RLock()
        self.cluster=Cluster(store)
        self.policies=PolicyEngine(store,vault)
        self.secrets=SecretResolver(store,vault,client)

    @property
    def maintenance(self):return self.store.setting('maintenance',False)

    @maintenance.setter
    def maintenance(self,value):self.store.set_setting('maintenance',bool(value))

    @property
    def generation(self):return self.store.setting('workspace_generation',0)

    @generation.setter
    def generation(self,value):self.store.set_setting('workspace_generation',value)

    @contextmanager
    def checking(self):
        with self.state_lock:
            if self.maintenance:raise HTTPException(503,'Workspace maintenance is in progress')
            if self.active+self.checks>=self.store.setting('runtime',{}).get('max_concurrency',64):
                raise HTTPException(429,'Gateway concurrency limit reached')
            lease=self.cluster.acquire('provider_check')
            self.checks+=1
        try:
            yield
        finally:
            with self.state_lock:
                self.checks-=1
                self.cluster.release(lease)

    def port(self, name):
        value = self.store.one("SELECT * FROM ports WHERE name=? OR id=?", (name, name))
        if not value:
            raise HTTPException(404, "AI service not found")
        return decoded_port(value)

    def candidates(self, port, required=None, _rollout=True):
        requirements = set(port["requirements"]) | set(required or [])
        candidates, decisions = [], []
        ids=port['deployments']
        rows=self.store.all("SELECT d.*,c.name AS connection_name,c.endpoint,c.provider,c.secret,c.enabled AS connection_enabled,c.auth_header,c.auth_prefix,c.site,c.region,h.available AS observed_available,h.open_until AS observed_open_until FROM deployments d JOIN connections c ON c.id=d.connection_id LEFT JOIN deployment_health h ON h.deployment_id=d.id AND h.site=? WHERE d.id IN ("+','.join('?' for _ in ids)+")",(self.cluster.topology.site,*ids)) if ids else []
        inventory={row['id']:row for row in rows}
        modes={row['id']:row['mode'] for row in self.store.all('SELECT id,mode FROM sites')}
        instant=self.store.clock()
        for deployment_id in ids:
            row=inventory.get(deployment_id)
            if not row:
                decisions.append({"deployment_id": deployment_id, "eligible": False, "reason": "Deployment no longer exists"})
                continue
            deployment = decoded_deployment(row)
            evidence=deployment["verified"] if port.get("verified_only") else deployment["capabilities"]
            missing = sorted(requirements - set(evidence))
            disabled=not deployment["enabled"] or not deployment["connection_enabled"]
            available=deployment['observed_available'] if deployment['observed_available'] is not None else 1
            open_until=deployment['observed_open_until'] or 0
            if not self.cluster.topology.multi_dc:available,open_until=deployment['available'],deployment['open_until']
            unavailable=not available or open_until>instant
            placement=port.get('placement') or {}
            location_error=placement_reason(deployment,placement,self.cluster.topology)
            if deployment.get('site') and modes.get(deployment['site'])!='active':location_error='Model site is unavailable, draining or on standby'
            reason = "Deployment or connection is disabled" if disabled else ("Temporarily unavailable; circuit breaker or inventory check" if unavailable else ("Missing required verified capabilities: " + ", ".join(missing) if missing and port.get("verified_only") else "Missing required capabilities: " + ", ".join(missing) if missing else "Meets all required capabilities"))
            eligible = not disabled and not unavailable and not missing and not location_error and port["enabled"]
            decisions.append({"deployment_id": deployment_id, "model": deployment["model"], "connection": deployment["connection_name"], "eligible": bool(eligible), "reason": (location_error or reason) if port["enabled"] else "AI service is disabled","ingress_site":self.cluster.topology.site,"model_site":deployment.get("site") or "shared","model_region":deployment.get("region") or "unspecified"})
            if eligible:
                candidates.append(deployment)
        if port["strategy"] == "round_robin" and len(candidates) > 1:
            count = self.store.setting('round_robin:'+port['id'],0)
            offset = count % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        if (port.get('placement') or {}).get('routing','local_first')=='local_first':
            candidates.sort(key=lambda d:0 if d.get('site')==self.cluster.topology.site else 1 if not d.get('site') else 2)
        if _rollout:
            from .quality import active_release
            release=active_release(self.store,port['id'])
            if release:
                candidate=self.port(release['candidate_id'])
                config=release['config']
                if self.base_fingerprint(self.port(port['id']))!=config['stable_fingerprint'] or self.base_fingerprint(candidate)!=config['candidate_fingerprint']:
                    return [],decisions+[{'eligible':False,'reason':'Release evidence is stale; roll back and evaluate the changed services'}]
                other,trail=self.candidates({**candidate,'verified_only':candidate.get('verified_only') or port.get('verified_only')},required,_rollout=False)
                allowed=[]
                for item in other:
                    boundary=placement_reason(item,port.get('placement') or {},self.cluster.topology)
                    if boundary:trail.append({'deployment_id':item['id'],'eligible':False,'reason':boundary})
                    else:allowed.append(item)
                other=allowed
                candidates=[{**d,'rollout_lane':'stable','release_id':release['id']} for d in candidates]+[{**d,'rollout_lane':'canary','release_id':release['id']} for d in other]
                decisions+=trail
        return candidates, decisions

    def base_fingerprint(self, port):
        ids=port['deployments']
        rows=self.store.all("SELECT d.id,d.model,d.capabilities,d.enabled,d.input_price,d.output_price,d.metadata_hash,c.endpoint,c.provider,c.enabled AS connection_enabled,c.auth_header,c.auth_prefix,c.site,c.region FROM deployments d JOIN connections c ON c.id=d.connection_id WHERE d.id IN ("+','.join('?' for _ in ids)+")",ids) if ids else []
        inventory={row['id']:row for row in rows}
        deployments=[inventory.get(record_id) for record_id in ids]
        content = {"name": port["name"], "version": port["version"], "enabled": port["enabled"], "requirements": port["requirements"], "strategy": port["strategy"], "log_content":port.get("log_content",0),"project_id":port.get("project_id"),"verified_only":port.get("verified_only",0),"cache_ttl":port.get("cache_ttl",0),"max_input_chars":port.get("max_input_chars",200000),"max_output_tokens":port.get("max_output_tokens",32768),"deployments": deployments,"placement":port.get("placement") or {}}
        content['contract_revision']=INFERENCE_CONTRACT_REVISION
        return hashlib.sha256(json.dumps(content,sort_keys=True,separators=(",",":")).encode()).hexdigest()

    def fingerprint(self,port):
        from .quality import release_fingerprint
        base=self.base_fingerprint(port)
        release=release_fingerprint(self,port)
        return hashlib.sha256(encode({'base':base,'release':release}).encode()).hexdigest() if release else base

    def manifest(self, port):
        candidates, decisions = self.candidates(port)
        return {"protocol": PROTOCOL_VERSION, "contract_revision":INFERENCE_CONTRACT_REVISION, "id": port["id"], "name": port["name"], "description": port["description"], "version": port["version"], "fingerprint": self.fingerprint(port), "requirements": port["requirements"],"placement":port.get("placement") or {}, "invocation": {"interface": "openai-chat-completions", "path": "/v1/chat/completions", "model": port["name"]}, "deployments": [{"model": item["model"],"site":item.get("site") or "shared","region":item.get("region") or "unspecified", "capabilities": item["capabilities"], "verified": item["verified"], "evidence": "Declared capabilities; verified lists independently tested capabilities"} for item in candidates], "readiness": "ready" if candidates else "unavailable", "decisions": decisions}

    def authorize_port(self, principal, port, gateway_id=None):
        if not service_allowed(self.store,principal,port):
            raise HTTPException(403,"You are not permitted to use this AI service")
        return self.policies.resolve(principal,port,gateway_id)

    def budget_usage(self, project_id, instant):
        month=datetime.fromtimestamp(instant,timezone.utc).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        end=month.replace(year=month.year+1,month=1) if month.month==12 else month.replace(month=month.month+1)
        return self.store.one("SELECT COALESCE(SUM(CASE WHEN status='pending' THEN reserved_micros ELSE COALESCE(cost_micros,known_cost_micros+reserved_micros) END),0) AS n FROM events WHERE project_id=? AND started>=? AND started<?",(project_id,month.timestamp(),end.timestamp()))['n']

    def refresh_reservation(self, event_id, port, body, prices, attempts, policy):
        """Cover expanded checked input before provider IO; keep one admission."""
        size=len(encode(body['messages']))
        if size>port.get('max_input_chars',200000) or policy and size>policy['config']['max_input_chars']:
            raise HTTPException(422,'Checked input exceeds the configured character limit')
        with self.store.transaction():
            self.cluster.ensure_live()
            row=self.store.one('SELECT project_id,started,reserved_micros FROM events WHERE id=?',(event_id,))
            project=self.store.one('SELECT monthly_budget FROM projects WHERE id=?',(row['project_id'],)) if row['project_id'] else None
            if project and project['monthly_budget'] is not None:
                if prices['input_price'] is None or prices['output_price'] is None:
                    raise HTTPException(422,'Configure model pricing before using a budgeted project')
                reserve=cost_micros(reservation_units(body),prices)*attempts
                if reserve>row['reserved_micros']:
                    used=self.budget_usage(row['project_id'],row['started'])-row['reserved_micros']
                    if used+reserve>project['monthly_budget']*1_000_000:
                        raise HTTPException(402,'Project budget cannot cover the checked input reservation')
                    self.store.update('events',event_id,{'reserved_micros':reserve})
            self.policies.refresh_reservation(policy,event_id,body,attempts)

    def begin(self, principal, port, deployment, body, decisions, reservation_prices=None, kind='inference', attempt_limit=1, policy=None, delivery=None, admission=None,candidates=None):
        event_id = uid('req_')
        with self.store.transaction():
            self.cluster.ensure_serving()
            started=self.store.clock()
            if not self.store.setting('organization_enabled',True):raise HTTPException(403,'Organization access is disabled')
            principal=current_principal(self.store,principal)
            if body.get('_prompt_project_id'):require_project(self.store,principal,body['_prompt_project_id'])
            if admission:
                latest=self.port(port['id']) if kind!='probe' else port
                if admission.generation!=self.generation or admission.service_fingerprint!=self.fingerprint(latest):
                    raise HTTPException(409,'Service configuration changed during admission; retry with the current contract')
                if kind=='probe':
                    if principal['role']!='admin':raise HTTPException(403,'Administrator access is required for a probe')
                    current_policy=self.policies.resolve(principal,latest,policy['id'] if policy else None)
                else:
                    current_policy=self.authorize_port(principal,latest,policy['id'] if policy else None)
                    effective={**latest,'verified_only':latest.get('verified_only') or bool(policy and policy['config']['require_verified'])}
                    eligible,_=self.candidates(effective,admission.required)
                    if not set(admission.deployments).issubset({d['id'] for d in eligible}):
                        raise HTTPException(503,'Deployment readiness changed during admission; retry when eligible models are available')
                    if candidates is not None and port['strategy']=='round_robin':
                        order={d['id']:i for i,d in enumerate(eligible)}
                        candidates.sort(key=lambda d:order[d['id']])
                        deployment=candidates[0]
                    if candidates is not None and any(d.get('release_id') for d in candidates):
                        from .quality import active_release
                        release=active_release(self.store,port['id'])
                        cursor=self.store.setting('release_cursor:'+release['id'],0)
                        lane='canary' if (cursor*37+17)%100<release['percent'] else 'stable'
                        order={d['id']:i for i,d in enumerate(eligible)}
                        candidates.sort(key=lambda d:(d.get('rollout_lane')!=lane,order[d['id']]))
                        deployment=candidates[0]
                if (current_policy['id'] if current_policy else None)!=(policy['id'] if policy else None):
                    raise HTTPException(409,'Gateway assignment changed during admission')
            key = principal.get('key')
            context=body.get('pliqra') or {}
            if not isinstance(context,dict):raise HTTPException(422,'pliqra must be an object')
            project_id=key['project_id'] if key else port.get('project_id') or context.get('project_id')
            team_id=key.get('team_id') if key else context.get('team_id')
            if project_id and not key:require_project(self.store,principal,project_id)
            if team_id:
                if not project_id or not self.store.one('SELECT team_id FROM team_projects WHERE team_id=? AND project_id=?',(team_id,project_id)):raise HTTPException(403,'Team is not assigned to this project')
                if not key and principal['role']!='admin' and not self.store.one('SELECT user_id FROM team_members WHERE team_id=? AND user_id=?',(team_id,principal['id'])):raise HTTPException(403,'You are not a member of this team')
            reserve=0
            if key:
                current=self.store.one('SELECT * FROM api_keys WHERE id=?',(key['id'],))
                if not current or not current['active'] or (current['expires'] and current['expires']<=started):raise HTTPException(401,'Access key has been revoked or expired')
                used=self.store.one("SELECT COUNT(*) AS n FROM events WHERE key_id=? AND started>=? AND status!='rejected'",(key['id'],started-60))['n']
                if used>=current['rpm']:raise HTTPException(429,'Access key request-per-minute limit reached',headers={'Retry-After':'60'})
            if project_id:
                project=self.store.one('SELECT * FROM projects WHERE id=?',(project_id,))
                if not project:raise HTTPException(422,'Project not found')
                pending=self.store.one("SELECT COUNT(*) AS n FROM events WHERE project_id=? AND status='pending'",(project_id,))['n']
                if pending>=project['max_concurrency']:raise HTTPException(429,'Project concurrent request limit reached')
                if project['monthly_budget'] is not None:
                    pricing=reservation_prices or deployment
                    if pricing['input_price'] is None or pricing['output_price'] is None:raise HTTPException(422,'This project has a budget. Configure input and output pricing before using this model.')
                    reserve=cost_micros(reservation_units(body),pricing)*attempt_limit
                    spent=self.budget_usage(project_id,started)
                    if spent+reserve>project['monthly_budget']*1_000_000:raise HTTPException(402,"Project budget cannot cover this request's estimated reservation")
            self.policies.admit(policy,event_id,principal,port,body,project_id,team_id,attempt_limit)
            self.store.insert('events',{'id':event_id,'started':started,'principal':principal['id'],'actor_user_id':key.get('created_by') if key else principal['id'],'project_id':project_id,'team_id':team_id,'key_id':key['id'] if key else None,'port_id':port['id'],'port_name':port['name'],'port_version':port['version'],'deployment_id':deployment['id'],'model':deployment['model'],'connection_name':deployment['connection_name'],'status':'pending','stream':int(bool(body.get('stream'))),'reserved_micros':reserve,'decisions':encode(decisions),'request_kind':kind,'price_snapshot':encode({'input_price':deployment['input_price'],'output_price':deployment['output_price']}),'gateway_id':policy['id'] if policy else None,'gateway_name':policy['name'] if policy else None,'gateway_fingerprint':policy['fingerprint'] if policy else None,'delivery':delivery,'node_id':self.cluster.node,'ingress_site':self.cluster.topology.site,'model_site':deployment.get('site') or 'shared'})
            if body.get('prompt'):self.store.update('events',event_id,{'prompt_id':body['prompt']['id'],'prompt_version':body['prompt']['version']})
            if deployment.get('release_id'):
                self.store.set_setting('release_cursor:'+deployment['release_id'],self.store.setting('release_cursor:'+deployment['release_id'],0)+1)
                self.store.update('events',event_id,{'decisions':encode(decisions+[{'release_id':deployment['release_id'],'lane':deployment['rollout_lane'],'reason':'Selected by the admitted canary traffic allocation'}])})
            if candidates is not None and port['strategy']=='round_robin':
                cursor='round_robin:'+port['id']
                self.store.set_setting(cursor,self.store.setting(cursor,0)+1)
        if port.get('log_content') and kind=='inference':
            raw=encode(body)
            content={'request':body} if len(raw.encode('utf-8'))<=64000 else {'request_excerpt':raw.encode('utf-8')[:64000].decode('utf-8',errors='ignore'),'request_truncated':True}
            self.store.insert('event_content',{'id':event_id,'payload':self.vault.encrypt(encode(content)),'created':now()})
        return event_id,started

    def capture_response(self,event_id,value):
        row=self.store.one('SELECT payload FROM event_content WHERE id=?',(event_id,))
        if not row:return
        content=json.loads(self.vault.decrypt(row['payload']))
        raw=encode(value)
        if len(raw.encode('utf-8'))<=64000:content['response']=value
        else:content.update(response_excerpt=raw.encode('utf-8')[:64000].decode('utf-8',errors='ignore'),response_truncated=True)
        self.store.update('event_content',event_id,{'payload':self.vault.encrypt(encode(content))})

    def start_attempt(self, event_id, deployment):
        # Guardrails and external secret resolution can outlive a replica lease.
        # Check after those awaits, immediately before starting provider work.
        with self.store.transaction():
            self.cluster.ensure_live()
            attempt_id=uid('attempt_')
            self.store.insert('attempts',{'id':attempt_id,'event_id':event_id,'deployment_id':deployment['id'],
                'model':deployment['model'],'model_site':deployment.get('site') or 'shared',
                'started':self.store.clock(),'outcome':'pending'})
        return attempt_id

    def finish_attempt(self, attempt_id, deployment, outcome, data=None, http_status=None):
        with self.store.transaction():
            self._finish_attempt(attempt_id,deployment,outcome,data,http_status)

    def _finish_attempt(self, attempt_id, deployment, outcome, data=None, http_status=None):
        usage=usage_of(data or {})
        self.store.update('attempts',attempt_id,{'finished':now(),'outcome':outcome,'http_status':http_status,'input_tokens':usage[0],'output_tokens':usage[1],'cost_micros':cost_micros(usage,deployment)})
        if self.cluster.topology.multi_dc:
            current=runtime_health(self.store,self.cluster.topology.site,deployment['id']) or {'failure_count':0}
            if outcome=='success':observe_deployment(self.store,self.cluster.topology.site,deployment['id'],available=1,failure_count=0,open_until=0)
            elif outcome in ('connection_error','upstream_error','error','incomplete'):
                count=current['failure_count']+1
                observe_deployment(self.store,self.cluster.topology.site,deployment['id'],failure_count=count,open_until=self.store.clock()+30 if count>=3 else 0)
        elif outcome=='success':self.store.update('deployments',deployment['id'],{'failure_count':0,'open_until':0})
        elif outcome in ('connection_error','upstream_error','error','incomplete'):
            current=self.store.one('SELECT failure_count FROM deployments WHERE id=?',(deployment['id'],))
            if current:
                count=current['failure_count']+1
                self.store.update('deployments',deployment['id'],{'failure_count':count,'open_until':now()+30 if count>=3 else 0})

    def finish(self,event_id,started,deployment,status,data=None,ttft=None,error=None):
        with self.store.transaction():
            self.cluster.ensure_live()
            pending=self.store.one("SELECT id FROM attempts WHERE event_id=? AND outcome='pending' ORDER BY started DESC LIMIT 1",(event_id,))
            if pending:self._finish_attempt(pending['id'],deployment,status,data,200 if status=='success' else None)
            attempts=self.store.all('SELECT * FROM attempts WHERE event_id=?',(event_id,))
            complete=bool(attempts) and all(a['input_tokens'] is not None and a['output_tokens'] is not None for a in attempts)
            known=bool(attempts) and any(a['input_tokens'] is not None or a['output_tokens'] is not None for a in attempts)
            all_priced=bool(attempts) and all(a['cost_micros'] is not None for a in attempts)
            known_cost=sum(a['cost_micros'] or 0 for a in attempts)
            row=self.store.one('SELECT reserved_micros FROM events WHERE id=?',(event_id,))
            fields={'status':status,'finished':now(),'latency_ms':(time.perf_counter()-started)*1000,'ttft_ms':ttft,'input_tokens':sum(a['input_tokens'] or 0 for a in attempts) if known else None,'output_tokens':sum(a['output_tokens'] or 0 for a in attempts) if known else None,'usage_source':'reported' if complete else 'partial' if known else 'unknown','cost_micros':known_cost if all_priced else None,'known_cost_micros':known_cost,'reserved_micros':0 if all_priced else max(0,(row['reserved_micros'] if row else 0)-known_cost),'error':error,'model_site':deployment.get('site') or 'shared'}
            if not attempts:
                fields.update(input_tokens=0,output_tokens=0,usage_source='not_invoked',cost_micros=0,reserved_micros=0,
                              deployment_id=None,model=None,connection_name=None,model_site=None)
            self.store.update('events',event_id,fields)
            self.policies.settle(event_id,status,attempts)

    def rejected(self,body,principal,error,gateway_id=None):
        key=principal.get('key')
        identity=gateway_id or (key or {}).get('gateway_id')
        policy=self.store.one('SELECT id,name FROM gateway_profiles WHERE id=? OR slug=?',(identity,identity)) if identity else None
        self.store.insert('events',{'id':uid('req_'),'ingress_site':self.cluster.topology.site,'started':self.store.clock(),'finished':now(),'principal':principal['id'],'actor_user_id':key.get('created_by') if key else principal['id'],'key_id':key['id'] if key else None,'project_id':key['project_id'] if key else None,'team_id':key.get('team_id') if key else None,'port_name':str(body.get('model',''))[:256],'status':'rejected','stream':int(bool(body.get('stream'))),'cost_micros':0,'usage_source':'not_invoked','error':encode(error.detail) if isinstance(error.detail,dict) else str(error.detail),'decisions':encode([{'eligible':False,'reason':'Rejected before upstream inference','http_status':error.status_code}]),'gateway_id':policy['id'] if policy else None,'gateway_name':policy['name'] if policy else None})

    async def chat(self,body,principal,binding=None,request_kind='inference',gateway_id=None):
        try:
            from .prompts import expand
            body=expand(self.store,principal,body)
            return await self.route(body,principal,binding,request_kind,gateway_id)
        except HTTPException as exc:
            recorded=isinstance(exc.detail,dict) and exc.detail.get('request_id')
            if not recorded and exc.status_code in (402,403,409,422,429,503):self.rejected(body,principal,exc,gateway_id)
            raise

    async def route(self,body,principal,binding=None,request_kind='inference',gateway_id=None):
        with self.store.lock:
            body,port,candidates,decisions,policy,admission=self.prepare(body,principal,binding,request_kind,gateway_id)
        return await self.execute(body,principal,port,candidates,decisions,request_kind,policy,admission=admission)

    def prepare(self,body,principal,binding=None,request_kind='inference',gateway_id=None,required_capabilities=None):
        if binding and (binding['principal']!=principal['id'] or binding['expires']<=now()):
            raise HTTPException(403,'Binding belongs to another identity or has expired')
        port=self.port(binding['port_id'] if binding else body['model'])
        policy=self.authorize_port(principal,port,gateway_id or (binding or {}).get('gateway_id'))
        service_fingerprint=self.fingerprint(port)
        if binding and (body['model'] not in (port['id'],port['name']) or binding['fingerprint']!=service_fingerprint):
            raise HTTPException(409,'Service contract changed. Discover and bind again; no silent model substitution was performed.')
        if binding and (binding.get('gateway_id')!=(policy['id'] if policy else None) or binding.get('gateway_fingerprint')!=(policy['fingerprint'] if policy else None)):
            raise HTTPException(409,'Gateway policy or guardrail configuration changed. Discover and bind again.')
        if not port['enabled']:raise HTTPException(409,'AI service is disabled')
        if len(encode(body['messages']))>port.get('max_input_chars',200000):raise HTTPException(422,'Input exceeds this service\'s configured character limit')
        default_output=min(1024,port.get('max_output_tokens',32768),policy['config']['max_output_tokens'] if policy else 131072)
        requested=body.get('max_completion_tokens',body.get('max_tokens',default_output))
        if requested>port.get('max_output_tokens',32768):raise HTTPException(422,'Output token limit exceeds this service\'s policy')
        body={**body}
        if 'max_completion_tokens' not in body:body['max_tokens']=requested
        self.policies.preflight(policy,body,principal)
        if policy and policy['config']['require_verified']:
            port={**port,'verified_only':True}
        required=set(required_capabilities or ['chat'])
        if body.get('stream') and not self.policies.buffered(policy):required.add('streaming')
        if requests_tools(body):required.add('tools')
        if body.get('response_format',{}).get('type') in ('json_object','json_schema'):required.add('structured_output')
        for message in body['messages']:
            if isinstance(message.get('content'),list) and any(part.get('type') in ('image_url','input_image') for part in message['content'] if isinstance(part,dict)):required.add('vision')
        if binding:
            required.update(json.loads(binding['requirements']))
        candidates,decisions=self.candidates(port,required)
        if not candidates:raise HTTPException(422,{'message':'No deployment satisfies the required capabilities','decisions':decisions})
        return body,port,candidates,decisions,policy,AdmissionSnapshot(service_fingerprint,self.generation,tuple(sorted(required)),tuple(d['id'] for d in candidates))

    async def execute(self,body,principal,port,candidates,decisions,kind='inference',policy=None,admission=None):
        if admission is None:
            with self.store.lock:
                admission=AdmissionSnapshot(self.fingerprint(port),self.generation)
        with self.state_lock:
            if self.maintenance:raise HTTPException(503,'Workspace maintenance is in progress')
            if self.active+self.checks>=self.store.setting('runtime',{}).get('max_concurrency',64):raise HTTPException(429,'Gateway concurrency limit reached')
            lease=self.cluster.acquire('inference')
            self.active+=1
        released=False
        def release():
            nonlocal released
            with self.state_lock:
                if not released:
                    released=True
                    self.active-=1
                    self.cluster.release(lease)
        chosen=candidates[0]
        response=None
        event_id=None
        started=time.perf_counter()
        streaming=False
        cache_id=None
        forced=body.get('tool_choice',body.get('function_call')) not in (None,'auto')
        buffered=bool(body.get('stream') and (self.policies.buffered(policy) or forced))
        delivery=('guarded_buffer' if self.policies.buffered(policy) else 'adapter_buffer') if buffered else 'live_stream' if body.get('stream') else 'json'
        try:
            if not policy and not any(d.get('release_id') for d in candidates) and port.get('cache_ttl',0)>0 and not body.get('stream') and not requests_tools(body) and kind=='inference':
                cache_id=hashlib.sha256((principal['id']+self.cluster.topology.site+self.fingerprint(port)+json.dumps(body,sort_keys=True)).encode()).hexdigest()
                cached=self.store.one('SELECT payload FROM cache_entries WHERE id=? AND expires>?',(cache_id,now()))
                if cached:
                    packet=json.loads(self.vault.decrypt(cached['payload']))
                    cached_deployment=next((d for d in candidates if d['id']==packet.get('deployment_id')),None)
                    if packet.get('format')!='pliqra/cache/1' or not cached_deployment:cached=None
                if cached:
                    chosen=cached_deployment
                    event_id,_=self.begin(principal,port,chosen,body,decisions,{'input_price':0,'output_price':0},kind,admission=admission)
                    data=valid_completion(packet['response'])
                    data['id']=event_id
                    data['pliqra']={'cache_hit':True,'billable_tokens':0}
                    self.capture_response(event_id,data)
                    self.store.update('events',event_id,{'status':'success','finished':now(),'latency_ms':(time.perf_counter()-started)*1000,'usage_source':'cache','input_tokens':0,'output_tokens':0,'cost_micros':0,'cache_hit':1,'reserved_micros':0,'decisions':encode([{'selected':True,'reason':'Encrypted exact-response cache; no upstream call was made'}])})
                    return JSONResponse(data,headers={'X-Request-ID':event_id,'X-Pliqra-Model':chosen['model'],'X-Pliqra-Cache':'HIT',**model_headers(self,chosen)})
            prices={field:max(item[field] for item in candidates) if all(item[field] is not None for item in candidates) else None for field in ('input_price','output_price')}
            event_id,_=self.begin(principal,port,chosen,body,decisions,prices,kind,len(candidates),policy,delivery,admission,candidates)
            chosen=candidates[0]
            if policy:
                checked=await self.policies.check(policy,'input',body,self.client,event_id)
                if checked['messages']!=body['messages']:
                    self.refresh_reservation(event_id,port,checked,prices,len(candidates),policy)
                body=checked
            upstream_body={**body,'stream':False} if buffered else body
            attempts=[]
            for deployment in candidates:
                chosen=deployment
                self.secrets.client=self.client
                secret=await self.secrets.resolve(self.vault.decrypt(deployment['secret']))
                url,payload,auth=upstream_request(deployment,upstream_body,deployment['model'],secret)
                payload.pop('pliqra',None)
                payload.pop('modelport',None)
                payload.pop('prompt',None)
                payload.pop('_prompt_project_id',None)
                attempt_id=self.start_attempt(event_id,deployment)
                try:
                    response=await self.client.send(self.client.build_request('POST',url,json=payload,headers=auth),stream=True)
                except httpx.HTTPError:
                    self.finish_attempt(attempt_id,deployment,'connection_error')
                    attempts.append({'deployment_id':deployment['id'],'model':deployment['model'],'outcome':'Connection failed before a response'})
                    continue
                if response.status_code>=400:
                    status=response.status_code
                    error_data={}
                    try:
                        chunks=[];size=0
                        async for chunk in response.aiter_bytes():
                            size+=len(chunk)
                            if size>2_000_000:break
                            chunks.append(chunk)
                        if size<=2_000_000:error_data=json.loads(b''.join(chunks))
                    except (httpx.HTTPError,ValueError):pass
                    await response.aclose()
                    self.finish_attempt(attempt_id,deployment,'upstream_error',error_data,status)
                    attempts.append({'deployment_id':deployment['id'],'model':deployment['model'],'outcome':f'HTTP {status}; fallback only before streaming'})
                    response=None
                    if status in (429,502,503,504):continue
                    break
                break
            trail=decisions+attempts+[{'deployment_id':chosen['id'],'model':chosen['model'],'selected':response is not None,'reason':'Selected the first available eligible deployment' if port['strategy']=='priority' else 'Selected by round-robin among eligible deployments'}]
            if chosen.get('release_id'):trail.append({'release_id':chosen['release_id'],'lane':chosen['rollout_lane'],'reason':'Actual release lane after pre-stream fallback'})
            with self.store.transaction():
                self.store.update('events',event_id,{'deployment_id':chosen['id'],'model':chosen['model'],'connection_name':chosen['connection_name'],'decisions':encode(trail),'price_snapshot':encode({'attempts':[{k:row.get(k) for k in ('id','model','input_price','output_price')} for row in candidates]})})
                if response is not None:
                    self.store.update('connections',chosen['connection_id'],{'status':'connected','last_checked':now(),'last_error':None})
            if response is None:
                self.finish(event_id,started,chosen,'error',error='All eligible upstreams failed or rejected this request')
                raise HTTPException(502,{'message':'All eligible upstreams failed or rejected this request','request_id':event_id})
            if body.get('stream') and not buffered:
                async def iterator():
                    chunks=self.stream(response,chosen,event_id,started)
                    try:
                        async for chunk in chunks:yield chunk
                    finally:
                        await chunks.aclose()
                        release()
                streaming=True
                return StreamingResponse(iterator(),media_type='text/event-stream',headers={'X-Request-ID':event_id,'X-Pliqra-Model':chosen['model'],'X-Pliqra-Delivery':delivery,**model_headers(self,chosen),**({'X-Pliqra-Gateway':policy['id']} if policy else {}),'Cache-Control':'no-cache','X-Accel-Buffering':'no'},background=BackgroundTask(release))
            data=await response_json(response)
            await response.aclose()
            if chosen['provider']=='ollama':data=ollama_result(data,event_id,chosen['model'])
            elif chosen['provider'] in ('responses','anthropic'):
                from .native import provider_result
                data=provider_result(chosen['provider'],data,event_id,chosen['model'])
            valid_completion(data)
            validate_tool_choice(data,body)
            self.finish_attempt(attempt_id,chosen,'success',data,200)
            if self.policies.buffered(policy):data=checked_completion(data,event_id,chosen['model'])
            if policy:data=await self.policies.check(policy,'output',body,self.client,event_id,response=data)
            self.capture_response(event_id,data)
            self.finish(event_id,started,chosen,'success',data,(time.perf_counter()-started)*1000 if buffered else None)
            self.mark_verified(chosen,'chat')
            if cache_id:
                packet={'format':'pliqra/cache/1','deployment_id':chosen['id'],'response':data}
                self.store.run('INSERT INTO cache_entries VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,expires=excluded.expires',(cache_id,port['id'],self.vault.encrypt(encode(packet)),now()+port['cache_ttl']))
            headers={'X-Request-ID':event_id,'X-Pliqra-Model':chosen['model'],'X-Pliqra-Cache':'MISS','X-Pliqra-Delivery':delivery,**model_headers(self,chosen)}
            if chosen.get('release_id'):headers['X-Pliqra-Lane']=chosen['rollout_lane']
            if policy:headers['X-Pliqra-Gateway']=policy['id']
            if buffered:
                async def guarded_chunks():
                    for choice in data['choices']:
                        message=choice['message']
                        if message.get('tool_calls'):
                            message={**message,'tool_calls':[{**t,'index':i} for i,t in enumerate(message['tool_calls'])]}
                        yield sse({'id':event_id,'object':'chat.completion.chunk','model':data.get('model',chosen['model']),'choices':[{'index':choice.get('index',0),'delta':message,'finish_reason':choice.get('finish_reason','stop')}]})
                    yield sse({'id':event_id,'object':'chat.completion.chunk','choices':[],'usage':data.get('usage',{})})
                    yield 'data: [DONE]\n\n'
                return StreamingResponse(guarded_chunks(),media_type='text/event-stream',headers={**headers,'Cache-Control':'no-cache'})
            return JSONResponse(data,headers=headers)
        except HTTPException as exc:
            if response:await response.aclose()
            if event_id and not self.cluster.fenced and self.store.one("SELECT id FROM events WHERE id=? AND status='pending'",(event_id,)):
                guard=isinstance(exc.detail,dict) and str(exc.detail.get('code','')).startswith('guardrail_')
                self.finish(event_id,started,chosen,'blocked' if guard and exc.status_code==422 else 'error',error='Guardrail blocked delivery' if guard else 'Upstream request could not be completed')
            if event_id:
                exc.detail={**exc.detail,'request_id':event_id} if isinstance(exc.detail,dict) else {'message':exc.detail,'request_id':event_id}
            raise
        except asyncio.CancelledError:
            if event_id:self.finish(event_id,started,chosen,'cancelled',error='Client disconnected; upstream cancellation requested')
            if response:await response.aclose()
            raise
        except (httpx.HTTPError,ValueError,TypeError,KeyError,AttributeError):
            if response:await response.aclose()
            if event_id:self.finish(event_id,started,chosen,'error',error='Endpoint returned an unreadable or incomplete response')
            raise HTTPException(502,{'message':'Endpoint returned an unreadable or incomplete response','request_id':event_id})
        finally:
            if not streaming:release()

    def mark_verified(self, deployment, capability, *additional):
        with self.store.transaction():
            row = self.store.one("SELECT verified FROM deployments WHERE id=?", (deployment["id"],))
            if row:
                verified = set(json.loads(row["verified"])) | {capability, *additional}
                self.store.update("deployments", deployment["id"], {"verified": encode(sorted(verified)),"verified_at":now()})

    async def stream(self, response, deployment, event_id, started):
        usage, terminal, ttft, error = {}, False, None, None
        captured=[];capture_size=0
        capture=bool(self.store.one("SELECT id FROM event_content WHERE id=?",(event_id,)))
        tool_index=0
        tool_seen=False
        try:
            lines=bounded_lines(response)
            if deployment['provider'] in ('responses','anthropic'):
                from .native_streaming import upstream_lines
                lines=upstream_lines(response,deployment['provider'],event_id,deployment['model'])
            async def observed(source):
                nonlocal capture_size
                async for line in source:
                    if capture and capture_size<64000:
                        captured.append(line.encode('utf-8')[:max(0,63999-capture_size)].decode('utf-8',errors='ignore'))
                        capture_size+=len(line.encode('utf-8'))+1
                    yield line
            lines=observed(lines)
            if deployment['provider']!='ollama':lines=sse_data(lines,comments=True)
            async for line in lines:
                if not line.strip():
                    continue
                if deployment["provider"] == "ollama":
                    obj = strict_json(line)
                    if not isinstance(obj,dict):raise ValueError('Invalid stream object')
                    if obj.get("error"):
                        raise ValueError("Upstream reported a streaming error")
                    message = obj.get("message", {})
                    text = message.get("content", "")
                    calls=ollama_calls(message.get('tool_calls'),tool_index)
                    for call in calls:
                        call['index']=tool_index
                        tool_index+=1
                    tool_seen=tool_seen or bool(calls)
                    if ttft is None and (text or message.get("tool_calls")):
                        ttft = (time.perf_counter() - started) * 1000
                    chunk = {"id": event_id, "object": "chat.completion.chunk", "model": deployment["model"], "choices": [{"index": 0, "delta": {"content": text, **({"tool_calls":calls} if calls else {}), **({'reasoning_content':message['thinking']} if message.get('thinking') else {})}, "finish_reason": ('tool_calls' if tool_seen else 'stop') if obj.get("done") else None}]}
                    if obj.get("done"):
                        normalized = ollama_result(obj, event_id, deployment["model"])
                        usage = normalized
                        if "usage" in normalized:
                            chunk["usage"] = normalized["usage"]
                        terminal = True
                    yield sse(chunk)
                    if terminal:
                        yield "data: [DONE]\n\n"
                        break
                else:
                    if line.startswith(":"):
                        yield line + "\n\n"
                        continue
                    payload = line.strip()
                    if payload == "[DONE]":
                        terminal = True
                        yield "data: [DONE]\n\n"
                        break
                    obj = strict_json(payload)
                    if not isinstance(obj,dict):raise ValueError('Invalid stream object')
                    if obj.get("error"):
                        raise ValueError("Upstream reported a streaming error")
                    if obj.get("usage"):
                        usage = obj
                    if ttft is None and any(choice.get("delta", {}).get("content") or choice.get("delta", {}).get("tool_calls") for choice in obj.get("choices", [])):
                        ttft = (time.perf_counter() - started) * 1000
                    yield sse(obj)
            if not terminal:
                error = "Upstream stream ended without its completion marker"
                yield sse({"error": {"message": error, "request_id": event_id, "type": "incomplete_stream"}})
        except (asyncio.CancelledError,GeneratorExit):
            if not terminal:self.finish(event_id, started, deployment, "cancelled", usage, ttft, "Client disconnected; upstream cancellation requested")
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            error = "Upstream stream failed; no fallback was attempted after streaming started"
            yield sse({"error": {"message": error, "request_id": event_id, "type": "upstream_stream_error"}})
        finally:
            await response.aclose()
            if capture:self.capture_response(event_id,{"stream_sse":"\n".join(captured),"truncated":capture_size>=64000})
            existing = self.store.one("SELECT status FROM events WHERE id=?", (event_id,))
            if existing and existing["status"] == "pending":
                self.finish(event_id, started, deployment, "success" if terminal and not error else "incomplete", usage, ttft, error)
                if terminal and not error:
                    self.mark_verified(deployment, "chat", "streaming")
