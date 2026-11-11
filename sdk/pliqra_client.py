"""Dependency-free client for the Pliqra discovery/binding protocol.

This module deliberately does not import the gateway implementation.
"""
import argparse
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class PliqraError(Exception):
    def __init__(self,status,detail):
        self.status,self.detail=status,detail
        super().__init__(f'HTTP {status}: {detail}')


class ContractChanged(PliqraError):
    pass


def checked_json(response):
    value=response.read(32_000_001)
    if len(value)>32_000_000:raise PliqraError(502,'Response exceeded 32 MB')
    return json.loads(value)


def sse_events(response):
    """Read transport chunks without waiting to fill a buffer; frame SSE events."""
    pending=bytearray();data=[];size=total=0;skip_lf=False;first=True
    read=getattr(response,'read1',response.read)
    while True:
        chunk=read(65536)
        if not chunk:break
        total+=len(chunk)
        if total>16_000_000:raise PliqraError(502,'Stream exceeded 16 MB')
        pending.extend(chunk)
        if skip_lf and pending:
            if pending[0]==10:del pending[:1]
            skip_lf=False
        while pending:
            endings=[n for n in (pending.find(b'\n'),pending.find(b'\r')) if n>=0]
            if not endings:break
            ending=min(endings)
            if ending>1_000_000:raise PliqraError(502,'Stream line exceeded 1 MB')
            line=bytes(pending[:ending]).decode('utf-8-sig' if first else 'utf-8');first=False
            consumed=ending+1
            if pending[ending]==13:
                if consumed<len(pending) and pending[consumed]==10:consumed+=1
                elif consumed==len(pending):skip_lf=True
            del pending[:consumed]
            if not line:
                if data:yield '\n'.join(data)
                data=[];size=0
            elif line=='data' or line.startswith('data:'):
                value=line[5:].removeprefix(' ');size+=len(value.encode())+1
                if size>1_000_000:raise PliqraError(502,'Stream event exceeded 1 MB')
                data.append(value)
        if len(pending)>1_000_000:raise PliqraError(502,'Stream line exceeded 1 MB')
    # Unterminated events, including a bare [DONE], are not dispatched at EOF.


class Pliqra:
    def __init__(self,base_url,api_key,timeout=90):
        parsed=urlsplit(base_url)
        if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use an HTTP(S) base URL without credentials, query, or fragment')
        self.base_url,self.api_key,self.timeout=base_url.rstrip('/'),api_key,timeout
        self.opener=build_opener(NoRedirect())
        self.protocol=None

    def request(self,path,body=None):
        if not path.startswith('/') or path.startswith('//') or '://' in path or '\\' in path:raise ValueError('Server returned a non-local protocol path')
        data=json.dumps(body).encode() if body is not None else None
        request=Request(self.base_url+path,data=data,headers={'Authorization':'Bearer '+self.api_key,'Content-Type':'application/json','Pliqra-Protocol':'pliqra/1.0'},method='POST' if data is not None else 'GET')
        try:return self.opener.open(request,timeout=self.timeout)
        except HTTPError as exc:
            with exc:
                try:detail=json.loads(exc.read(200000)).get('detail','Protocol request failed')
                except (ValueError,AttributeError):detail='Protocol request failed'
            cls=ContractChanged if exc.code==409 else PliqraError
            raise cls(exc.code,detail) from None

    def services(self):
        with self.request(self.metadata()['discovery']) as response:return checked_json(response)['services']

    def metadata(self):
        if self.protocol is None:
            with self.request('/.well-known/pliqra') as response:document=checked_json(response)
            if not isinstance(document,dict) or document.get('protocol') not in ('pliqra/1.0','pliqra/0.1-draft'):raise PliqraError(422,'Unsupported Pliqra protocol version')
            if not isinstance(document.get('supported_protocols',[]),list):raise PliqraError(422,'Invalid protocol advertisement')
            if document.get('protocol')=='pliqra/0.1-draft' and 'pliqra/1.0' in document.get('supported_protocols',[]):raise PliqraError(422,'Server did not honor explicit protocol selection')
            self.protocol=document
        return self.protocol

    def connect(self,service,requirements=None,ttl_seconds=3600,required_extensions=None,optional_extensions=None):
        required=required_extensions or []
        with self.request(self.metadata()['bind'],{'service':service,'requirements':requirements if requirements is not None else ['chat'],'ttl_seconds':ttl_seconds,'required_extensions':required,'optional_extensions':optional_extensions or []}) as response:
            binding=checked_json(response)
            if set(required)-set(binding.get('extensions',[])):raise PliqraError(422,'Server did not negotiate all required extensions')
            return Connection(self,binding)


