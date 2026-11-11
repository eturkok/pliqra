"use strict";

window.PliqraFabric = {
 async load(){
  if(!isAdmin())return;
  state.gateways=await api('/api/v1/gateways');
  if(['gateways','guardrails'].includes(state.view)){
   [state.guardrails,state.teams,state.guardDecisions]=await Promise.all([api('/api/v1/guardrails'),api('/api/v1/teams'),api('/api/v1/guardrail-decisions')]);
  }
 },
 window(s){return ({60:'minute',3600:'hour',86400:'day',2592000:'30 days'})[s]||num(s)+' seconds';},
 check(name,label,value,checked){return '<label class="check-row"><input type="checkbox" name="'+esc(name)+'" value="'+esc(value)+'" '+(checked?'checked':'')+'>'+esc(label)+'</label>';},
 rowSelect(name,label,options,value){return '<label>'+esc(label)+'<select name="'+name+'">'+options.map(([v,l])=>'<option value="'+esc(v)+'" '+(v===value?'selected':'')+'>'+esc(l)+'</option>').join('')+'</select></label>';},
 rowNumber(name,label,value,max){return '<label>'+label+'<input type="number" name="'+name+'" value="'+value+'" required min="1" max="'+max+'"></label>';},
 playPath(){const g=(state.gateways||[]).find(v=>v.id===state.playGateway);return g?g.base_path+'/chat/completions':'/v1/chat/completions';},
 playFields(){
  const g=(state.gateways||[]).find(v=>v.id===state.playGateway),p=state.ports.find(v=>v.name===state.playPort);
  const project=g?.config.project_id||p?.project_id||state.playProject||'';
  return selectField('Gateway policy','play-gateway',[['','Standard service policies'],...(state.gateways||[]).map(v=>[v.id,v.name])],state.playGateway||'')+
   (g?.config.guardrails.some(v=>v.stages.includes('output'))?'<div class="notice">The answer is checked before it appears. First content may arrive later.</div>':'')+
   selectField('Project attribution','play-project',project&&g?.config.project_id?state.projects.filter(v=>v.id===project).map(v=>[v.id,v.name]):[['','Personal playground'],...state.projects.map(v=>[v.id,v.name])],project)+
   selectField('Team attribution','play-team',[['','Unattributed'],...(state.teams||[]).filter(t=>t.projects.some(v=>v.project_id===project)&&(!g?.config.team_ids.length||g.config.team_ids.includes(t.id))).map(t=>[t.id,t.name])],state.playTeam||g?.config.team_ids[0]||'');
 },
 gatewaysView(){
  if(!isAdmin())return head('Gateways','Gateway policies are managed by administrators.');
  let html=head('Gateways','Publish models as a service. Shape access, quotas and safety for each workload.',btn('Import definition','gw-import','code')+btn('Create gateway','gw-create','plus','primary'));
  html+='<section class="fabric-intro"><strong>Your endpoint. Your rules.</strong><p>Give each workload its own gateway address. Its policy follows assigned keys across every inference path.</p><div class="fabric-path"><span>Identity</span>'+icon('arrow')+'<span>Quotas</span>'+icon('arrow')+'<span>Input rails</span>'+icon('arrow')+'<span>Model</span>'+icon('arrow')+'<span>Output rails</span></div></section>';
  if(!state.gateways?.length)return html+empty('route','Create a gateway for your first workload','Connect services, add request and token quotas, then choose your guardrail chain.')+footer();
  return html+'<div class="gateway-grid">'+state.gateways.map(g=>{
   const attr='data-id="'+esc(g.id)+'"',c=g.config,s=g.last_24h;
   return '<section class="panel gateway-card"><div class="panel-head"><div><h2>'+esc(g.name)+'</h2><p class="mono">'+esc(g.base_path)+'</p></div>'+badge(c.enabled?'success':'disconnected',c.enabled?'Enabled':'Disabled')+'</div><div class="gateway-card-body"><div class="gateway-facts"><span><strong>'+c.services.length+'</strong> services</span><span><strong>'+c.limits.length+'</strong> quota rules</span><span><strong>'+c.guardrails.length+'</strong> guardrails</span></div><div class="gateway-rules">'+(c.limits.map(r=>'<span>'+num(r.limit)+' '+esc(r.metric)+' / '+PliqraFabric.window(r.window_seconds)+' · '+esc(r.scope)+'</span>').join('')||'<span>Key and project limits apply.</span>')+'</div><p class="muted">Last 24 hours · '+num(s.requests)+' admitted · '+num(s.tokens)+' charged/reserved tokens · '+s.blocked+' blocked · '+s.active+' active</p><div class="gateway-contract"><span>v'+g.version+'</span><code>'+esc(g.fingerprint.slice(0,16))+'…</code><span>'+(c.guardrails.some(r=>r.stages.includes('output'))?'Output checked before delivery':'Live streaming available')+'</span></div><div class="actions">'+btn('Edit policy','gw-edit','settings','',attr)+btn('Issue key','gw-key','key','primary',attr)+btn('Preview','gw-preview','search','',attr)+btn('Try','gw-try','chat','',attr)+'</div><div class="actions secondary-actions">'+btn('Usage','gw-usage','chart','ghost',attr)+btn('Versions','gw-versions','history','ghost',attr)+btn('Clone','gw-clone','copy','ghost',attr)+btn('Export','gw-export','code','ghost',attr)+'</div></div></section>';
  }).join('')+'</div>'+footer();
 },
 guardrailsView(){
  if(!isAdmin())return '';
  let html=head('Guardrails','Reusable checks before inference and before an answer reaches an application.',btn('Add guardrail','gw-rail-create','plus','primary'));
  html+='<div class="notice">'+icon('shield')+'Local phrase, email/phone and JSON Schema rules run here. NeMo and custom HTTP checks run at endpoints you configure. They receive the content selected for checking.</div><section class="panel section-gap"><div class="panel-head"><h2>Guardrail library</h2><span class="pill">Reusable across gateways</span></div>';
  if(state.guardrails?.length)html+='<div class="table-wrap"><table><thead><tr><th>Check</th><th>Provider</th><th>Rule</th><th>Status</th><th></th></tr></thead><tbody>'+state.guardrails.map(r=>'<tr><td><strong>'+esc(r.name)+'</strong><div class="cell-sub">v'+r.version+'</div></td><td>'+esc({builtin:'Built in',nemo:'NVIDIA NeMo',webhook:'Custom HTTP'}[r.provider])+'</td><td>'+esc(r.provider==='builtin'?r.config.rule.replaceAll('_',' '):r.config.config_id||'Decision webhook')+'</td><td>'+badge(r.enabled?'success':'disconnected',r.enabled?'Enabled':'Unavailable')+'</td><td>'+btn('Edit','gw-rail-edit','edit','','data-id="'+r.id+'"')+btn('Test','gw-rail-test','bolt','','data-id="'+r.id+'"')+'</td></tr>').join('')+'</tbody></table></div>';
  else html+=empty('shield','Your checks, in one library','Add a local rule, connect a NeMo check endpoint or bring your own HTTP validator.');
  return html+'</section><section class="panel section-gap"><div class="panel-head"><div><h2>Recent guardrail decisions</h2><p>Verdicts and timing. Checked content and provider response text are excluded.</p></div></div>'+PliqraFabric.decisionsTable(state.guardDecisions||[])+'</section>'+footer();
 },
 quotaRow(r={metric:'requests',scope:'gateway',window_seconds:60,limit:60}){
  return '<div class="quota-row fabric-row">'+PliqraFabric.rowSelect('quota_metric','Metric',[['requests','Requests'],['tokens','Tokens']],r.metric)+PliqraFabric.rowSelect('quota_scope','Scope',['gateway','key','user','project','team','service'].map(v=>[v,v]),r.scope)+PliqraFabric.rowNumber('quota_window','Window · seconds',r.window_seconds,2592000)+PliqraFabric.rowNumber('quota_limit','Limit',r.limit,1e12)+ibtn('Remove quota','gw-remove-row','close')+'</div>';
 },
 stepRow(s={stages:['input'],mode:'enforce',on_error:'block'}){
  return '<div class="rail-step fabric-row">'+PliqraFabric.rowSelect('rail_id','Guardrail',[['','Select a check'],...(state.guardrails||[]).map(r=>[r.id,r.name])],s.guardrail_id)+PliqraFabric.rowSelect('rail_stage','Stage',[['input','Input'],['output','Output'],['both','Input + output']],s.stages.length===2?'both':s.stages[0])+PliqraFabric.rowSelect('rail_mode','Mode',[['enforce','Enforce'],['observe','Observe only']],s.mode)+PliqraFabric.rowSelect('rail_error','On failure',[['block','Stop the request'],['allow','Allow and record']],s.on_error)+ibtn('Remove guardrail step','gw-remove-row','close')+'</div>';
 },
 gatewayModal(id,imported,clone=false){
  const g=imported||(state.gateways||[]).find(v=>v.id===id),c=g?.config||{},f=PliqraFabric;
  let html=imported?'<div class="notice">Map imported references to local services and checks before publishing. Every guardrail step must have a local check selected.</div>':'';
  html+='<div class="field-inline">'+field('Gateway name','name','e.g. Finance gateway','text',g?(g.name+(clone?' copy':'')):'','required maxlength="100"')+field('Address name','slug','e.g. finance','text',g?(g.slug+(clone?'-copy':'')):'','required pattern="[a-z0-9][a-z0-9-]{0,62}"')+'</div>'+selectField('Project restriction','project_id',[['','Use the accessible caller project'],...state.projects.map(p=>[p.id,p.name])],c.project_id||'');
  html+='<div class="field"><label>Published services</label><div class="check-list">'+state.ports.map(p=>f.check('services',p.name,p.id,c.services?.includes(p.id))).join('')+'</div></div><details class="journey-advanced"><summary>Restrict to teams · optional</summary><p class="muted">Select a project first. Every selected team must belong to that project.</p><div class="check-list">'+(state.teams||[]).map(t=>f.check('team_ids',t.name,t.id,c.team_ids?.includes(t.id))).join('')+'</div></details>';
  html+='<div class="fabric-editor-head"><h3>Request & token quotas</h3>'+btn('Add quota','gw-add-quota','plus')+'</div><p class="muted">Combine rolling windows and scopes. All rules, key limits and project budgets apply together.</p><div id="quota-rows">'+(c.limits||[{metric:'requests',scope:'gateway',window_seconds:60,limit:120},{metric:'tokens',scope:'key',window_seconds:60,limit:100000}]).map(f.quotaRow).join('')+'</div>';
  html+='<div class="fabric-editor-head"><h3>Guardrail chain</h3>'+btn('Add check','gw-add-step','plus')+'</div><p class="muted">Checks run in order at each stage. Output checks buffer the full answer before delivery, including SSE clients.</p><div id="rail-rows">'+(c.guardrails||[]).map(f.stepRow).join('')+'</div>';
  html+='<details class="journey-advanced"><summary>Inference controls</summary><div class="field-inline">'+field('Concurrent requests','max_concurrency','','number',c.max_concurrency??20,'required min="1" max="200"')+field('Maximum output tokens','max_output_tokens','','number',c.max_output_tokens??4096,'required min="1" max="131072"')+'</div>'+field('Maximum input characters','max_input_chars','','number',c.max_input_chars??200000,'required min="1" max="1000000"')+f.check('require_verified','Route only to verified capabilities','on',c.require_verified)+f.check('allow_tools','Allow tool calling','on',c.allow_tools??true)+f.check('allow_vision','Allow image inputs','on',c.allow_vision??true)+f.check('enabled','Gateway enabled','on',c.enabled??true)+'</details><div class="notice">'+icon('info')+'Token admission reserves UTF-8 input bytes plus requested output for every possible attempt. Reported usage settles it. Unknown usage stays reserved. This is an estimate, not a model tokenizer.</div>';
  let impactPreview=null;
  modal(id&&!clone?'Edit gateway policy':'Create gateway','A new version requires bound applications to accept the new policy.',html,id&&!clone?'Preview impact':'Create gateway',async(form,dialog)=>{
   const metrics=form.getAll('quota_metric'),scopes=form.getAll('quota_scope'),windows=form.getAll('quota_window'),limits=form.getAll('quota_limit'),rails=form.getAll('rail_id'),stages=form.getAll('rail_stage'),modes=form.getAll('rail_mode'),errors=form.getAll('rail_error');
   if(rails.some(v=>!v))throw new Error('Select a local check for every guardrail step.');
   const config={services:form.getAll('services'),project_id:form.get('project_id')||null,team_ids:form.getAll('team_ids'),limits:metrics.map((metric,i)=>({metric,scope:scopes[i],window_seconds:Number(windows[i]),limit:Number(limits[i])})),guardrails:rails.map((guardrail_id,i)=>({guardrail_id,stages:stages[i]==='both'?['input','output']:[stages[i]],mode:modes[i],on_error:errors[i]})),max_concurrency:Number(form.get('max_concurrency')),max_input_chars:Number(form.get('max_input_chars')),max_output_tokens:Number(form.get('max_output_tokens')),require_verified:form.has('require_verified'),allow_tools:form.has('allow_tools'),allow_vision:form.has('allow_vision'),enabled:form.has('enabled')};
   const document=JSON.stringify({name:form.get('name'),slug:form.get('slug'),config});
   if(id&&!clone&&impactPreview?.body!==document){
    const result=await api('/api/v1/gateways/'+id+'/impact',{method:'POST',body:document});
    impactPreview={body:document,fingerprint:result.current_fingerprint};
    let target=dialog.querySelector('[data-impact]');if(!target){target=window.document.createElement('div');target.dataset.impact='';dialog.querySelector('.modal-content').append(target);}
    mountPublicationImpact(target,result,offset=>api(`/api/v1/gateways/${id}/impact?offset=${offset}`,{method:'POST',body:document}));setTimeout(()=>{dialog.querySelector('#modal-submit').textContent='Publish policy';},0);return;
   }
   try {await api(id&&!clone?'/api/v1/gateways/'+id:'/api/v1/gateways',{method:id&&!clone?'PUT':'POST',headers:impactPreview?{'If-Match':'"'+impactPreview.fingerprint+'"'}:{},body:document});}
   catch(error){impactPreview=null;throw error;}
   state.reportChoices=null;await changed('Gateway policy published.');
  });
 },
 railModal(id){
  const r=(state.guardrails||[]).find(v=>v.id===id),c=r?.config||{},f=PliqraFabric;
  let html=field('Guardrail name','name','e.g. Customer data protection','text',r?.name||'','required maxlength="100"')+selectField('Provider','rail_provider',[['builtin','Built-in rules'],['nemo','NVIDIA NeMo check API'],['webhook','Custom HTTP decision API']],r?.provider||'builtin');
  html+='<div data-rail-local>'+selectField('Rule','rail_rule',[['keywords','Keywords & phrases'],['pii','Email / phone patterns'],['json_schema','Output JSON Schema']],c.rule||'keywords')+'<div data-rail-terms class="field"><label for="rail-terms">Phrases · one per line</label><textarea id="rail-terms" name="terms" rows="3">'+esc((c.terms||[]).join('\n'))+'</textarea></div><div data-rail-pii>'+f.check('pii_types','Email addresses','email',(c.pii_types||['email']).includes('email'))+f.check('pii_types','Phone-like numbers','phone',c.pii_types?.includes('phone'))+'<p class="muted">Approximate patterns do not detect every form of personal data.</p></div><div data-rail-schema class="field"><label for="rail-schema">JSON Schema</label><textarea id="rail-schema" name="json_schema" rows="5">'+esc(c.json_schema?JSON.stringify(c.json_schema,null,2):'{"type":"object"}')+'</textarea></div>'+'<div data-rail-action>'+selectField('When matched','rail_action',[['block','Block'],['redact','Replace matched text']],c.action||'block')+'</div></div>';
  html+='<div data-rail-remote>'+field('Full check endpoint','endpoint','https://guardrails.example.com/v1/checks','url',c.endpoint||'')+'<div data-rail-nemo>'+field('NeMo configuration ID','config_id','e.g. company-policy','text',c.config_id||'')+'<p class="muted">Requires POST /v1/checks. Accepts passed / blocked decisions. Modified or incompatible responses follow the failure policy.</p></div><div data-rail-webhook class="notice">Receives stage and messages. Return {"allow":true} or {"allow":false}.</div>'+field('API key · blank keeps existing','api_key','','password','','autocomplete="off"')+f.check('clear_key','Remove stored API key','on',false)+'<div class="field-inline">'+field('Authentication header','auth_header','','text',c.auth_header||'Authorization')+field('Prefix · blank for raw key','auth_prefix','','text',c.auth_prefix??'Bearer')+'</div>'+field('Deadline · seconds','timeout_seconds','','number',c.timeout_seconds??10,'min="0.1" max="60" step="0.1"')+'</div>'+f.check('rail_enabled','Provider enabled','on',r?.enabled!==0)+'<p class="muted">An unavailable provider follows the gateway failure policy. Keep Stop the request for fail-closed enforcement.</p>';
  modal(r?'Edit guardrail':'Add guardrail','Reusable across gateways. Changes invalidate their bound contracts.',html,'Save guardrail',async form=>{
   const provider=form.get('rail_provider'),rule=form.get('rail_rule')||c.rule||'keywords';
   const config={rule,terms:String(form.get('terms')||'').split('\n').map(v=>v.trim()).filter(Boolean),pii_types:form.getAll('pii_types'),action:form.get('rail_action')||'block',json_schema:rule==='json_schema'&&provider==='builtin'?JSON.parse(form.get('json_schema')):null,endpoint:form.get('endpoint')||'',config_id:form.get('config_id')||'',auth_header:form.get('auth_header')||'Authorization',auth_prefix:form.get('auth_prefix')??'Bearer',timeout_seconds:Number(form.get('timeout_seconds')||10)};
   await api(r?'/api/v1/guardrails/'+r.id:'/api/v1/guardrails',{method:r?'PUT':'POST',body:JSON.stringify({name:form.get('name'),provider,config,enabled:form.has('rail_enabled'),api_key:form.has('clear_key')?'':form.get('api_key')||null})});await changed('Guardrail saved.');
  });f.railVisibility();
 },
 railVisibility(){
  const provider=document.getElementById('f-rail_provider')?.value,rule=document.getElementById('f-rail_rule')?.value;
  const show=(s,on)=>document.querySelectorAll(s).forEach(el=>{el.hidden=!on;el.querySelectorAll('input,select,textarea').forEach(v=>v.disabled=!on);});
  show('[data-rail-local]',provider==='builtin');show('[data-rail-remote]',provider!=='builtin');show('[data-rail-nemo]',provider==='nemo');show('[data-rail-webhook]',provider==='webhook');show('[data-rail-terms]',provider==='builtin'&&rule==='keywords');show('[data-rail-pii]',provider==='builtin'&&rule==='pii');show('[data-rail-schema]',provider==='builtin'&&rule==='json_schema');show('[data-rail-action]',provider==='builtin'&&rule!=='json_schema');
 },
 keyModal(id){
  const g=state.gateways.find(v=>v.id===id),c=g.config;
  modal('Issue a gateway key',g.name+' · policy follows this key across every inference path.',field('Application name','name','e.g. Finance assistant','text','','required')+selectField('Project','project_id',state.projects.filter(p=>!c.project_id||p.id===c.project_id).map(p=>[p.id,p.name]),c.project_id||'')+selectField('Team attribution','team_id',[...(!c.team_ids.length?[['','Unattributed']]:[]),...(state.teams||[]).filter(t=>!c.team_ids.length||c.team_ids.includes(t.id)).map(t=>[t.id,t.name])],c.team_ids[0]||'')+'<div class="field-inline">'+field('Key requests per minute','rpm','','number',60,'required min="1" max="100000"')+field('Expires in days','expires_days','','number',90,'required min="1" max="365"')+'</div><p class="muted">Restricted to this gateway and its currently published services. Gateway quotas and project budgets also apply.</p>','Create key',async form=>{
   const result=await api('/api/v1/keys',{method:'POST',body:JSON.stringify({name:form.get('name'),project_id:form.get('project_id'),team_id:form.get('team_id')||null,gateway_id:g.id,allowed_ports:c.services,rpm:Number(form.get('rpm')),expires_days:Number(form.get('expires_days'))})});
   await refreshData();renderView();modal('Gateway key ready','Store this key now. It is shown once.','<div class="key-box mono">'+esc(result.token)+'</div><p class="journey-copy">OpenAI-compatible base URL</p><pre>'+esc(location.origin+BASE+g.base_path)+'</pre><p class="muted">The same gateway policy applies when this key uses the standard Pliqra API.</p>','Copy key',async()=>{await copy(result.token);toast('Key copied.');});
  });
 },
 previewModal(id){
  const g=state.gateways.find(v=>v.id===id);
  modal('Preview gateway policy','No inference or external check is sent.',selectField('Service','service',state.ports.filter(p=>g.config.services.includes(p.id)).map(p=>[p.name,p.name]))+field('Maximum output tokens','max_tokens','','number',Math.min(512,g.config.max_output_tokens),'required min="1"')+selectField('Team attribution','team_id',[['','Default attribution'],...(state.teams||[]).filter(t=>!g.config.team_ids.length||g.config.team_ids.includes(t.id)).map(t=>[t.id,t.name])])+'<div class="field"><label for="preview-input">Sample input for size estimation</label><textarea id="preview-input" name="input_text" rows="3">Synthetic policy preview</textarea></div>'+PliqraFabric.check('stream','SSE response requested','on',true),'Evaluate policy',async form=>{
   const r=await api('/api/v1/gateways/'+id+'/preview',{method:'POST',body:JSON.stringify({service:form.get('service'),max_tokens:Number(form.get('max_tokens')),team_id:form.get('team_id')||null,input_text:form.get('input_text'),stream:form.has('stream')})});
   modal('Policy preview',r.admissible?'Admissible for this administrator preview':'Not currently admissible','<div class="notice">'+esc(r.note)+'</div><div class="setting-row"><span>Delivery</span><strong>'+esc(r.delivery.replaceAll('_',' '))+'</strong></div>'+r.limits.map(v=>'<div class="setting-row"><span>'+esc(v.scope)+' · '+esc(v.metric)+' / '+PliqraFabric.window(v.window_seconds)+'</span><strong>'+num(v.used)+' + '+num(v.proposed)+' / '+num(v.limit)+'</strong></div>').join('')+'<p class="journey-copy">'+r.guardrails.length+' configured checks. Test their decisions explicitly from Guardrails.</p><pre>'+esc(JSON.stringify(r.routing,null,2))+'</pre>','Done',async()=>document.getElementById('modal').close());
  });
 },
 decisionsTable(rows){
  return rows.length?'<div class="table-wrap guard-decisions"><table><thead><tr><th>Check</th><th>Stage</th><th>Verdict</th><th>Policy</th><th>Time</th><th></th></tr></thead><tbody>'+rows.map(r=>'<tr><td><strong>'+esc(r.name)+'</strong></td><td>'+esc(r.stage)+'</td><td><span class="pill">'+esc(r.outcome)+'</span></td><td>'+esc(r.mode)+' · error: '+esc(r.on_error)+'</td><td>'+ms(r.latency_ms)+'</td><td>'+(r.event_id?.startsWith('req_')?btn('Request','request-detail','arrow','ghost','data-id="'+esc(r.event_id)+'"'):'Operator test')+'</td></tr>').join('')+'</tbody></table></div>':empty('shield','No decisions yet','Checks appear after a gateway request or an explicit test.');
 },
 requestDetails(r){
  return (r.gateway_id?'<h3 class="section-gap">Gateway policy</h3><div class="setting-row"><span>'+esc(r.gateway_name)+'</span><strong>'+esc(r.delivery||'not invoked')+'</strong></div>'+(r.gateway_fingerprint?'<p class="muted mono">'+esc(r.gateway_fingerprint)+'</p>':''):'')+(r.guardrails?.length?'<h3 class="section-gap">Guardrail decisions</h3>'+PliqraFabric.decisionsTable(r.guardrails):'');
 },
 async handle(action,id,el){
  if(!action.startsWith('gw-'))return false;
  const f=PliqraFabric;
  if(action==='gw-create')f.gatewayModal();
  else if(action==='gw-edit')f.gatewayModal(id);
  else if(action==='gw-clone')f.gatewayModal(id,null,true);
  else if(action==='gw-rail-create')f.railModal();
  else if(action==='gw-rail-edit')f.railModal(id);
  else if(action==='gw-key')f.keyModal(id);
  else if(action==='gw-preview')f.previewModal(id);
  else if(action==='gw-try'){const g=state.gateways.find(v=>v.id===id);state.playGateway=id;state.playPort=state.ports.find(p=>g.config.services.includes(p.id))?.name;state.playProject=g.config.project_id||'';state.playTeam=g.config.team_ids[0]||'';state.chat=[];state.lastRequest=null;await navigate('playground');}
  else if(action==='gw-add-quota')document.getElementById('quota-rows').insertAdjacentHTML('beforeend',f.quotaRow());
  else if(action==='gw-add-step')document.getElementById('rail-rows').insertAdjacentHTML('beforeend',f.stepRow());
  else if(action==='gw-remove-row')el.closest('.fabric-row').remove();
  else if(action==='gw-usage'){state.reportFilters={gateway_id:id};state.reportChoices=null;await navigate('usage');}
  else if(action==='gw-export'){const doc=await api('/api/v1/gateways/'+id+'/export');modal('Gateway definition','No credentials included. Map local references before using another instance.','<pre>'+esc(JSON.stringify(doc,null,2))+'</pre>','Copy definition',async()=>{await copy(JSON.stringify(doc,null,2));toast('Definition copied.');});}
  else if(action==='gw-import')modal('Import gateway definition','Map references in the editor before publishing.','<div class="field"><label for="gateway-import">Gateway JSON</label><textarea id="gateway-import" name="document" rows="10" required></textarea></div>','Open policy editor',async form=>{const doc=JSON.parse(form.get('document'));if(doc.schema!=='pliqra/gateway/1'||!doc.config)throw new Error('Unsupported gateway definition.');f.gatewayModal(null,doc);});
  else if(action==='gw-versions'){const rows=await api('/api/v1/gateways/'+id+'/versions');modal('Gateway history','Restoring publishes a new version. Bound clients must reconnect.','<div class="settings-list">'+rows.map(r=>'<div class="setting-row"><span>Version '+r.version+'<small>'+date(r.created)+'</small></span>'+btn('Restore','gw-restore','history','','data-id="'+esc(id)+'" data-version="'+r.version+'"')+'</div>').join('')+'</div>',null,null);}
  else if(action==='gw-restore'){await api('/api/v1/gateways/'+id+'/rollback/'+el.dataset.version,{method:'POST',body:'{}'});document.getElementById('modal').close();await changed('Gateway version restored.');}
  else if(action==='gw-rail-test'){const r=state.guardrails.find(v=>v.id===id);modal('Test guardrail',r.name+' · explicitly calls this provider and may incur charges.',selectField('Stage','stage',[['input','Input'],['output','Output']],r.config.rule==='json_schema'?'output':'input')+'<div class="field"><label for="rail-test-text">Test content</label><textarea id="rail-test-text" name="text" required rows="5">Synthetic guardrail test</textarea></div>','Run check',async form=>{const result=await api('/api/v1/guardrails/'+id+'/test',{method:'POST',body:JSON.stringify(Object.fromEntries(form))});await refreshData();renderView();modal('Guardrail result',result.outcome,'<p class="journey-copy">'+esc(result.name)+' · '+esc(result.stage)+' · '+ms(result.latency_ms)+'</p><p class="muted">Observed the verdict without enforcing it. Pliqra performed no model inference.</p>','Done',async()=>document.getElementById('modal').close());});}
  else return false;
  return true;
 }
};
document.addEventListener('change',event=>{if(['f-rail_provider','f-rail_rule'].includes(event.target.id))PliqraFabric.railVisibility();});

document.addEventListener('change',event=>{if(event.target.id!=='f-play-gateway')return;state.playGateway=event.target.value;const g=(state.gateways||[]).find(v=>v.id===state.playGateway);state.playProject=g?.config.project_id||'';state.playTeam=g?.config.team_ids[0]||'';renderView();});
