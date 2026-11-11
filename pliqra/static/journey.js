"use strict";

window.PliqraJourney = {
 async load() {
  const selected = state.journeyService || state.ports.find(p => p.name === state.playPort)?.id || '';
  try { state.journey = await api('/api/v1/getting-started' + (selected ? '?service_id=' + encodeURIComponent(selected) : '')); }
  catch (error) {
   if (!selected || state.ports.some(p => p.id === selected)) throw error;
   state.journeyService = '';
   state.journey = await api('/api/v1/getting-started');
  }
 },
 overview() {
  const j = state.journey || {}, service = j.service;
  const tested = !!j.has_successful_call, bound = j.current_bindings > 0;
  const next = !service ? 'connect' : !tested ? 'j-test' : 'j-bind';
  const label = !service ? 'Connect your endpoint' : !tested ? 'Make your first call' : bound ? 'View your service' : 'Connect your application';
  const action = bound ? 'j-service' : next;
  const steps = [
   {done:!!service, title:'Connect an endpoint', text:service?(j.ready?'Models discovered. A stable service is ready.':'Models discovered. Check service readiness before making a call.'):'Bring a running model endpoint. Pliqra discovers the models and creates your services.', action:'connect', label:service?'Add another endpoint':'Connect endpoint', disabled:!isAdmin()},
   {done:tested, title:'Make the first call', text:tested?'A successful real inference call is recorded.':'Try your service in the playground. See the response and inspect its route.', action:'j-test', label:tested?'Open playground':'Try the service', disabled:!service||!j.ready},
   {done:bound, title:'Connect your application', text:bound?'A current application contract is active.':'Choose the capabilities your app needs. Get a scoped key and a bound invocation.', action:'j-bind', label:bound?'Connect another app':'Connect application', disabled:!service||!j.ready}
  ];
  const notices = (j.needs_rebinding || j.changes?.length) ? `<section class="journey-alert" role="status">${icon('info')}<div><strong>${j.needs_rebinding ? num(j.needs_rebinding)+' application connection'+(j.needs_rebinding===1?' needs':'s need')+' renewal' : 'Advertised model inventory changed'}</strong><p>${j.needs_rebinding?'The current service contract differs from the one these applications accepted. Existing stale invocations return HTTP 409.':''}</p>${(j.changes||[]).map(c=>`<p>${esc(c.message)}</p>`).join('')}</div>${btn('Review contract','j-contract','code')}</section>` : '';
  const options = state.ports.map(p => [p.id,p.name]);
  const evidence = service ? `<section class="panel journey-contract"><div class="panel-head"><div><h2>Your connection agreement</h2><p>Declared capabilities and observed evidence stay separate.</p></div>${btn('Inspect contract','j-contract','code')}</div><div class="journey-contract-body"><div class="journey-contract-meta"><strong>${esc(service.name)} <span class="chip">v${service.version}</span></strong><span>${badge(j.ready?'success':'configured',j.ready?'Ready':'Unavailable')}</span><code title="${esc(service.fingerprint)}">${esc(service.fingerprint.slice(0,16))}…</code></div><div class="journey-evidence">${(j.evidence||[]).map(e=>`<div><strong>${esc(e.model)}</strong><span>Declared: ${e.declared.map(v=>esc(v.replaceAll('_',' '))).join(', ')||'None'}</span><span>Verified: ${e.verified.map(v=>esc(v.replaceAll('_',' '))).join(', ')||'No evidence yet'}</span></div>`).join('')}</div><p class="journey-limit">Pliqra detects service policy and advertised metadata changes. An unchanged endpoint cannot reveal undisclosed model-weight changes.</p></div></section>` : '';
  const activity = (tested || state.events.total || Object.values(state.reportFilters||{}).some(Boolean)) ? `<div class="journey-report-header"><h2>Workspace activity</h2>${period()}</div>${PliqraUI.filterBar()}${overviewActivity()}` : `<div class="journey-empty-note">${icon('shield')} Requests, usage and routing decisions appear here after your first call. Advanced workspace controls are available in the sidebar.</div>${footer()}`;
  return head('Overview','Know what you connect to. See when the agreement changes.') + notices + `<section class="hero journey-hero"><div><div class="eyebrow"><span class="dot"></span>Connect. Verify. Stay informed.</div><h2>Know what you’re connecting to.</h2><p>Discover your model’s capabilities, test what works, and keep your application connected to an explicit contract.</p>${btn(label,action,!service?'plus':!tested?'chat':'plug','light',service&&!j.ready?'disabled':!service&&!isAdmin()?'disabled':'')}</div>${heroArt()}</section><section class="journey"><div class="journey-heading"><div><h2>From endpoint to application</h2><p>Three steps. Everything else can wait.</p></div>${options.length?selectField('Service','journey-service',options,service?.id||''):''}</div><ol class="journey-steps">${steps.map((s,i)=>`<li class="${s.done?'is-done':''}"><span class="journey-step-number">${s.done?icon('check'):i+1}</span><h3>${s.title}</h3><p>${s.text}</p>${btn(s.label,s.action,null,i===steps.findIndex(v=>!v.done)?'primary':'',s.disabled?'disabled':'')}<span class="journey-step-state">${s.done?'Complete':'Step '+(i+1)}</span></li>`).join('')}</ol></section>${evidence}${window.PliqraBehavior?PliqraBehavior.overview():''}${activity}`;
 },
 selected() { return state.ports.find(p=>p.id===state.journey?.service?.id); },
 async handle(action) {
  if (!action.startsWith('j-')) return false;
  const service = PliqraJourney.selected();
  if (action==='j-test') { if(!service) return true; state.playPort=service.name; state.chat=[];state.lastRequest=null;await navigate('playground'); }
  else if(action==='j-bind') { if(service) PliqraUI.bindModal(service.id); }
  else if(action==='j-contract') { if(service){ const contract=await api(`/api/v1/ports/${service.id}/manifest`);modal('Your service contract','Compare declared capabilities, verified evidence and the current fingerprint.',`<pre>${esc(JSON.stringify(contract,null,2))}</pre>`,null,null); } }
  else if(action==='j-service') await navigate('services');
  else return false;
  return true;
 },
 bindModal(id) {
  const p=state.ports.find(v=>v.id===id), caps={chat:'Chat',streaming:'Streaming',tools:'Function calling',structured_output:'JSON Schema output',vision:'Image inputs',embeddings:'Embeddings',audio_speech:'Text to speech',audio_transcription:'Audio to text'};
  const fields=isAdmin()?field('Application name','name','e.g. Invoice application','text',p.name+' application','required maxlength="100"')+(p.project_id?`<p class="muted">Project: ${esc(state.projects.find(v=>v.id===p.project_id)?.name||p.project_id)}</p>`:selectField('Project','project_id',state.projects.map(v=>[v.id,v.name]))):'<p>Use the application key supplied by your workspace administrator.</p>';
  const tokenField=field('Existing application key','token','plq_…','password','','autocomplete="off" '+(isAdmin()?'':'required'));
  modal('Connect your application',p.name+' · a capability contract your app can rely on',`${fields}${isAdmin()?`<div class="notice">${icon('key')} Pliqra creates a key restricted to this service: 60 requests/minute, valid for 90 days. Copy it once after connecting.</div><details class="journey-advanced"><summary>Use an existing key instead</summary>${tokenField}</details>`:tokenField}<details class="journey-advanced"><summary>Capabilities and contract lifetime</summary><div class="check-list">${Object.entries(caps).map(([v,label])=>`<label class="check-row"><input type="checkbox" name="requirements" value="${v}" ${(v==='chat'&&!p.requirements.some(r=>['embeddings','audio_speech','audio_transcription'].includes(r)))||p.requirements.includes(v)?'checked':''}>${label}</label>`).join('')}</div>${field('Required extensions · comma separated','required_extensions','pliqra.prompts/1, pliqra.batches/1')}${field('Optional extensions · comma separated','optional_extensions','')}${field('Binding lifetime · seconds','ttl_seconds','','number',3600,'required min="60" max="86400"')}</details><div class="notice">${icon('shield')} Policy and advertised model metadata changes invalidate the old contract. Your app must explicitly connect again.</div>`,'Connect application',async form=>{
   const token=String(form.get('token')||'').trim();let binding,key=null,keyExpires=null;
   const request={service:p.name,requirements:form.getAll('requirements'),ttl_seconds:Number(form.get('ttl_seconds')),required_extensions:String(form.get('required_extensions')||'').split(',').map(v=>v.trim()).filter(Boolean),optional_extensions:String(form.get('optional_extensions')||'').split(',').map(v=>v.trim()).filter(Boolean)};
   if(token) binding=await api('/protocol/v1/bindings',{method:'POST',headers:{Authorization:'Bearer '+token},body:JSON.stringify(request)});
   else { const result=await api('/api/v1/application-connections',{method:'POST',body:JSON.stringify({...request,name:form.get('name'),project_id:p.project_id||form.get('project_id')||null})});binding=result.binding;key=result.key;keyExpires=result.key_expires; }
   await refreshData();renderView();
   const operation=request.requirements.includes('embeddings')?'embeddings':request.requirements.includes('audio_speech')?'speech':request.requirements.includes('audio_transcription')?'transcription':null;
   const example=operation==='embeddings'?{input:'Hello'}:operation==='speech'?{input:'Hello',voice:'alloy'}:operation==='transcription'?{audio_base64:'BASE64_AUDIO',filename:'recording.wav'}:{messages:[{role:'user',content:'Hello'}],stream:request.requirements.includes('streaming')};
   const invocationPath=operation?binding.operation_invocations[operation]:'/v1/chat/completions';
   const command=`curl ${location.origin}${BASE}${invocationPath} \\
  -H "Authorization: Bearer $PLIQRA_API_KEY" \\
${operation?'':'  -H "X-Pliqra-Binding: '+binding.id+'" \\\n'}  -H "Content-Type: application/json" \\
  -d '${JSON.stringify({model:p.name,...example})}'`;
   modal('Your application is connected',p.name+' · v'+binding.version+' · contract expires '+date(binding.expires),`${key?`<div class="notice">${icon('key')} Save this key now. It is shown once and expires ${date(keyExpires)}.</div><div class="key-box mono">${esc(key.token)}</div>`:''}<p class="journey-copy">Set PLIQRA_API_KEY to ${key?'the key above':'the same application key'}, then invoke this connection.</p><pre>${esc(command)}</pre>${operation?'':`<p>Keep your existing OpenAI client. Add this setting when creating the client.</p><pre>${esc('default_headers={"X-Pliqra-Binding": "'+binding.id+'"}')}</pre><p>JavaScript / TypeScript</p><pre>${esc('defaultHeaders: {"X-Pliqra-Binding": "'+binding.id+'"}')}</pre><p class="muted">Use this service name as model. Renew explicitly when the contract changes or expires.</p>`}<p class="muted">Accepted contract fingerprint</p><div class="key-box mono">${esc(binding.fingerprint)}</div>`,'Copy invocation',async()=>{await copy(command);toast('Invocation copied.');},key?btn('Copy access key','j-copy-key','key','',`data-key="${esc(key.token)}"`):'');
  });
 }
};

document.addEventListener('change',async event=>{
 if(event.target.id!=='f-journey-service')return;
 state.journeyService=event.target.value;
 try{await PliqraJourney.load();renderView();}catch(error){toast(error.message,true);}
});
document.addEventListener('toggle',event=>{if(event.target.id==='advanced-nav')state.advancedNav=event.target.open;},true);
