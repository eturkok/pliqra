import assert from 'node:assert/strict';
import {ReadableStream} from 'node:stream/web';
import {Pliqra,PliqraError,ContractChanged} from './pliqra.mjs';

let requests=[];
const json=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
let mode='normal';
let wireProtocol='pliqra/0.1-draft', advertised=[];
async function transport(url,options){
  requests.push({url,options});
  assert.equal(options.redirect,'error');assert.equal(options.credentials,'omit');
  assert.equal(options.headers.Authorization,'Bearer synthetic-key');
  assert.equal(options.headers['Pliqra-Protocol'],'pliqra/1.0');
  if(url.endsWith('/.well-known/pliqra'))return json({protocol:wireProtocol,supported_protocols:advertised,discovery:'/protocol/v1/services',bind:'/protocol/v1/bindings'});
  if(url.endsWith('/protocol/v1/services'))return json({services:[{name:'synthetic'}]});
  const body=JSON.parse(options.body);
  if(url.endsWith('/protocol/v1/bindings'))return json({id:'binding',service:body.service,fingerprint:'fingerprint',expires:9999999999,invoke:'/protocol/v1/bindings/binding/invoke',extensions:mode==='ignore-extensions'?[]:body.required_extensions});
  if(mode==='changed')return json({detail:'Contract changed'},409);
  if(body.stream){
    const bytes=new TextEncoder().encode('data: '+JSON.stringify({choices:[{delta:{content:'Hello, world 🌍'}}]})+'\n\n'+(mode==='truncated'?'':'data: [DONE]\n\n'));
    let offset=0;
    return new Response(new ReadableStream({pull(controller){if(offset>=bytes.length){controller.close();return;}controller.enqueue(bytes.slice(offset,++offset));}}),{headers:{'content-type':'text/event-stream'}});
  }
  return json({choices:[{message:{role:'assistant',content:'Synthetic completion'}}]});
}
const client=new Pliqra('https://gateway.test/org/research','synthetic-key',{fetch:transport});
assert.equal((await client.services())[0].name,'synthetic');
const bound=await client.connect('synthetic',{requiredExtensions:['pliqra.operations/1']});
assert.equal((await bound.complete([{role:'user',content:'Synthetic'}])).choices[0].message.content,'Synthetic completion');
let text='';for await(const chunk of bound.stream([{role:'user',content:'Synthetic'}]))text+=chunk.choices[0].delta.content;
assert.equal(text,'Hello, world 🌍');
assert.ok(requests.every(request=>request.url.startsWith('https://gateway.test/org/research/')));
mode='changed';await assert.rejects(()=>bound.complete([]),ContractChanged);
mode='truncated';await assert.rejects(async()=>{for await(const _ of bound.stream([])){}},error=>error instanceof PliqraError&&error.status===502);
mode='ignore-extensions';await assert.rejects(()=>client.connect('synthetic',{requiredExtensions:['missing/1']}),error=>error.status===422);
await assert.rejects(()=>client.json('//attacker.test/'),/local/);
assert.throws(()=>new Pliqra('https://user:password@gateway.test','key'),/credentials/);
wireProtocol='pliqra/1.0';advertised=['pliqra/1.0','pliqra/0.1-draft'];
assert.equal((await new Pliqra('https://gateway.test','synthetic-key',{fetch:transport}).metadata()).protocol,'pliqra/1.0');
for(const protocol of ['pliqra/2.0','pliqra/0.1-draft']){
  wireProtocol=protocol;
  const rejected=new Pliqra('https://gateway.test','synthetic-key',{fetch:transport});
  for(let attempt=0;attempt<2;attempt++)await assert.rejects(()=>rejected.metadata(),error=>error.status===422);
}
for(const ending of ['\n','\r\n','\r']){
  const raw='\ufeff: keepalive'+ending+'data: {"choices":'+ending+'data: [{"delta":{"content":"hello 🌍"}}]}'+ending+ending+'data: [DONE]'+ending+ending;
  const bytes=new TextEncoder().encode(raw);let offset=0;
  const streamClient=new Pliqra('https://gateway.test','synthetic-key',{fetch:async()=>new Response(new ReadableStream({pull(c){if(offset===bytes.length)c.close();else c.enqueue(bytes.slice(offset,++offset));}}))});
  const {Connection}=await import('./pliqra.mjs');
  const connection=new Connection(streamClient,{service:'test',invoke:'/invoke'});
  const items=[];for await(const item of connection.stream([]))items.push(item);
  assert.equal(items[0].choices[0].delta.content,'hello 🌍');
}
for(const ending of ['','\n','\r\n','\r']){
  const response=new Response(new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('data: [DONE]'+ending));c.close();}}));
  const streamClient=new Pliqra('https://gateway.test','synthetic-key',{fetch:async()=>response});
  const {Connection}=await import('./pliqra.mjs');
  await assert.rejects(async()=>{for await(const _ of new Connection(streamClient,{service:'test',invoke:'/invoke'}).stream([])){}},e=>e instanceof PliqraError&&e.status===502);
  assert.equal(response.body.locked,false);
}
export const result={passed:18,failed:0};
