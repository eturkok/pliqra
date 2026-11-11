import json
import math
import hashlib
import copy

import httpx
from fastapi import HTTPException

from .security import endpoint_url

MAX_RESPONSE_BYTES = 16_000_000
MAX_STREAM_LINE_BYTES = 1_000_000


def strict_json(value):
    def invalid_constant(_):
        raise ValueError('Non-finite JSON number')
    def finite_float(number):
        result=float(number)
        if not math.isfinite(result):
            raise ValueError('Non-finite JSON number')
        return result
    try:
        return json.loads(value, parse_constant=invalid_constant, parse_float=finite_float)
    except RecursionError:
        raise ValueError('Endpoint JSON nesting exceeded the parser limit') from None


async def response_json(response, limit=MAX_RESPONSE_BYTES):
    chunks, size = [], 0
    async for chunk in response.aiter_bytes(chunk_size=65536):
        size += len(chunk)
        if size > limit:
            raise ValueError('Endpoint response exceeded its size limit')
        chunks.append(chunk)
    return strict_json(b''.join(chunks))


async def bounded_lines(response):
    pending, size = bytearray(), 0
    skip_lf, first = False, True
    # Preserve transport chunks: chunk_size buffering would delay the first SSE token.
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError('Stream exceeded its size limit')
        pending.extend(chunk)
        if skip_lf and pending:
            if pending[0]==10:del pending[:1]
            skip_lf=False
        while pending:
            endings=[n for n in (pending.find(b'\n'),pending.find(b'\r')) if n>=0]
            if not endings:break
            ending=min(endings)
            if ending > MAX_STREAM_LINE_BYTES:
                raise ValueError('Stream frame exceeded its size limit')
            line = bytes(pending[:ending])
            consumed=ending+1
            if pending[ending]==13:
                if consumed<len(pending) and pending[consumed]==10:consumed+=1
                elif consumed==len(pending):skip_lf=True
            del pending[:consumed]
            yield line.decode('utf-8-sig' if first else 'utf-8')
            first=False
        if len(pending) > MAX_STREAM_LINE_BYTES:
            raise ValueError('Stream frame exceeded its size limit')
    if pending:
        yield bytes(pending).decode('utf-8-sig' if first else 'utf-8')


async def sse_data(lines, *, comments=False):
    """Dispatch bounded SSE events only at blank lines, never at EOF."""
    data, size = [], 0
    async for line in lines:
        if not line:
            if data:yield '\n'.join(data)
            data,size=[],0
        elif line=='data' or line.startswith('data:'):
            value=line[5:].removeprefix(' ')
            size+=len(value.encode())+1
            if size>MAX_STREAM_LINE_BYTES:raise ValueError('SSE event exceeded its size limit')
            data.append(value)
        elif comments and line.startswith(':'):
            yield line


def headers(secret, name="Authorization", prefix="Bearer"):
    if name.lower() in {"host", "content-length", "connection", "cookie", "transfer-encoding"} or name.lower().startswith("x-forwarded-"):
        raise HTTPException(422, "Choose an authentication header, not a transport or forwarding header")
    return {name: (prefix.strip() + " " if prefix.strip() else "") + secret} if secret else {}


async def discover(client, endpoint, secret="", provider="auto", auth_header="Authorization", auth_prefix="Bearer"):
    native_provider=provider if provider in ('responses','anthropic') else None
    if native_provider:provider='openai'
    endpoint = endpoint_url(endpoint)
    for ending in ("/chat/completions", "/models", "/api/tags", "/api/chat"):
        if endpoint.endswith(ending):
            endpoint = endpoint[:-len(ending)]
            break
    candidates = []
    if provider in ("auto", "openai"):
        candidates.extend([("openai", endpoint + "/models"), ("openai", endpoint + "/v1/models")])
    if provider in ("auto", "ollama"):
        root = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
        candidates.append(("ollama", root + "/api/tags"))
    errors = []
    for kind, url in candidates:
        try:
            auth=headers(secret,auth_header,auth_prefix)
            if native_provider=='anthropic':auth['anthropic-version']='2023-06-01'
            async with client.stream('GET', url, headers=auth, timeout=8) as response:
                if response.status_code in (401, 403):
                    errors.append("Endpoint rejected the credentials")
                    continue
                if response.status_code != 200:
                    errors.append(f"Discovery returned HTTP {response.status_code}")
                    continue
                payload = await response_json(response, 2_000_000)
            entries = payload.get("data" if kind == "openai" else "models")
            if not isinstance(entries, list):
                continue
            models = []
            for item in entries[:200]:
                if not isinstance(item, dict):
                    continue
                name = item.get("id" if kind == "openai" else "name")
                if isinstance(name, str) and 0 < len(name) <= 256:
                    metadata = {key: item[key] for key in ("id", "name", "created", "owned_by", "digest", "details", "context_length") if key in item}
                    fingerprint = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
                    models.append({"model": name, "capabilities": ["chat", "streaming"], "metadata_hash": fingerprint, "evidence": "adapter-declared; inference not yet tested"})
            if models:
                base = url[:-len("/models")] if kind == "openai" else url[:-len("/api/tags")]
                return {"provider": native_provider or kind, "endpoint": base, "models": models}
        except (httpx.HTTPError, ValueError, AttributeError):
            errors.append("Endpoint is unreachable or did not return a supported model list")
    raise HTTPException(422, {"message": "Could not discover models. Check the endpoint and credentials, or enter the model name manually.", "reasons": list(dict.fromkeys(errors))})


