"use strict";

const fleetAction=(label,action,id,primary=false)=>btn(label,action,null,primary?'primary':'ghost',`data-id="${esc(id||'')}"`);
const fleetList=value=>[...new Set(String(value||'').split(',').map(v=>v.trim()).filter(Boolean))];
const fleetText=(label,name,value)=>`<div class="field"><label for="fleet-${esc(name)}">${esc(label)}</label><textarea id="fleet-${esc(name)}" name="${esc(name)}" rows="3">${esc(value||'')}</textarea></div>`;

window.PliqraFleet={
 async load(){
  if(!isAdmin()||state.view!=='fleet')return;
  state.fleet||={tab:'members',member:'',team:''};
  const f=state.fleet;
  const query=new URLSearchParams({member_id:f.member||'',team_id:f.team||''});
  const [data,summary,rails,teams]=await Promise.all([api('/api/v1/fleet'),api('/api/v1/fleet/summary?'+query),api('/api/v1/guardrails'),api('/api/v1/teams')]);
  Object.assign(f,data,{summary});state.guardrails=rails;state.teams=teams;
 },
 views:{fleet(){return PliqraFleet.view();}},
 view(){
  if(!isAdmin())return empty('shield','Administrator access required','Pairings and cross-instance policies are managed by administrators.');
  const f=state.fleet||{},s=f.summary||{totals:{},included_members:[],excluded_members:[],teams:[]},t=s.totals;
  let content=head('Fleet management','Bring independent Pliqra instances together. Keep each workspace in control.',fleetAction('Refresh members','fleet-sync',null)+fleetAction('Connect an instance','fleet-member-new',null,true));
  content+=`<div class="stats">${[['Fresh instances',s.included_members.length],['Requests · 24h',num(t.requests)],['Tokens reported',num((t.input_tokens||0)+(t.output_tokens||0))],['Known est. cost',money(t.known_cost_micros)]].map(([label,value])=>`<div class="stat"><div class="stat-top">${esc(label)}</div><div class="stat-value">${esc(value)}</div></div>`).join('')}</div>`;
  content+=`<div class="notice">${s.excluded_members.length} paused, unavailable or stale instances excluded from totals. Snapshots cover the last 24 hours and must be less than five minutes old. ${num(t.unknown_usage)} calls have unknown usage; ${num(t.unknown_cost)} have unknown cost.</div>`;
  content+=PliqraEnterprise.tabs([['members','Instances'],['policies','Policy proposals'],['pairings','Trusted controllers']],f.tab||'members','fleet-tab');
  if(f.tab==='policies'){
   content+=`<section class="panel"><div class="panel-head"><div><h2>Plan. Review. Apply.</h2><p>Every target is mapped explicitly. A different administrator approves the exact plan.</p></div>${fleetAction('Reuse a gateway','fleet-reuse')}${fleetAction('New policy proposal','fleet-propose',null,true)}</div>${(f.proposals||[]).length?eTable(['Proposal','Decision','Targets','Created',''],f.proposals.map(p=>`<tr><td><strong>${esc(p.name)}</strong><small>${esc(p.fingerprint.slice(0,12))}</small></td><td>${badge(p.status,p.status)}</td><td>${p.targets.filter(t=>t.status==='applied').length}/${p.targets.length} applied</td><td>${date(p.created)}</td><td>${fleetAction('Review & delivery','fleet-proposal',p.id)}</td></tr>`)):empty('route','Publish a shared policy','Define a gateway once, map its services in each instance, then request a second review.')}</section>`;
  }else if(f.tab==='pairings'){
   content+=`<section class="panel"><div class="panel-head"><div><h2>Let another controller connect here</h2><p>Create a scoped pairing on this member, then paste its one-time token into the controller.</p></div>${fleetAction('Trust a controller','fleet-trust-new',null,true)}</div>${(f.trusts||[]).length?eTable(['Controller','Granted access','Gateway namespace','State',''],f.trusts.map(t=>`<tr><td>${esc(t.name)}</td><td>${esc(t.scopes.join(', '))}</td><td><code>${esc(t.prefix)}*</code></td><td>${t.active?'Active':'Revoked'}</td><td>${t.active?fleetAction('Revoke','fleet-revoke',t.id):''}</td></tr>`)):empty('shield','No controller has access','Pairing is explicit. Model credentials, users and raw conversations are not shared.')}</section>`;
  }else{
   const members=(f.members||[]).filter(m=>!f.member||m.id===f.member);
   const selected=(f.members||[]).find(m=>m.id===f.member);
   content+=`<section class="panel"><form id="fleet-filters" class="panel-filters">${selectField('Instance','member_id',[['','All independent instances'],...(f.members||[]).map(m=>[m.id,m.name])],f.member||'')}${selectField('Team · select its instance first','team_id',[['','All teams'],...(selected?.snapshot?.teams||[]).filter(t=>t.id).map(t=>[t.id,t.name])],f.team||'')}${btn('Apply filters','fleet-filter',null)}</form><div class="panel-head"><div><h2>Connected instances</h2><p>Independent databases and quotas. Fleet management does not pool their token allowances.</p></div></div>${members.length?eTable(['Instance','Snapshot / readiness','Services','Policy drift',''],members.map(m=>{const s=m.snapshot||{},fresh=m.enabled&&!m.last_error&&m.last_sync>Date.now()/1000-300;return `<tr><td><strong>${esc(m.name)}</strong><small>${esc(m.endpoint)}</small></td><td>${badge(fresh&&s.ready?'success':'disconnected',!m.enabled?'Paused':m.last_error?'Unreachable':fresh?(s.ready?'Ready':'Not serving'):'Stale')}<small>${date(m.last_sync)}</small></td><td>${num(s.service_total??s.services?.length)}</td><td>${(s.managed_gateways||[]).filter(g=>g.drifted).length} changed</td><td>${fleetAction('Inspect','fleet-member',m.id)}${fleetAction('Edit','fleet-member-edit',m.id)}</td></tr>`;})):empty('plug','Connect an independent instance','Create a pairing in its Fleet → Trusted controllers page, then connect it here.')}</section>`;
   if(s.teams.length)content+=`<section class="panel section-gap"><div class="panel-head"><h2>Teams across instances</h2></div>${eTable(['Instance','Team','Requests','Reported tokens','Known est. cost'],s.teams.map(t=>`<tr><td>${esc(t.member_name)}</td><td>${esc(t.name)}</td><td>${num(t.requests)}</td><td>${num(t.input_tokens+t.output_tokens)}</td><td>${money(t.known_cost_micros)}</td></tr>`))}</section>`;
  }
  return content+footer();
 },
 memberModal(id){
  const m=(state.fleet.members||[]).find(m=>m.id===id);
  modal(m?'Edit connected instance':'Connect an independent instance','The member administrator chooses the access granted to this controller.',field('Instance name','name','','text',m?.name||'','required maxlength="100"')+field('Instance base URL','endpoint','https://instance.example.com','url',m?.endpoint||'','required '+(m?'readonly':''))+field(m?'New pairing token · blank keeps current':'One-time pairing token','token','plq_fleet_…','password','','autocomplete="off" '+(m?'':'required'))+eCheck('Include this instance in synchronization','enabled',m?.enabled??true),'Save connection',async form=>{const body={name:form.get('name'),endpoint:form.get('endpoint'),enabled:form.has('enabled')};if(form.get('token'))body.token=form.get('token');await api('/api/v1/fleet/members'+(m?'/'+m.id:''),{method:m?'PUT':'POST',body:JSON.stringify(body)});await changed('Instance connection saved.');});
 },
 trustModal(){
  modal('Trust a controller','This grants access to this workspace only. Save the token once, then use it on your controller.',field('Controller name','name','','text','','required maxlength="100"')+field('Managed gateway address prefix','prefix','','text','fleet-','required pattern="[a-z0-9][a-z0-9-]*" maxlength="40"')+'<p>Read-only metadata access includes service names, usage totals and policy inventory. Raw prompts, responses, user records and provider credentials are excluded.</p>'+eCheck('Also allow creating/updating managed gateway policies','policy',false),'Create pairing',async form=>{const result=await api('/api/v1/fleet/trusts',{method:'POST',body:JSON.stringify({name:form.get('name'),prefix:form.get('prefix'),scopes:['status',...(form.has('policy')?['policy']:[])]})});await refreshData();renderView();PliqraUI.secretModal('Controller pairing token',result.token,'Shown once. Store it only on the trusted controller; revoke this pairing to remove its access.');});
 },
 railRow(step={}){
  return `<div class="rail-step fabric-row">${field('Check alias','fleet_rail_alias','e.g. safety','text',step.guardrail_id||'','required')}${PliqraFabric.rowSelect('rail_stage','Stage',[['input','Input'],['output','Output'],['both','Input + output']],step.stages?.length===2?'both':step.stages?.[0]||'input')}${PliqraFabric.rowSelect('rail_mode','Mode',[['enforce','Enforce'],['observe','Observe']],step.mode||'enforce')}${PliqraFabric.rowSelect('rail_error','On error',[['block','Stop the request'],['allow','Allow and record']],step.on_error||'block')}${ibtn('Remove check','gw-remove-row','close')}</div>`;
 },
 policyModal(template){
  const c=template?.config||{},members=(state.fleet.members||[]).filter(m=>m.enabled&&m.snapshot?.scopes?.includes('policy'));
  if(!members.length){toast('Connect at least one member with policy permission first.',true);return;}
  let html=field('Proposal name','proposal_name','','text','','required maxlength="100"')+field('Gateway name','name','','text',template?.name||'Team AI','required maxlength="100"')+field('Gateway address','slug','','text',template?.slug||'fleet-team-ai','required pattern="[a-z0-9][a-z0-9-]{0,62}"')+field('Service aliases · comma separated','service_aliases','chat, embeddings','text',(c.services||['chat']).join(', '),'required')+field('Project alias · optional','project_alias','','text',c.project_id||'')+field('Team aliases · optional, comma separated','team_aliases','','text',(c.team_ids||[]).join(', '));
  html+=`<h3>Targets</h3>${members.map(m=>eCheck(m.name+' · '+m.endpoint,'target',false).replace('name="target"','name="target" value="'+esc(m.id)+'"')).join('')}<div class="fabric-editor-head"><h3>Request and token quotas</h3>${btn('Add quota','gw-add-quota','plus')}</div><div id="quota-rows">${(c.limits||[{metric:'requests',scope:'gateway',window_seconds:60,limit:120}]).map(PliqraFabric.quotaRow).join('')}</div><div class="fabric-editor-head"><h3>Guardrails</h3>${btn('Add check','fleet-add-rail','plus')}</div><div id="fleet-rails">${(c.guardrails||[]).map(PliqraFleet.railRow).join('')}</div>`;
  html+=`<details><summary>Inference controls</summary>${field('Concurrent requests','max_concurrency','','number',c.max_concurrency??20,'required min="1" max="200"')}${field('Maximum input characters','max_input_chars','','number',c.max_input_chars??200000,'required min="1" max="1000000"')}${field('Maximum output tokens','max_output_tokens','','number',c.max_output_tokens??4096,'required min="1" max="131072"')}${eCheck('Require verified capabilities','require_verified',c.require_verified)}${eCheck('Allow tool calls','allow_tools',c.allow_tools??true)}${eCheck('Allow image input','allow_vision',c.allow_vision??true)}${eCheck('Enable gateway','enabled',c.enabled??true)}</details>`;
  modal('Prepare a shared gateway policy','Use aliases now; choose the exact local resources in each target next.',html,'Map target resources',async form=>{
   const metrics=form.getAll('quota_metric'),scopes=form.getAll('quota_scope'),windows=form.getAll('quota_window'),limits=form.getAll('quota_limit'),rails=form.getAll('fleet_rail_alias'),stages=form.getAll('rail_stage'),modes=form.getAll('rail_mode'),errors=form.getAll('rail_error');
   const draft={name:form.get('proposal_name'),document:{name:form.get('name'),slug:form.get('slug'),config:{services:fleetList(form.get('service_aliases')),project_id:form.get('project_alias')||null,team_ids:fleetList(form.get('team_aliases')),limits:metrics.map((metric,i)=>({metric,scope:scopes[i],window_seconds:Number(windows[i]),limit:Number(limits[i])})),guardrails:rails.map((guardrail_id,i)=>({guardrail_id,stages:stages[i]==='both'?['input','output']:[stages[i]],mode:modes[i],on_error:errors[i]})),max_concurrency:Number(form.get('max_concurrency')),max_input_chars:Number(form.get('max_input_chars')),max_output_tokens:Number(form.get('max_output_tokens')),require_verified:form.has('require_verified'),allow_tools:form.has('allow_tools'),allow_vision:form.has('allow_vision'),enabled:form.has('enabled')}}};
   const ids=form.getAll('target');if(!ids.length)throw new Error('Select at least one target instance.');if(!draft.document.config.services.length)throw new Error('Add at least one service alias.');PliqraFleet.mappingModal(draft,ids);
  });
 },
 mappingModal(draft,ids){
  const config=draft.document.config,targets=ids.map(id=>state.fleet.members.find(m=>m.id===id)),bindings=[];
  let html='';
  for(const m of targets){
   html+=`<h3>${esc(m.name)}</h3>`;
   const inventory=m.snapshot.policy_inventory||{};
   const groups={services:[config.services,m.snapshot.services||[]],guardrails:[[...new Set(config.guardrails.map(r=>r.guardrail_id))],inventory.guardrails||[]],projects:[config.project_id?[config.project_id]:[],inventory.projects||[]],teams:[config.team_ids,inventory.teams||[]]};
   for(const [kind,[aliases,options]] of Object.entries(groups))for(const alias of aliases){const name='mapping_'+bindings.length;bindings.push({member:m.id,kind,alias,name});html+=selectField(kind+' · '+alias,name,[['','Choose a local resource'],...options.map(o=>[o.id,o.name])]);}
  }
  modal('Map resources in each instance','Planning validates every local mapping. No gateway policy is applied yet.',html,'Create review proposal',async form=>{
   const mapped=ids.map(member_id=>({member_id,mapping:{services:{},guardrails:{},projects:{},teams:{}}}));
   for(const binding of bindings){const value=form.get(binding.name);if(!value)throw new Error('Choose every local resource.');mapped.find(m=>m.member_id===binding.member).mapping[binding.kind][binding.alias]=value;}
   const result=await api('/api/v1/fleet/proposals',{method:'POST',body:JSON.stringify({...draft,targets:mapped})});await changed('Proposal prepared. A different administrator must approve it.');PliqraFleet.review(result.id);
  });
 },
 review(id){
  const p=state.fleet.proposals.find(p=>p.id===id);if(!p)return;
  let html=`<p><strong>${esc(p.name)}</strong> · ${esc(p.status)}</p><p>Fingerprint: <code>${esc(p.fingerprint)}</code></p>`;
  const person=id=>state.users.find(u=>u.id===id)?.name||id;
  html+=`<p>Prepared by ${esc(person(p.author))} · ${p.approved_by?'Decision by '+esc(person(p.approved_by))+' · '+date(p.approved):'Awaiting a separate administrator'}</p>`;
  for(const target of p.targets){const m=state.fleet.members.find(m=>m.id===target.member_id),plan=target.plan,c=plan.document.config;html+=`<section class="panel section-gap"><h3>${esc(m?.name||target.member_id)}</h3><p><code>/g/${esc(plan.document.slug)}/v1</code> · ${plan.expected_fingerprint?'Update existing gateway':'Create managed gateway'} · ${esc(target.status)}</p><p>${esc(c.limits.map(q=>q.limit+' '+q.metric+' / '+q.scope+' / '+q.window_seconds+'s').join(' · ')||'No gateway quota configured')}</p><p>${c.guardrails.length} guardrail steps · ${c.max_concurrency} concurrent · ${c.max_output_tokens} maximum output tokens</p><small>Plan expires ${date(plan.expires)}</small>${target.last_error?'<div class="notice">'+esc(target.last_error)+'</div>':''}<details><summary>Exact before/after policy</summary><pre>${esc(JSON.stringify({before:plan.previous_document,after:plan.document},null,2))}</pre></details></section>`;}
  const actions=(p.status==='pending'&&p.author!==state.session.user.id?fleetAction('Reject','fleet-reject',id)+fleetAction('Approve exact plan','fleet-approve',id,true):'')+(p.status==='approved'?fleetAction('Apply approved policy','fleet-deploy',id,true):'')+(p.targets.some(t=>t.status==='applied')?fleetAction('Prepare rollback proposal','fleet-rollback',id):'');
  if(p.status==='pending'&&p.author===state.session.user.id)html+='<p>A different administrator must sign in to review this proposal.</p>';
  html+='<p>Delivery is tracked per instance. A partial rollout stays visible and uncertain deliveries can be retried with the same operation identifier.</p>';
  modal('Policy review & delivery','Approval covers the immutable plans shown below.',html,null,null,actions);
 },
 async handle(action,id,el){
  if(!action.startsWith('fleet-'))return false;
  const f=state.fleet;
  if(action==='fleet-tab'){f.tab=id;renderView();}
  else if(action==='fleet-filter'){const form=new FormData(document.getElementById('fleet-filters'));const selected=form.get('member_id');f.team=selected===f.member?form.get('team_id'):'';f.member=selected;await PliqraFleet.load();renderView();}
  else if(action==='fleet-sync'){await api('/api/v1/fleet/sync',{method:'POST',body:'{}'});await changed('Member snapshots refreshed. Unavailable members remain visible.');}
  else if(action==='fleet-member-new')PliqraFleet.memberModal();
  else if(action==='fleet-member-edit')PliqraFleet.memberModal(id);
  else if(action==='fleet-trust-new')PliqraFleet.trustModal();
  else if(action==='fleet-revoke')modal('Revoke this controller pairing?','The controller will lose metadata and policy access to this workspace.','<p>Previously applied gateway policies stay in place and can be managed locally.</p>','Revoke pairing',async()=>{await api('/api/v1/fleet/trusts/'+id,{method:'DELETE'});await changed('Controller access revoked.');});
  else if(action==='fleet-member'){const m=f.members.find(m=>m.id===id),s=m.snapshot||{};modal(m.name,'Metadata snapshot · '+date(m.last_sync),`<p>${esc(m.endpoint)} · ${esc(s.version||'Unknown version')}</p>${m.last_error?'<div class="notice">'+esc(m.last_error)+'</div>':''}${eTable(['Service','Contract'],(s.services||[]).map(s=>`<tr><td>${esc(s.name)}</td><td><code>${esc(s.fingerprint.slice(0,16))}</code></td></tr>`))}${eTable(['Managed gateway','State'],(s.managed_gateways||[]).map(g=>`<tr><td>${esc(g.slug)}</td><td>${g.drifted?'Changed since approval':'Matches applied policy'}</td></tr>`))}`,null,null);}
  else if(action==='fleet-propose')PliqraFleet.policyModal();
  else if(action==='fleet-reuse')modal('Reuse an existing gateway','The source configuration becomes an independent immutable proposal.',selectField('Source gateway','gateway_id',(state.gateways||[]).map(g=>[g.id,g.name])),'Prepare policy',async form=>{const source=state.gateways.find(g=>g.id===form.get('gateway_id'));if(!source)throw new Error('Create a local gateway first or use New policy proposal.');const template=JSON.parse(JSON.stringify(source));const c=template.config;c.services=c.services.map((_,i)=>'service_'+(i+1));c.guardrails=c.guardrails.map((r,i)=>({...r,guardrail_id:'check_'+(i+1)}));c.project_id=c.project_id?'project':null;c.team_ids=c.team_ids.map((_,i)=>'team_'+(i+1));template.slug='fleet-'+template.slug.slice(0,56);PliqraFleet.policyModal(template);});
  else if(action==='fleet-add-rail')document.getElementById('fleet-rails').insertAdjacentHTML('beforeend',PliqraFleet.railRow());
  else if(action==='fleet-proposal')PliqraFleet.review(id);
  else if(action==='fleet-approve'||action==='fleet-reject'){const p=f.proposals.find(p=>p.id===id);await api('/api/v1/fleet/proposals/'+id+'/decision',{method:'POST',body:JSON.stringify({fingerprint:p.fingerprint,approve:action==='fleet-approve'})});await changed(action==='fleet-approve'?'Exact proposal approved.':'Proposal rejected.');PliqraFleet.review(id);}
  else if(action==='fleet-deploy'){await api('/api/v1/fleet/proposals/'+id+'/deploy',{method:'POST',body:'{}'});await changed('Delivery results recorded for every attempted target.');PliqraFleet.review(id);}
  else if(action==='fleet-rollback'){const result=await api('/api/v1/fleet/proposals/'+id+'/rollback-proposal',{method:'POST',body:'{}'});await changed('Rollback proposal prepared. A separate approval is required.');PliqraFleet.review(result.id);}
  else return false;
  return true;
 }
};
