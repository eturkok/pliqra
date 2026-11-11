"""Non-chat model interfaces sharing the gateway's authorization and accounting."""
import asyncio
import base64
import json
import math
import time
from typing import Annotated, Literal

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .connectors import headers, response_json
from .db import encode


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)


class EmbeddingInput(Strict):
    model:str
    input:str | list[str] | list[int] | list[list[int]]
    dimensions:Annotated[int|None,Field(ge=1,le=65536)]=None
    encoding_format:Literal['float','base64']='float'
    user:str|None=None
    pliqra:dict|None=None

    @field_validator('input')
    @classmethod
    def nonempty(cls,value):
        if not value or isinstance(value,list) and (len(value)>2048 or any(item=='' or item==[] for item in value)):
            raise ValueError('Provide 1–2048 nonempty inputs')
        if len(encode(value))>1_000_000:raise ValueError('Embedding input exceeds 1 MB')
        return value


class SpeechInput(Strict):
    model:str
    input:Annotated[str,Field(min_length=1,max_length=20000)]
    voice:Annotated[str,Field(min_length=1,max_length=100)]='alloy'
    instructions:Annotated[str|None,Field(max_length=2000)]=None
    response_format:Literal['mp3','opus','aac','flac','wav','pcm']='mp3'
    speed:Annotated[float,Field(ge=.25,le=4)]=1
    pliqra:dict|None=None


class TranscriptionInput(Strict):
    model:str
    audio_base64:Annotated[str,Field(min_length=1,max_length=44_000_000)]
    filename:Annotated[str,Field(min_length=1,max_length=100)]='audio.wav'
    language:Annotated[str|None,Field(max_length=20)]=None
    prompt:Annotated[str|None,Field(max_length=2000)]=None
    temperature:Annotated[float,Field(ge=0,le=1)]=0
    response_format:Literal['json','text']='json'
    pliqra:dict|None=None


CAPS={'embeddings':'embeddings','speech':'audio_speech','transcription':'audio_transcription'}
CONTENT_TYPES={'mp3':'audio/mpeg','opus':'audio/ogg','aac':'audio/aac','flac':'audio/flac','wav':'audio/wav','pcm':'application/octet-stream'}


def canonical(operation,body):
    if operation=='transcription':text='[Audio input; content is not available for text guardrails]'
    else:
        value=body['input']
        text=value if isinstance(value,str) else encode(value)
    messages=[{'role':'user','content':text}]
    if operation=='speech' and body.get('instructions'):messages.insert(0,{'role':'system','content':body['instructions']})
    return {'model':body['model'],'messages':messages,'max_tokens':1,'stream':False,
            **({'pliqra':body['pliqra']} if body.get('pliqra') else {})}


