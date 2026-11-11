"use strict";

let behaviorFieldId = 0;
window.PliqraBehavior = {
 async load() {
  if (isAdmin() && ['overview','behavior'].includes(state.view)) {
   state.behavior = (await api('/api/v1/behavior-checks')).items;
  }
 },
 status(row) {
  if (!row.enabled && row.last_status!=='running') return badge('configured','Paused');
  const labels = {passed:'Passed',failed:'Assertion failed',error:'Check error',interrupted:'Interrupted',running:'Running',pending:'Waiting',superseded:'Configuration changed',paused:'Paused'};
  return badge(row.last_status === 'passed' ? 'success' : ['failed','error','interrupted'].includes(row.last_status) ? 'error' : 'configured',labels[row.last_status] || row.last_status);
 },
 overview() {
  if (!isAdmin() || !state.conns.length) return '';
  const rows = state.behavior || [];
  return `<section class="panel section-gap"><div class="panel-head"><div><h2>Model behavior</h2><p>Scheduled assertions against the connected endpoints.</p></div>${btn(rows.length?'Open model health':'Set up checks','nav','activity','', 'data-view="behavior"')}</div><div class="behavior-overview">${rows.length ? rows.slice(0,3).map(row=>`<div><strong>${esc(row.name)}</strong>${PliqraBehavior.status(row)}<span>Last successful cycle · ${date(row.last_success_at)}</span></div>`).join('') : '<p>Choose small response, streaming, JSON or tool checks and a daily run limit.</p>'}</div></section>`;
 },
 views: {behavior() {return PliqraBehavior.view();}},
 view() {
  if (!isAdmin()) return head('Model health','Administrator access is required.');
  const rows = (state.behavior || []).filter(row=>!state.behaviorProject || row.project_id===state.behaviorProject);
  const attention = rows.filter(row=>row.enabled && ['failed','error','interrupted'].includes(row.last_status)).length;
  const latest = Math.max(0,...rows.map(row=>row.last_success_at || 0));
  return head('Model health','Track response behavior at the same model endpoint.',btn('New schedule','bh-edit','plus','primary',state.conns.length?'':'disabled')) +
   `<div class="stats"><div class="stat"><div class="stat-top">Enabled schedules</div><div class="stat-value">${rows.filter(row=>row.enabled).length}</div></div><div class="stat"><div class="stat-top">Needs attention</div><div class="stat-value">${attention}</div></div><div class="stat"><div class="stat-top">Last successful cycle</div><div class="behavior-date">${date(latest)}</div></div></div>
   <div class="toolbar">${selectField('Project','behavior-project',[['','All projects'],...state.projects.map(project=>[project.id,project.name])],state.behaviorProject || '')}</div>
   <div class="notice">${icon('info')} Checks make real model calls within their configured limits. Observed failures and recovery create notifications. Test responses are not retained.</div>
   ${rows.map(row=>PliqraBehavior.card(row)).join('') || empty('activity','No behavior-check schedules','Create a schedule, choose the checks, then run a first baseline.')}` + footer();
 },
 card(row) {
  const results = new Map((row.last_results || []).map(result=>[result.name,result]));
  return `<section class="panel section-gap"><div class="panel-head behavior-head"><div><h2>${esc(row.name)} ${PliqraBehavior.status(row)}</h2><p>${esc(row.deployment?.model || 'Missing deployment')} · ${esc(row.deployment?.connection || '')}</p><p class="mono behavior-endpoint">${esc(row.deployment?.endpoint || '')}</p></div><div class="actions">${btn('Run now','bh-run','bolt','',`data-id="${esc(row.id)}" ${row.last_status==='running'?'disabled':''}`)}${btn('Edit','bh-edit','edit','',`data-id="${esc(row.id)}"`)}${btn('History','bh-history','history','',`data-id="${esc(row.id)}"`)}${btn(row.enabled?'Pause':'Enable','bh-toggle',row.enabled?'stop':'bolt','ghost',`data-id="${esc(row.id)}"`)}</div></div>
   <div class="behavior-meta"><span>Every ${row.interval_seconds % 60 ? row.interval_seconds : row.interval_seconds/60} ${row.interval_seconds % 60 ? 'seconds' : row.interval_seconds===60 ? 'minute' : 'minutes'}</span><span>${row.daily_runs}/${row.max_runs_per_day} cycles today · UTC</span><span>${row.maximum_daily_requests} calls/day with current checks · ${row.max_tokens} output tokens/call</span><span>Next cycle · ${row.enabled?date(row.next_run):'Paused'}</span></div>
   ${row.last_error?`<p class="notice warning">${esc(row.last_error)}</p>`:''}
   <div class="behavior-results">${row.checks.map(check=>{
    const result=results.get(check.name);
    const label=result ? {passed:'Passed',failed:'Assertion failed',error:'Check error'}[result.outcome] || result.outcome : 'Not tested';
    return `<article><div><strong>${esc(check.name)}</strong><small>${esc(check.capability.replaceAll('_',' '))}</small></div><div>${badge(result?.passed?'success':result?'error':'configured',label)}${result?.changed?'<span class="chip">Changed</span>':''}</div><p>Last tested · ${date(result?.tested_at)}</p><p>Last successful test · ${date(result?.last_success_at)}</p>${result?.last_change_at?`<p>Last change · ${date(result.last_change_at)} · ${esc(result.previous_outcome)} → ${esc(result.outcome)}</p>`:''}${result?.detail?`<p>${esc(result.detail)}</p>`:''}</article>`;
   }).join('')}</div></section>`;
 },
 document(row) {
  const keys=['name','deployment_id','project_id','enabled','interval_seconds','max_runs_per_day','max_tokens','timeout_seconds','checks'];
  return Object.fromEntries(keys.map(key=>[key,row[key]]));
 },
 checkRow(check={name:'Chat response',capability:'chat'}) {
  const prefix='behavior-check-'+(++behaviorFieldId);
  return `<section class="behavior-check-row" data-behavior-check="${prefix}"><div class="behavior-check-heading"><strong>Response expectation</strong>${btn('Remove','bh-remove-check','close','ghost')}</div>
   <div class="field-inline">${field('Check name',prefix+'-name','','text',check.name,'required maxlength="100"')}${selectField('Capability',prefix+'-capability',[['chat','Chat response'],['streaming','Streaming'],['structured_output','JSON Schema'],['tools','Tool calling']],check.capability)}</div>
   <div data-behavior-text ${check.capability==='chat'?'':'hidden'}>${field('Custom prompt · optional',prefix+'-prompt','Default: reply with OK','text',check.prompt||'','maxlength="4000"')}${field('Expected text · optional',prefix+'-contains','Case-insensitive text match','text',check.contains||'','maxlength="500"')}</div>
   ${field('Maximum response latency · ms, optional',prefix+'-latency','No latency assertion','number',check.max_latency_ms??'','min="1" max="90000"')}
   </section>`;
 },
 edit(id) {
  const row=(state.behavior || []).find(item=>item.id===id);
  const deployments=state.conns.flatMap(connection=>connection.models.map(model=>[model.id,connection.name+' · '+model.model]));
  const defaults=[{name:'Chat response',capability:'chat'},{name:'Streaming',capability:'streaming'},{name:'JSON Schema',capability:'structured_output'},{name:'Tool calling',capability:'tools'}];
  const body=field('Schedule name','name','e.g. Support model checks','text',row?.name||'','required maxlength="100"')+
   selectField('Model deployment','deployment_id',deployments,row?.deployment_id||'')+
   selectField('Project for accounting and budget','project_id',state.projects.map(project=>[project.id,project.name]),row?.project_id||'default')+
   `<div class="field-inline">${field('Interval · minutes','interval_minutes','','number',(row?.interval_seconds||3600)/60,'required min="1" max="10080" step="any"')}${field('Maximum cycles per UTC day','max_runs_per_day','','number',row?.max_runs_per_day||24,'required min="1" max="288"')}</div>`+
   `<div class="field-inline">${field('Maximum output tokens per call','max_tokens','','number',row?.max_tokens||128,'required min="16" max="512"')}${field('Timeout per check · seconds','timeout_seconds','','number',row?.timeout_seconds||30,'required min="5" max="90"')}</div>`+
   `<label class="check-row"><input type="checkbox" name="enabled" ${row?.enabled?'checked':''}>Enable periodic checks</label><p>Each cycle calls the model once per check. Daily limits also include manual and interrupted cycles. Project budgets and workspace concurrency apply.</p>
   <div class="behavior-check-editor">${(row?.checks||defaults).map(check=>PliqraBehavior.checkRow(check)).join('')}</div><div class="actions">${btn('Add check','bh-add-check','plus')}</div>`;
  modal(row?'Edit behavior schedule':'Schedule behavior checks','Checks observe outcomes and produce notifications. Existing routing and capability declarations are controlled separately.',body,'Save schedule',async(form,dialog)=>{
   const checks=Array.from(dialog.querySelectorAll('[data-behavior-check]')).map(node=>{
    const prefix=node.dataset.behaviorCheck;
    const value=key=>node.querySelector(`[name="${prefix}-${key}"]`).value;
    const capability=value('capability');
    return {name:value('name'),capability,prompt:capability==='chat'?value('prompt'):'',contains:capability==='chat'?value('contains'):'',max_latency_ms:value('latency')===''?null:Number(value('latency'))};
   });
   if (!checks.length) throw new Error('Add at least one check.');
   const document={name:form.get('name'),deployment_id:form.get('deployment_id'),project_id:form.get('project_id'),enabled:form.has('enabled'),interval_seconds:Math.round(Number(form.get('interval_minutes'))*60),max_runs_per_day:Number(form.get('max_runs_per_day')),max_tokens:Number(form.get('max_tokens')),timeout_seconds:Number(form.get('timeout_seconds')),checks};
   await api('/api/v1/behavior-checks'+(row?'/'+row.id:''),{method:row?'PUT':'POST',body:JSON.stringify(document)});
   await changed('Behavior-check schedule saved.');
  });
 },
 async history(id,offset=0) {
  const report=await api(`/api/v1/behavior-checks/${id}/runs?offset=${offset}`);
  const row=(state.behavior || []).find(item=>item.id===id);
  const content=report.items.map(run=>`<article class="behavior-history"><h3>${esc(run.status)} · ${date(run.created)} · configuration v${run.version}</h3>${run.error?`<p>${esc(run.error)}</p>`:''}${run.results.map(result=>`<div><strong>${esc(result.name)}</strong> ${badge(result.passed?'success':'error',result.outcome)}<p>${esc(result.detail)}</p>${result.request_id?btn('Inspect request','request-detail','activity','',`data-id="${esc(result.request_id)}"`):''}</div>`).join('')}</article>`).join('');
  const paging=`<div class="actions">${offset?btn('Previous','bh-history','chevron','',`data-id="${esc(id)}" data-offset="${Math.max(0,offset-50)}"`):''}${offset+50<report.total?btn('Next','bh-history','chevron','',`data-id="${esc(id)}" data-offset="${offset+50}"`):''}</div>`;
  modal(row?.name||'Behavior checks',`${report.total} retained cycles · request accounting remains separately available`,content||'<p>No cycles have run yet.</p>',null,null,paging);
 },
 async handle(action,id,element) {
  if (!action.startsWith('bh-')) return false;
  if (action==='bh-edit') PliqraBehavior.edit(id);
  else if (action==='bh-run') {await api(`/api/v1/behavior-checks/${id}/run`,{method:'POST',body:'{}'});await refreshData();renderView();toast('Behavior checks started.');}
  else if (action==='bh-toggle') {
   const row=state.behavior.find(item=>item.id===id);
   const document={...PliqraBehavior.document(row),enabled:!row.enabled};
   await api(`/api/v1/behavior-checks/${id}`,{method:'PUT',body:JSON.stringify(document)});
   await refreshData();renderView();toast(row.enabled?'Periodic checks paused.':'Periodic checks enabled.');
  }
  else if (action==='bh-history') await PliqraBehavior.history(id,Number(element.dataset.offset||0));
  else if (action==='bh-remove-check') element.closest('[data-behavior-check]').remove();
  else if (action==='bh-add-check') {
   const editor=element.closest('form').querySelector('.behavior-check-editor');
   if (editor.children.length>=8) throw new Error('A schedule supports up to eight checks.');
   editor.insertAdjacentHTML('beforeend',PliqraBehavior.checkRow({name:'Text check '+(editor.children.length+1),capability:'chat'}));
  }
  return true;
 }
};

document.addEventListener('change',event=>{
 const node=event.target;
 if (node.id==='f-behavior-project') {state.behaviorProject=node.value;renderView();}
 const row=node.closest('[data-behavior-check]');
 if (row && node.name.endsWith('-capability')) row.querySelector('[data-behavior-text]').hidden=node.value!=='chat';
});
