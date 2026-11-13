"use strict";

window.PliqraOps = {
 plan: null,
 async load() {
  if (!isAdmin() || !['diagnostics','policy-files'].includes(state.view)) return;
  state.opsRuntime = await api('/api/v1/diagnostics/runtime');
  if (state.view === 'policy-files') state.opsGateways = await api('/api/v1/gateways');
 },
 views: {
  diagnostics() {return PliqraOps.diagnostics();},
  'policy-files'() {return PliqraOps.policies();}
 },
 diagnostics() {
  if (!isAdmin()) return head('Connection diagnosis','Administrator access is required.');
  const runtime = state.opsRuntime;
  return head('Connection diagnosis','Explain access, contracts, routing and available capacity without calling a model.') +
   `<section class="panel ops-panel"><form id="ops-diagnose"><div class="ops-fields">
   ${selectField('AI service','service',[['','Connection only'],...state.ports.map(p=>[p.name,p.name])])}
   ${selectField('Application identity','key_id',[['','My administrator identity'],...(state.keys||[]).map(k=>[k.id,k.name])])}
   ${selectField('Connection · optional','connection_id',[['','Eligible service deployment'],...state.conns.map(c=>[c.id,c.name])])}
   </div><details class="ops-advanced"><summary>Request shape and contract options</summary><div class="ops-fields">
   ${field('Gateway ID or address · optional','gateway_id','Use the key assignment by default')}
   ${field('Binding ID · optional','binding_id','Check an existing application contract')}
   ${field('Input characters · estimate only','input_chars','0','number','0','min=0 max=1000000 required')}
   ${field('Maximum output tokens','max_tokens','512','number','512','min=1 max=131072 required')}
   </div><fieldset class="ops-checks"><legend>Required capabilities</legend>${['chat','streaming','tools','vision','structured_output'].map(cap=>`<label class="check-row"><input type="checkbox" name="requirements" value="${cap}" ${cap==='chat'?'checked':''}>${esc(cap.replaceAll('_',' '))}</label>`).join('')}</fieldset>
   <label class="check-row"><input type="checkbox" name="stream" checked>Stream the response</label></details>
   <label class="check-row"><input type="checkbox" name="inspect_endpoint">Also request model-list metadata from the selected endpoint</label>
   <p class="muted">Default: no endpoint traffic and no quota reservation. Optional discovery sends a bounded GET with the stored provider credential; it does not verify inference or execute guardrails.</p>
   ${btn('Run diagnosis','ops-diagnose','activity','primary')}</form><div id="ops-diagnosis" aria-live="polite"></div></section>
   <section class="panel ops-panel section-gap"><h2>Tracing and sign-in protection</h2><p>${runtime?.telemetry.enabled?'OpenTelemetry is enabled.':'OpenTelemetry is disabled.'} OTLP HTTP/protobuf export is opt-in. Search your trace backend by <code>pliqra.request.id</code> to correlate a request. Prompt, response and credential content is excluded.</p><p>Sign-in admission is shared through ${esc(runtime?.login_protection.backend||'the workspace database')}: 15 attempts per client address in five minutes. ${num(runtime?.login_protection.trusted_proxy_networks)} explicitly trusted proxy networks.</p><p><a href="https://github.com/eturkok/pliqra/blob/main/docs/OPERATIONS_TOOLS.md" target="_blank" rel="noopener">Configuration and operational limits ↗</a></p></section>` + footer();
 },
 diagnosisReport(report) {
  return `<h2>${report.ready===null?'Configuration found; reachability not assessed':report.ready?'Checks passed for this snapshot':'Review required'}</h2><p class="muted">${esc(report.note)}</p>${report.checks.map(check=>`<article class="ops-check-result"><h3>${badge(check.passed?'success':'error',check.passed?'Passed':'Needs attention')} ${esc(check.phase.replaceAll('_',' '))}</h3><p>${esc(check.message||check.action||'')}</p>${check.category?`<p>Category: ${esc(check.category)}${check.http_status?` · HTTP ${check.http_status}`:''}</p>`:''}${check.action&&check.message?`<p>${esc(check.action)}</p>`:''}${check.routing?.length?`<ul>${check.routing.map(d=>`<li>${esc(d.model||'Deployment')} · ${esc(d.reason||'')}</li>`).join('')}</ul>`:''}${check.limits?.length?eTable(['Limit','Used','Proposed','Maximum','Result'],check.limits.map(q=>`<tr><td>${esc(q.name)}</td><td>${esc(q.used)}</td><td>${esc(q.proposed??'Unknown')}</td><td>${esc(q.limit)}</td><td>${esc(q.passed?'Available':q.reason||'Exceeded')}</td></tr>`)):''}</article>`).join('')}`;
 },
 policies() {
  if (!isAdmin()) return head('Policy files','Administrator access is required.');
  PliqraOps.plan = null;
  return head('Policy files','Keep gateway policies in Git. Review drift and application impact before an atomic apply.') +
   `<section class="panel ops-panel"><div class="ops-fields">${selectField('Start from an existing gateway','export_gateway',[['','Choose a gateway'],...(state.opsGateways||[]).map(g=>[g.id,g.name])])}<div class="actions">${btn('Load export','ops-export','download')}${btn('Load example','ops-example','code')}</div></div>
   <p class="muted">Credential-free JSON with explicit local mappings. Plans expire after ten minutes and are tied to your administrator account. Missing resources are never deleted. Fleet-managed gateways use their existing approval workflow.</p>
   <label for="ops-manifest">Policy manifest</label><textarea id="ops-manifest" class="mono ops-json" rows="15" spellcheck="false" placeholder='{"format":"pliqra/policies/1","gateways":[...],"mapping":{"services":{...}}}'>${esc(state.opsManifest||'')}</textarea>
   <div class="actions section-gap">${btn('Plan changes','ops-plan','activity','primary')}${btn('Download manifest','ops-download','download')}</div>
   <div id="ops-policy-report" aria-live="polite"></div></section>` + footer();
 },
 document(profile) {
  const c = profile.config;
  return {format:'pliqra/policies/1',gateways:[{name:profile.name,slug:profile.slug,config:c}],mapping:{services:Object.fromEntries(c.services.map(i=>[i,i])),guardrails:Object.fromEntries(c.guardrails.map(r=>[r.guardrail_id,r.guardrail_id])),projects:c.project_id?{[c.project_id]:c.project_id}:{},teams:Object.fromEntries(c.team_ids.map(i=>[i,i]))}};
 },
 download(value,name) {
  const url=URL.createObjectURL(new Blob([value],{type:'application/json'}));
  const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 },
 async impact(id,offset=0) {
  const report=await api(`/api/v1/behavior-checks/${encodeURIComponent(id)}/impact?offset=${offset}&limit=50`);
  const rows=report.items.map(row=>`<tr><td>${esc(row.application)}<small>${esc(row.project_id||'No project')}${row.team_id?' · '+esc(row.team_id):''}</small></td><td>${esc(row.service)}<small>${esc(row.requirements.join(', ')||'No explicit requirements')}</small></td><td>${esc(row.status.replaceAll('_',' '))}${row.already_stale?'<small>Existing binding already stale</small>':''}</td><td>${esc(row.alternatives.map(a=>a.model).join(', ')||'No eligible alternative')}</td></tr>`);
  const paging=`<div class="actions">${offset?btn('Previous','ops-impact','chevron','',`data-id="${esc(id)}" data-offset="${Math.max(0,offset-50)}"`):''}${offset+50<report.total?btn('Next','ops-impact','chevron','',`data-id="${esc(id)}" data-offset="${offset+50}"`):''}</div>`;
  modal('Application impact','Observed behavior → retained application contracts',`<p class="notice">${esc(report.note)}</p>${report.observation_state==='not_tested'?'<p class="notice">Run a first behavior-check cycle before assessing application impact.</p>':''}${report.observation_stale?'<p class="notice warning">The endpoint configuration changed after the last observation. Run a new cycle to assess current behavior.</p>':''}${report.scan_truncated?'<p class="notice warning">Scan limited to 10,000 recent bindings; this report is incomplete.</p>':''}<p>${report.total} retained bindings · ${report.summary.review_required} require review · ${report.summary.alternative_available} have configuration-eligible alternatives · ${report.excluded_inactive_or_unauthorized} inactive or unauthorized excluded</p>${eTable(['Application / scope','Service / requirements','Assessment','Configuration-eligible alternatives'],rows)||''}${!rows.length?'<p>No matching authorized bindings in this snapshot.</p>':''}${paging}`,null);
 },
 async handle(action,id,element) {
  if (!action.startsWith('ops-')) return false;
  if (!isAdmin()) throw new Error('Administrator access is required.');
  if (action==='ops-diagnose') {
   const form=document.getElementById('ops-diagnose');if(!form.reportValidity())return true;
   const values=new FormData(form), body={requirements:values.getAll('requirements'),stream:values.has('stream'),inspect_endpoint:values.has('inspect_endpoint'),input_chars:Number(values.get('input_chars')),max_tokens:Number(values.get('max_tokens'))};
   for(const key of ['service','key_id','connection_id','gateway_id','binding_id'])if(values.get(key)?.trim())body[key]=values.get(key).trim();
   const report=await api('/api/v1/diagnostics',{method:'POST',body:JSON.stringify(body)});
   document.getElementById('ops-diagnosis').innerHTML=PliqraOps.diagnosisReport(report);
  } else if(action==='ops-impact') await PliqraOps.impact(id,Number(element.dataset.offset||0));
  else if(action==='ops-export'||action==='ops-example') {
   let value;
   if(action==='ops-export') {
    const selected=document.getElementById('f-export_gateway').value;if(!selected)throw new Error('Choose a gateway first.');
    value=PliqraOps.document(await api(`/api/v1/gateways/${selected}/export`));
   } else value={format:'pliqra/policies/1',gateways:[{name:'Application gateway',slug:'application',config:{services:['chat'],max_concurrency:20,limits:[{metric:'requests',scope:'gateway',limit:100,window_seconds:60}]}}],mapping:{services:{chat:state.ports[0]?.id||'REPLACE_WITH_SERVICE_ID'}}};
   state.opsManifest=JSON.stringify(value,null,2);PliqraOps.plan=null;renderView();
  } else if(action==='ops-download') {
   const value=document.getElementById('ops-manifest').value;JSON.parse(value);PliqraOps.download(value,'pliqra-policies.json');
  } else if(action==='ops-plan') {
   const source=document.getElementById('ops-manifest').value;
   const report=await api('/api/v1/policies/plan',{method:'POST',body:JSON.stringify(JSON.parse(source))});
   PliqraOps.plan={...report,source};state.opsManifest=source;
   document.getElementById('ops-policy-report').innerHTML=`<h2>${report.has_changes?'Review proposed changes':'No configuration drift'}</h2><p>${esc(report.note)} Expires ${date(report.expires_at)}.</p>${report.changes.map(c=>`<article class="ops-check-result"><h3>${esc(c.slug)} · ${esc(c.action)}</h3><div class="ops-diff"><div><h4>Current</h4><pre>${esc(JSON.stringify(c.before,null,2))}</pre></div><div><h4>Proposed</h4><pre>${esc(JSON.stringify(c.after,null,2))}</pre></div></div>${c.impact?`<h4>Application impact</h4><pre>${esc(JSON.stringify(c.impact,null,2))}</pre>`:''}</article>`).join('')}${report.has_changes?`<label class="check-row"><input id="ops-approve" type="checkbox">I reviewed the complete changes and application impact.</label>${btn('Apply reviewed plan','ops-apply','check','primary','disabled')}`:''}${btn('Download reviewed plan','ops-plan-download','download')}`;
  } else if(action==='ops-plan-download') {
   if(!PliqraOps.plan)throw new Error('Create a current plan first.');
   const {source,...report}=PliqraOps.plan;PliqraOps.download(JSON.stringify(report,null,2),'pliqra-policy-plan.json');
  } else if(action==='ops-apply') {
   const report=PliqraOps.plan;
   if(!report||!document.getElementById('ops-approve')?.checked||document.getElementById('ops-manifest').value!==report.source)throw new Error('Review a fresh plan before applying.');
   if(Date.now()/1000>=report.expires_at)throw new Error('This plan expired. Create and review a new plan.');
   const result=await api('/api/v1/policies/apply',{method:'POST',body:JSON.stringify({plan:report.plan,approve:true})});
   PliqraOps.plan=null;document.getElementById('ops-policy-report').innerHTML=`<h2>Reviewed policy applied</h2><pre>${esc(JSON.stringify(result.changes,null,2))}</pre><p>Create another plan to check drift.</p>`;toast('Policy applied and audited.');
  }
  return true;
 }
};

document.addEventListener('input',event=>{
 if(event.target.id==='ops-manifest'){
  state.opsManifest=event.target.value;
  if(PliqraOps.plan){PliqraOps.plan=null;document.getElementById('ops-policy-report').innerHTML='<p class="notice">Manifest edited. Create and review a fresh plan.</p>';}
 }
 if(event.target.id==='ops-approve'){
  const button=document.querySelector('[data-action="ops-apply"]');if(button)button.disabled=!event.target.checked;
 }
});

document.addEventListener('submit',event=>{
 if(event.target.id==='ops-diagnose'){
  event.preventDefault();event.target.querySelector('[data-action="ops-diagnose"]').click();
 }
});