def normalize_embedding(data,model,body,provider):
    if provider=='ollama':
        vectors=data.get('embeddings')
        if not isinstance(vectors,list):raise ValueError('Invalid embedding response')
        entries=[{'embedding':vector,'index':index} for index,vector in enumerate(vectors)]
        count=data.get('prompt_eval_count')
    else:
        entries=data.get('data'); count=(data.get('usage') or {}).get('prompt_tokens')
    if not isinstance(entries,list) or not entries:raise ValueError('Missing embeddings')
    source=body['input']
    expected=len(source) if isinstance(source,list) and source and isinstance(source[0],(str,list)) else 1
    if len(entries)!=expected:raise ValueError('Embedding count does not match input count')
    result=[]
    for index,entry in enumerate(entries):
        if entry.get('index')!=index:raise ValueError('Invalid embedding index')
        vector=entry['embedding']
        if isinstance(vector,list):
            if not vector or len(vector)>65536 or any(isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x) for x in vector):raise ValueError('Invalid vector')
            if body.get('dimensions') and len(vector)!=body['dimensions']:raise ValueError('Embedding dimension mismatch')
            if body.get('encoding_format')=='base64':
                import struct
                vector=base64.b64encode(struct.pack('<'+'f'*len(vector),*vector)).decode()
        elif isinstance(vector,str) and body.get('encoding_format')=='base64':
            decoded=base64.b64decode(vector,validate=True)
            if not decoded or len(decoded)%4 or len(decoded)>65536*4:raise ValueError('Invalid vector encoding')
            import struct
            if not all(math.isfinite(v) for v in struct.unpack('<'+'f'*(len(decoded)//4),decoded)):raise ValueError('Non-finite vector')
            if body.get('dimensions') and len(decoded)//4!=body['dimensions']:raise ValueError('Embedding dimension mismatch')
        else:raise ValueError('Unsupported vector encoding')
        result.append({'object':'embedding','index':index,'embedding':vector})
    usage={'prompt_tokens':count,'total_tokens':count} if isinstance(count,int) and not isinstance(count,bool) and count>=0 else {}
    return {'object':'list','data':result,'model':model,**({'usage':usage} if usage else {})},({'usage':{'prompt_tokens':count,'completion_tokens':0}} if usage else {})


async def run_operation(gateway,operation,body,principal,gateway_id=None,binding=None,request_kind='inference'):
    try:return await _run_operation(gateway,operation,body,principal,gateway_id,binding,request_kind)
    except HTTPException as exc:
        if not (isinstance(exc.detail,dict) and exc.detail.get('request_id')) and exc.status_code in (402,403,409,422,429,503):
            gateway.rejected({'model':body['model']},principal,exc,gateway_id)
        raise


async def _run_operation(gateway,operation,body,principal,gateway_id=None,binding=None,request_kind='inference'):
    normalized=canonical(operation,body)
    with gateway.store.lock:
        normalized,port,candidates,decisions,policy,admission=gateway.prepare(normalized,principal,binding,request_kind,gateway_id,[CAPS[operation]])
    steps=(policy or {}).get('config',{}).get('guardrails',[])
    if operation in ('embeddings','speech') and any('output' in step['stages'] for step in steps):
        raise HTTPException(422,'Text output guardrails cannot inspect vectors or audio; use a compatible gateway policy')
    if operation=='transcription' and any('input' in step['stages'] for step in steps):
        raise HTTPException(422,'Text input guardrails cannot inspect uploaded audio')
    if operation=='embeddings' and isinstance(body['input'],list) and body['input'] and isinstance(body['input'][0],int) and steps:
        raise HTTPException(422,'Guarded embeddings require text inputs')
    audio=None
    if operation=='transcription':
        try:audio=base64.b64decode(body['audio_base64'],validate=True)
        except ValueError:raise HTTPException(422,'Invalid base64 audio')
        if not audio or len(audio)>32_000_000:raise HTTPException(413,'Audio must contain 1 byte to 32 MB')
        if '/' in body['filename'] or '\\' in body['filename'] or '\x00' in body['filename']:raise HTTPException(422,'Use an audio filename without a path')
        normalized['_input_units']=len(audio)
    if operation=='speech':normalized['_output_units']=port.get('max_output_tokens',32768)
    store=gateway.store
    with gateway.cluster.checking('model_operation'):
        event_id=None;chosen=candidates[0];started=time.perf_counter()
        try:
            prices={field:max(d[field] for d in candidates) if all(d[field] is not None for d in candidates) else None for field in ('input_price','output_price')}
            if operation!='embeddings':prices={'input_price':None,'output_price':None}
            event_id,_=gateway.begin(principal,port,chosen,normalized,decisions,prices,request_kind,len(candidates),policy,operation,admission,candidates)
            if policy:
                checked=await gateway.policies.check(policy,'input',normalized,gateway.client,event_id)
                if checked['messages']!=normalized['messages']:
                    gateway.refresh_reservation(event_id,port,checked,prices,len(candidates),policy)
                normalized=checked
            payload={k:v for k,v in body.items() if k not in ('pliqra','audio_base64','filename')}
            if operation!='transcription':
                checked=normalized['messages'][-1]['content']
                payload['input']=json.loads(checked) if isinstance(body['input'],list) else checked
                if operation=='speech' and body.get('instructions'):payload['instructions']=normalized['messages'][0]['content']
            for chosen in candidates:
                if chosen['provider'] not in ('openai','ollama') or chosen['provider']=='ollama' and operation!='embeddings':
                    raise HTTPException(422,'This adapter does not support the selected operation')
                gateway.secrets.client=gateway.client
                secret=await gateway.secrets.resolve(gateway.vault.decrypt(chosen['secret']))
                auth=headers(secret,chosen['auth_header'],chosen['auth_prefix'])
                if chosen['provider']=='ollama':
                    url=chosen['endpoint']+'/api/embed'
                    outgoing={'model':chosen['model'],'input':payload['input'],**({'dimensions':payload['dimensions']} if payload.get('dimensions') else {})}
                else:
                    url=chosen['endpoint']+({'embeddings':'/embeddings','speech':'/audio/speech','transcription':'/audio/transcriptions'}[operation])
                    outgoing={**payload,'model':chosen['model']}
                    if operation=='transcription':outgoing['response_format']='json'
                options={'files':{'file':(body['filename'],audio,'application/octet-stream')},'data':{k:str(v) for k,v in outgoing.items() if v is not None}} if operation=='transcription' else {'json':outgoing}
                attempt_id=gateway.start_attempt(event_id,chosen)
                try:
                    async with gateway.client.stream('POST',url,headers=auth,**options) as response:
                        if response.status_code>=400:
                            gateway.finish_attempt(attempt_id,chosen,'upstream_error',http_status=response.status_code)
                            if response.status_code in (429,502,503,504):continue
                            raise HTTPException(502,{'message':'Provider rejected the model operation','request_id':event_id})
                        if operation=='speech':
                            chunks=[];size=0
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                size+=len(chunk)
                                if size>32_000_000:raise ValueError('Audio response exceeds 32 MB')
                                chunks.append(chunk)
                            binary=b''.join(chunks)
                            format=body.get('response_format','mp3')
                            media=response.headers.get('content-type','').split(';')[0].lower()
                            header_ok={'mp3':binary.startswith(b'ID3') or (len(binary)>1 and binary[0]==255 and binary[1]&224==224),
                                       'wav':binary.startswith(b'RIFF') and binary[8:12]==b'WAVE','opus':binary.startswith(b'OggS'),
                                       'flac':binary.startswith(b'fLaC'),'aac':len(binary)>1 and binary[0]==255 and binary[1]&240==240,
                                       'pcm':bool(binary) and len(binary)%2==0}[format]
                            if not header_ok or not (media.startswith('audio/') or media=='application/octet-stream'):raise ValueError('Invalid speech response format')
                            result={};accounting={}
                        else:
                            data=await response_json(response)
                            if operation=='embeddings':result,accounting=normalize_embedding(data,chosen['model'],body,chosen['provider'])
                            else:
                                if not isinstance(data,dict) or not isinstance(data.get('text'),str):raise ValueError('Invalid transcription')
                                result={'text':data['text']}; accounting={}
                    gateway.finish_attempt(attempt_id,chosen,'success',accounting,200)
                    break
                except httpx.HTTPError:
                    gateway.finish_attempt(attempt_id,chosen,'connection_error')
            else:raise HTTPException(502,{'message':'All model endpoints failed','request_id':event_id})
            if operation=='transcription' and policy:
                envelope={'choices':[{'message':{'role':'assistant','content':result['text']}}]}
                checked=await gateway.policies.check(policy,'output',normalized,gateway.client,event_id,response=envelope)
                result={'text':checked['choices'][0]['message']['content']}
            store.update('events',event_id,{'deployment_id':chosen['id'],'model':chosen['model'],'connection_name':chosen['connection_name'],'model_site':chosen.get('site') or 'shared'})
            gateway.finish(event_id,started,chosen,'success',accounting)
            gateway.mark_verified(chosen,CAPS[operation])
            gateway.capture_response(event_id,result if operation!='speech' else {'audio_bytes':len(binary),'format':body.get('response_format','mp3')})
            from .topology import model_headers
            response_headers={**model_headers(gateway,chosen),'X-Request-ID':event_id,'X-Pliqra-Model':chosen['model'],**({'X-Pliqra-Gateway':policy['id']} if policy else {})}
            if operation=='speech':return Response(binary,media_type=CONTENT_TYPES[body.get('response_format','mp3')],headers=response_headers)
            if operation=='transcription' and body.get('response_format')=='text':return Response(result['text'],media_type='text/plain',headers=response_headers)
            return JSONResponse(result,headers=response_headers)
        except HTTPException as exc:
            if event_id and not gateway.cluster.fenced:gateway.finish(event_id,started,chosen,'blocked' if exc.status_code==422 and isinstance(exc.detail,dict) and exc.detail.get('code')=='guardrail_blocked' else 'error',error='Model operation failed policy or provider checks')
            if event_id:exc.detail={**exc.detail,'request_id':event_id} if isinstance(exc.detail,dict) else {'message':exc.detail,'request_id':event_id}
            raise
        except asyncio.CancelledError:
            if event_id:gateway.finish(event_id,started,chosen,'cancelled',error='Model operation cancelled; usage may require reconciliation')
            raise
        except (ValueError,TypeError,KeyError,AttributeError,RecursionError):
            if event_id:gateway.finish(event_id,started,chosen,'error',error='Malformed model operation response')
            raise HTTPException(502,{'message':'Malformed model operation response','request_id':event_id})


def mount(app):
    principal=app.state.context['principal']

    @app.post('/v1/embeddings',tags=['Model operations'])
    async def embeddings(body:EmbeddingInput,p=Depends(principal)):
        return await run_operation(app.state.gateway,'embeddings',body.model_dump(exclude_none=True),p)

    @app.post('/v1/audio/speech',tags=['Model operations'])
    async def speech(body:SpeechInput,p=Depends(principal)):
        return await run_operation(app.state.gateway,'speech',body.model_dump(exclude_none=True),p)

    async def transcribe(request,p,gateway_id=None):
        if request.headers.get('content-type','').startswith('multipart/form-data'):
            async with request.form(max_files=1,max_fields=10,max_part_size=32_000_000) as form:
                upload=form.get('file')
                if not upload or not hasattr(upload,'read'):raise HTTPException(422,'Audio file is required')
                raw=await upload.read(32_000_001)
                if len(raw)>32_000_000:raise HTTPException(413,'Audio exceeds 32 MB')
                body=TranscriptionInput.model_validate({**{k:v for k,v in form.items() if k!='file'},'audio_base64':base64.b64encode(raw).decode(),'filename':upload.filename or 'audio.wav'})
        else:body=TranscriptionInput.model_validate(await request.json())
        return await run_operation(app.state.gateway,'transcription',body.model_dump(exclude_none=True),p,gateway_id=gateway_id)

    @app.post('/v1/audio/transcriptions',tags=['Model operations'])
    async def transcription(request:Request,p=Depends(principal)):
        return await transcribe(request,p)

    @app.post('/g/{slug}/v1/audio/transcriptions',tags=['Model operations'])
    async def named_transcription(slug:str,request:Request,p=Depends(principal)):
        return await transcribe(request,p,slug)

    @app.post('/g/{slug}/v1/embeddings',tags=['Model operations'])
    async def named_embeddings(slug:str,body:EmbeddingInput,p=Depends(principal)):
        return await run_operation(app.state.gateway,'embeddings',body.model_dump(exclude_none=True),p,gateway_id=slug)

    @app.post('/g/{slug}/v1/audio/speech',tags=['Model operations'])
    async def named_speech(slug:str,body:SpeechInput,p=Depends(principal)):
        return await run_operation(app.state.gateway,'speech',body.model_dump(exclude_none=True),p,gateway_id=slug)

    @app.post('/protocol/v1/bindings/{record_id}/operations/{operation}',tags=['Protocol'])
    async def bound_operation(record_id:str,operation:Literal['embeddings','speech','transcription'],request:Request,p=Depends(principal)):
        binding=app.state.store.one('SELECT * FROM bindings WHERE id=?',(record_id,))
        if not binding:raise HTTPException(404,'Binding not found')
        schema={'embeddings':EmbeddingInput,'speech':SpeechInput,'transcription':TranscriptionInput}[operation]
        body=schema.model_validate(await request.json()).model_dump(exclude_none=True)
        return await run_operation(app.state.gateway,operation,body,p,binding=binding)