def upstream_request(connection, body, model, secret):
    payload = dict(body)
    payload["model"] = model
    if connection['provider'] in ('responses','anthropic'):
        from .native import responses_request,anthropic_request
        auth=headers(secret,connection.get('auth_header','Authorization'),connection.get('auth_prefix','Bearer'))
        if connection['provider']=='anthropic':
            auth['anthropic-version']='2023-06-01'
            return connection['endpoint']+'/messages',anthropic_request(body,model),auth
        return connection['endpoint']+'/responses',responses_request(body,model),auth
    if connection["provider"] == "openai":
        if payload.get("stream"):
            payload["stream_options"] = {**payload.get("stream_options", {}), "include_usage": True}
        return connection["endpoint"] + "/chat/completions", payload, headers(secret, connection.get("auth_header", "Authorization"), connection.get("auth_prefix", "Bearer"))
    allowed = {"model": model, "messages": ollama_messages(body["messages"]), "stream": bool(body.get("stream"))}
    options = {}
    for key, dest in (("temperature", "temperature"), ("top_p", "top_p"), ("max_tokens", "num_predict"), ("max_completion_tokens", "num_predict"), ("seed", "seed")):
        if key in body:
            options[dest] = body[key]
    if options:
        allowed["options"] = options
    if body.get("tools"):
        allowed["tools"] = body["tools"]
    elif body.get('functions'):
        allowed['tools']=[{'type':'function','function':item} for item in body['functions']]
    choice=body.get('tool_choice',body.get('function_call'))
    if choice=='none':allowed.pop('tools',None)
    elif isinstance(choice,dict):
        name=choice.get('name') or choice.get('function',{}).get('name')
        allowed['tools']=[t for t in allowed.get('tools',[]) if t['function']['name']==name]
        if not allowed['tools']:raise HTTPException(422,'Selected function is not declared')
    fmt = body.get("response_format", {})
    if fmt.get("type") == "json_object":
        allowed["format"] = "json"
    elif fmt.get("type") == "json_schema":
        allowed["format"] = fmt.get("json_schema", {}).get("schema", {})
    return connection["endpoint"] + "/api/chat", allowed, headers(secret, connection.get("auth_header", "Authorization"), connection.get("auth_prefix", "Bearer"))


def ollama_messages(messages):
    result=[];names={}
    for message in messages:
        value={k:copy.deepcopy(message[k]) for k in ('role','content') if k in message}
        value['content']=value.get('content') or ''
        calls=message.get('tool_calls') or []
        if message.get('function_call'):calls=[{'id':'legacy_'+message['function_call']['name'],'function':message['function_call']}]
        if calls:
            value['tool_calls']=[]
            for call in calls:
                function=copy.deepcopy(call['function'])
                if isinstance(function['arguments'],str):function['arguments']=strict_json(function['arguments'])
                if not isinstance(function['arguments'],dict):raise HTTPException(422,'Tool arguments must be a JSON object')
                names[call.get('id','')]=function['name']
                value['tool_calls'].append({'type':'function','function':function})
        if message['role'] in ('tool','function'):
            name=names.get(message.get('tool_call_id','')) or message.get('name')
            if not name:raise HTTPException(422,'Tool result must refer to a preceding tool call')
            value.update(role='tool',tool_name=name)
        if isinstance(value['content'],list):
            text=[];images=[]
            for part in value['content']:
                if part['type']=='text':text.append(part['text'])
                elif part['type'] in ('image_url','input_image'):
                    image=part.get('image_url') or part.get('image_url',{})
                    url=image.get('url') if isinstance(image,dict) else image
                    if not isinstance(url,str) or not url.startswith('data:image/') or ';base64,' not in url:
                        raise HTTPException(422,'Ollama image inputs require inline base64 image data')
                    images.append(url.split(';base64,',1)[1])
                else:raise HTTPException(422,'Unsupported Ollama content part')
            value['content']='\n'.join(text)
            if images:value['images']=images
        result.append(value)
    return result


def ollama_calls(calls,offset=0):
    result=[]
    for index,call in enumerate(calls or []):
        function=call['function']
        arguments=function['arguments']
        if not isinstance(arguments,str):arguments=json.dumps(arguments,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        if not isinstance(strict_json(arguments),dict):raise ValueError('Invalid tool arguments')
        identity=hashlib.sha256((str(offset+index)+function['name']+arguments).encode()).hexdigest()[:20]
        result.append({'id':call.get('id') or 'call_'+identity,'type':'function','function':{'name':function['name'],'arguments':arguments}})
    return result


def ollama_result(data, request_id, model):
    message = data.get("message", {})
    calls=ollama_calls(message.get('tool_calls'))
    result = {"id": request_id, "object": "chat.completion", "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": message.get("content", ""), **({"tool_calls": calls} if calls else {}), **({'reasoning_content':message['thinking']} if message.get('thinking') else {})}, "finish_reason": 'tool_calls' if calls else 'length' if data.get('done_reason')=='length' else "stop"}]}
    if "prompt_eval_count" in data and "eval_count" in data:
        result["usage"] = {"prompt_tokens": data["prompt_eval_count"], "completion_tokens": data["eval_count"], "total_tokens": data["prompt_eval_count"] + data["eval_count"]}
    return result


def sse(data):
    return "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n"
