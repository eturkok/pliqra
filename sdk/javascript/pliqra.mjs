/** Dependency-free Pliqra client for Node 22+ and modern browsers. */
export class PliqraError extends Error {
  constructor(status, detail) { super(`HTTP ${status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`); this.status = status; this.detail = detail; }
}
export class ContractChanged extends PliqraError {}

function localPath(path) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//') || path.includes('://') || path.includes('\\')) throw new Error('Protocol paths must be local');
  return path;
}
async function bounded(response, limit = 16_000_000) {
  const reader = response.body.getReader(); const parts = []; let size = 0;
  try {
    while (true) { const {done,value} = await reader.read(); if (done) break; size += value.byteLength; if (size > limit) throw new PliqraError(502,'Response exceeded its size limit'); parts.push(value); }
  } finally { await reader.cancel().catch(()=>{}); reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const part of parts) { bytes.set(part,offset); offset += part.length; }
  return bytes;
}
async function* sseEvents(response) {
  const reader=response.body.getReader(),decoder=new TextDecoder('utf-8',{fatal:true}),encoder=new TextEncoder();
  let buffer='',data=[],eventSize=0,total=0,skipLF=false;
  try {
    while(true) {
      const {done,value}=await reader.read();
      if(value){total+=value.byteLength;if(total>16_000_000)throw new PliqraError(502,'Stream exceeded 16 MB');}
      buffer+=done?decoder.decode():decoder.decode(value,{stream:true});
      if(skipLF&&buffer){if(buffer[0]==='\n')buffer=buffer.slice(1);skipLF=false;}
      let end;
      while((end=buffer.search(/[\r\n]/))>=0) {
        const line=buffer.slice(0,end),cr=buffer[end]==='\r';let consumed=end+1;
        if(cr){if(buffer[consumed]==='\n')consumed++;else if(consumed===buffer.length)skipLF=true;}
        buffer=buffer.slice(consumed);
        if(encoder.encode(line).byteLength>1_000_000)throw new PliqraError(502,'Stream line exceeded 1 MB');
        if(!line){if(data.length)yield data.join('\n');data=[];eventSize=0;}
        else if(line==='data'||line.startsWith('data:')){
          const text=line.slice(5).replace(/^ /,'');eventSize+=encoder.encode(text).byteLength+1;
          if(eventSize>1_000_000)throw new PliqraError(502,'Stream event exceeded 1 MB');data.push(text);
        }
      }
      if(encoder.encode(buffer).byteLength>1_000_000)throw new PliqraError(502,'Stream line exceeded 1 MB');
      if(done)break;
    }
    // EOF never dispatches an event without its blank-line delimiter.
  } finally {await reader.cancel().catch(()=>{});reader.releaseLock();}
}
export class Pliqra {
  constructor(baseURL, apiKey, {fetch: transport = globalThis.fetch, timeout = 90_000} = {}) {
    const url = new URL(baseURL);
    if (!['http:','https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Use an HTTP(S) base URL without credentials, query or fragment');
    this.baseURL = baseURL.replace(/\/+$/,''); this.apiKey = apiKey; this.fetch = transport; this.timeout = timeout;
  }
  async request(path, body, {signal} = {}) {
    const abort = signal ? AbortSignal.any([signal,AbortSignal.timeout(this.timeout)]) : AbortSignal.timeout(this.timeout);
    const response = await this.fetch(this.baseURL + localPath(path), {method: body === undefined ? 'GET' : 'POST', headers: {'Authorization':`Bearer ${this.apiKey}`,'Content-Type':'application/json','Pliqra-Protocol':'pliqra/1.0'}, body: body === undefined ? undefined : JSON.stringify(body), redirect:'error', credentials:'omit', signal:abort});
    if (!response.ok) {
      let detail = 'Protocol request failed';
      try { detail = JSON.parse(new TextDecoder().decode(await bounded(response,200_000))).detail || detail; } catch {}
      throw new (response.status === 409 ? ContractChanged : PliqraError)(response.status,detail);
    }
    return response;
  }
  async json(path,body,options) { return JSON.parse(new TextDecoder().decode(await bounded(await this.request(path,body,options)))); }
  async metadata() {
    if (!this.protocol) { const metadata = await this.json('/.well-known/pliqra'); if (!['pliqra/1.0','pliqra/0.1-draft'].includes(metadata?.protocol)) throw new PliqraError(422,'Unsupported Pliqra protocol'); if(metadata.supported_protocols!==undefined&&!Array.isArray(metadata.supported_protocols))throw new PliqraError(422,'Invalid protocol advertisement'); if(metadata.protocol==='pliqra/0.1-draft'&&metadata.supported_protocols?.includes('pliqra/1.0'))throw new PliqraError(422,'Server did not honor explicit protocol selection'); this.protocol=metadata; }
    return this.protocol;
  }
  async services() { return (await this.json((await this.metadata()).discovery)).services; }
  async connect(service,{requirements=['chat'],ttlSeconds=3600,requiredExtensions=[],optionalExtensions=[]}={}) {
    const binding = await this.json((await this.metadata()).bind,{service,requirements,ttl_seconds:ttlSeconds,required_extensions:requiredExtensions,optional_extensions:optionalExtensions});
    if (requiredExtensions.some(id=>!binding.extensions?.includes(id))) throw new PliqraError(422,'Server did not negotiate required extensions');
    return new Connection(this,binding);
  }
}
export class Connection {
  constructor(client,binding) { this.client=client; this.binding=binding; }
  async complete(messages,parameters={}) { return this.client.json(this.binding.invoke,{...parameters,model:this.binding.service,messages,stream:false}); }
  async operation(operation,parameters={}) {
    if (!this.binding.extensions?.includes('pliqra.operations/1')) throw new PliqraError(422,'Negotiate pliqra.operations/1 when connecting');
    if (!['embeddings','speech','transcription'].includes(operation)) throw new Error('Unsupported operation');
    const response=await this.client.request(`/protocol/v1/bindings/${encodeURIComponent(this.binding.id)}/operations/${operation}`,{...parameters,model:this.binding.service});
    const bytes=await bounded(response,32_000_000);
    return response.headers.get('content-type')?.includes('json') ? JSON.parse(new TextDecoder().decode(bytes)) : bytes;
  }
  async batch(items,{name='Application batch',storeOutputs=true}={}) {
    if (!this.binding.extensions?.includes('pliqra.batches/1')) throw new PliqraError(422,'Negotiate pliqra.batches/1 when connecting');
    return this.client.json('/api/v1/batches',{name,store_outputs:storeOutputs,items:items.map((item,index)=>({binding_id:this.binding.id,custom_id:String(index),body:{...item,model:this.binding.service}}))});
  }
  async *stream(messages,parameters={},options={}) {
    const response=await this.client.request(this.binding.invoke,{...parameters,model:this.binding.service,messages,stream:true},options);
    for await(const event of sseEvents(response)) {
      const text=event.trim();if(text==='[DONE]')return;if(!text)continue;
      const data=JSON.parse(text);if(data.error)throw new PliqraError(502,data.error.message||'Stream failed');yield data;
    }
    throw new PliqraError(502,'Stream ended without a completion marker');
  }
}