class Connection:
    def __init__(self,client,binding):
        self.client,self.binding=client,binding

    def complete(self,messages,**parameters):
        body={**parameters,'model':self.binding['service'],'messages':messages,'stream':False}
        with self.client.request(self.binding['invoke'],body) as response:return checked_json(response)

    def operation(self,operation,**parameters):
        if 'pliqra.operations/1' not in self.binding.get('extensions',[]):raise PliqraError(422,'Negotiate pliqra.operations/1 when connecting')
        if operation not in ('embeddings','speech','transcription'):raise ValueError('Unsupported operation')
        path='/protocol/v1/bindings/'+self.binding['id']+'/operations/'+operation
        with self.client.request(path,{'model':self.binding['service'],**parameters}) as response:
            value=response.read(32_000_001)
            if len(value)>32_000_000:raise PliqraError(502,'Response exceeded 32 MB')
            return json.loads(value) if 'json' in response.headers.get('Content-Type','') else value

    def batch(self,items,name='Application batch',store_outputs=True):
        if 'pliqra.batches/1' not in self.binding.get('extensions',[]):raise PliqraError(422,'Negotiate pliqra.batches/1 when connecting')
        body={'name':name,'store_outputs':store_outputs,'items':[{'binding_id':self.binding['id'],'custom_id':str(index),'body':{'model':self.binding['service'],**item}} for index,item in enumerate(items)]}
        with self.client.request('/api/v1/batches',body) as response:return checked_json(response)

    def stream(self,messages,**parameters):
        body={**parameters,'model':self.binding['service'],'messages':messages,'stream':True}
        terminal=False
        with self.client.request(self.binding['invoke'],body) as response:
            for data in sse_events(response):
                data=data.strip()
                if data=='[DONE]':terminal=True;break
                if not data:continue
                item=json.loads(data)
                if item.get('error'):raise PliqraError(502,item['error'].get('message','Streaming failed'))
                yield item
        if not terminal:raise PliqraError(502,'Stream ended without a completion marker')


def main():
    parser=argparse.ArgumentParser(description='Discover, bind and invoke Pliqra services')
    parser.add_argument('--url',default='http://localhost:8088')
    parser.add_argument('--key-env',default='PLIQRA_API_KEY')
    parser.add_argument('command',choices=['services','chat','conformance'])
    parser.add_argument('--service')
    parser.add_argument('--prompt',default='Reply with OK.')
    args=parser.parse_args()
    token=os.getenv(args.key_env)
    if not token:parser.error('Set '+args.key_env+' to a scoped access key')
    client=Pliqra(args.url,token)
    if args.command=='services':print(json.dumps(client.services(),indent=2));return
    if not args.service:parser.error('--service is required')
    bound=client.connect(args.service,['chat'])
    if args.command=='conformance':
        assert bound.binding['fingerprint'] and bound.binding['expires']
        try:client.connect(args.service,['unsupported-capability'])
        except PliqraError as exc:assert exc.status==422
        else:raise AssertionError('Unknown capability was silently accepted')
        print('Discovery and binding conformance passed. No inference was performed.');return
    result=bound.complete([{'role':'user','content':args.prompt}])
    print(result['choices'][0]['message'].get('content',''))


if __name__=='__main__':main()
