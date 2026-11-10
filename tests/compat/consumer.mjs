import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const config=JSON.parse(readFileSync(0,'utf8'));
const {Pliqra}=await import(pathToFileURL(process.argv[2]).href);
const client=new Pliqra(config.url,config.token);
const metadata=await client.metadata();
if(metadata.protocol!==config.protocol)throw Error('Unexpected negotiated protocol');
const binding=await client.connect(config.service,{requirements:['chat','streaming']});
const messages=[{role:'user',content:'Synthetic compatibility check'}];
const completion=await binding.complete(messages);
if(!completion.choices?.length)throw Error('Completion failed');
let chunks=0;for await(const chunk of binding.stream(messages)){if(chunk.choices)chunks++;}
if(!chunks)throw Error('Stream produced no data');
console.log(JSON.stringify({passed:true,protocol:metadata.protocol,completion:true,stream:true}));
