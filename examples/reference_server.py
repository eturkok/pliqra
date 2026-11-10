"""Independent minimal Pliqra server, using only the Python standard library.

Forward to a running OpenAI-compatible endpoint with REFERENCE_UPSTREAM,
REFERENCE_MODEL and optional REFERENCE_UPSTREAM_KEY. This implementation shares
no code, database, authentication or SDK internals with the Pliqra gateway.
"""
import hashlib
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError,URLError

STABLE_PROTOCOL = 'pliqra/1.0'
LEGACY_PROTOCOL = 'pliqra/0.1-draft'
SUPPORTED_PROTOCOLS = [STABLE_PROTOCOL, LEGACY_PROTOCOL]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class Handler(BaseHTTPRequestHandler):
    bindings={}
    lock=threading.RLock()

    def log_message(self,*_):pass

    def send_json(self,status,data):
        raw=json.dumps(data).encode()
        self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.protocol_headers();self.end_headers();self.wfile.write(raw)

    def protocol_headers(self):
        selected=self.headers.get('Pliqra-Protocol',LEGACY_PROTOCOL)
        if len(self.headers.get_all('Pliqra-Protocol',[]))<=1 and selected in SUPPORTED_PROTOCOLS:self.send_header('Pliqra-Protocol',selected)
        self.send_header('Vary','Pliqra-Protocol')
        self.send_header('Cache-Control','no-store')

    def select_protocol(self):
        selected=self.headers.get('Pliqra-Protocol',LEGACY_PROTOCOL)
        if len(self.headers.get_all('Pliqra-Protocol',[]))>1 or selected not in SUPPORTED_PROTOCOLS:
            self.send_json(406,{'detail':{'code':'unsupported_protocol','supported':SUPPORTED_PROTOCOLS}})
            return None
        return selected

    def principal(self):
        return hashlib.sha256(self.headers.get('Authorization','').encode()).hexdigest()

    def authenticated(self):
        expected=os.environ.get('REFERENCE_TOKEN','')
        actual=self.headers.get('Authorization','').removeprefix('Bearer ')
        return bool(expected) and secrets.compare_digest(actual,expected)

    def fingerprint(self):
        value=[os.environ.get('REFERENCE_UPSTREAM'),os.environ.get('REFERENCE_MODEL'),'reference-chat',1]
        return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

    def do_GET(self):
        selected=self.select_protocol()
        if selected is None:return
        if self.path=='/.well-known/pliqra':return self.send_json(200,{'protocol':selected,'status':'stable' if selected==STABLE_PROTOCOL else 'compatibility','preferred_protocol':STABLE_PROTOCOL,'supported_protocols':SUPPORTED_PROTOCOLS,'specification_version':'1.0.0','discovery':'/protocol/v1/services','bind':'/protocol/v1/bindings','extensions':{},'conformance_profile':'pliqra-core/1'})
        if not self.authenticated():return self.send_json(401,{'detail':'Access key required'})
        if self.path=='/protocol/v1/services':return self.send_json(200,{'protocol':selected,'services':[{'name':'reference-chat','id':'reference-chat','version':1,'fingerprint':self.fingerprint(),'requirements':['chat'],'deployments':[{'model':os.environ.get('REFERENCE_MODEL'),'capabilities':['chat','streaming'],'verified':[]}],'readiness':'ready','invocation':{'interface':'openai-chat-completions','model':'reference-chat','path':'/v1/chat/completions'}}]})
        self.send_json(404,{'detail':'Not found'})

    def do_POST(self):
        selected=self.select_protocol()
        if selected is None:return
        if not self.authenticated():return self.send_json(401,{'detail':'Access key required'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>1_000_000:raise ValueError()
            body=json.loads(self.rfile.read(length))
        except (ValueError,TypeError):return self.send_json(400,{'detail':'Invalid JSON request'})
        if not isinstance(body,dict):return self.send_json(422,{'detail':'Use a JSON object'})
        if self.path=='/protocol/v1/bindings':
            for field in ('required_extensions','optional_extensions'):
                extensions=body.get(field,[])
                if not isinstance(extensions,list) or len(extensions)>20 or any(not isinstance(item,str) for item in extensions):return self.send_json(422,{'detail':'Use at most 20 extension identifiers per list'})
            ttl=body.get('ttl_seconds',3600)
            if isinstance(ttl,bool) or not isinstance(ttl,int) or not 60<=ttl<=86400:return self.send_json(422,{'detail':'ttl_seconds must be an integer from 60 to 86400'})
            if body.get('required_extensions'):return self.send_json(422,{'detail':{'code':'unsupported_extensions','supported':[]}})
            required=body.get('requirements',[])
            if body.get('service')!='reference-chat':return self.send_json(404,{'detail':'Service not found'})
            if not isinstance(required,list) or any(not isinstance(item,str) for item in required) or set(required)-{'chat','streaming'}:return self.send_json(422,{'detail':'Unsupported capability'})
            record_id='bind_'+secrets.token_hex(12)
            record={'id':record_id,'service':'reference-chat','version':1,'fingerprint':self.fingerprint(),'expires':time.time()+ttl,'invoke':f'/protocol/v1/bindings/{record_id}/invoke','requirements':required}
            record['extensions']=[]
            with self.lock:self.bindings[record_id]={**record,'principal':self.principal()}
            return self.send_json(201,record)
        if self.path.startswith('/protocol/v1/bindings/') and self.path.endswith('/invoke'):
            record_id=self.path.split('/')[4]
            with self.lock:record=self.bindings.get(record_id)
            if not record:return self.send_json(404,{'detail':'Binding not found'})
            if record['principal']!=self.principal() or record['expires']<=time.time():return self.send_json(403,{'detail':'Binding expired or belongs to another identity'})
            if record['fingerprint']!=self.fingerprint():return self.send_json(409,{'detail':'Service contract changed'})
            if body.get('model')!='reference-chat':return self.send_json(409,{'detail':'Bound service name changed'})
            if not isinstance(body.get('messages'),list):return self.send_json(422,{'detail':'Use messages'})
            if selected==STABLE_PROTOCOL:
                if set(body)-{'model','messages','stream','max_tokens'} or not body['messages'] or any(not isinstance(m,dict) or set(m)-{'role','content'} or m.get('role') not in ('system','developer','user','assistant') or not isinstance(m.get('content'),str) for m in body['messages']):return self.send_json(422,{'detail':'The reference core supports text messages and optional max_tokens only'})
                maximum=body.get('max_tokens',1024)
                if isinstance(maximum,bool) or not isinstance(maximum,int) or not 1<=maximum<=32768 or not isinstance(body.get('stream',False),bool):return self.send_json(422,{'detail':'Invalid core chat parameters'})
            body['model']=os.environ['REFERENCE_MODEL']
            auth={'Content-Type':'application/json'}
            if os.getenv('REFERENCE_UPSTREAM_KEY'):auth['Authorization']='Bearer '+os.environ['REFERENCE_UPSTREAM_KEY']
            request=Request(os.environ['REFERENCE_UPSTREAM'].rstrip('/')+'/chat/completions',json.dumps(body).encode(),headers=auth,method='POST')
            try:
                with build_opener(NoRedirect()).open(request,timeout=90) as response:
                    if not body.get('stream'):
                        raw=response.read(16_000_001)
                        if len(raw)>16_000_000:return self.send_json(502,{'detail':'Upstream response exceeded the reference limit'})
                        try:
                            result=json.loads(raw)
                            if not isinstance(result,dict) or not isinstance(result.get('choices'),list) or not result['choices']:raise ValueError()
                        except (ValueError,TypeError):return self.send_json(502,{'detail':'Upstream returned an invalid completion'})
                        return self.send_json(200,result)
                    if not response.headers.get('Content-Type','').startswith('text/event-stream'):return self.send_json(502,{'detail':'Upstream did not return an event stream'})
                    self.send_response(200);self.send_header('Content-Type','text/event-stream' if body.get('stream') else 'application/json');self.send_header('Connection','close');self.protocol_headers();self.end_headers()
                    while True:
                        value=response.read1(4096)
                        if not value:break
                        self.wfile.write(value);self.wfile.flush()
                self.close_connection=True
            except (HTTPError,URLError):self.send_json(502,{'detail':'Upstream request failed'})
            except (BrokenPipeError,ConnectionResetError):self.close_connection=True
            return
        self.send_json(404,{'detail':'Not found'})


if __name__=='__main__':
    if not all(os.getenv(name) for name in ['REFERENCE_TOKEN','REFERENCE_UPSTREAM','REFERENCE_MODEL']):raise SystemExit('Set REFERENCE_TOKEN, REFERENCE_UPSTREAM and REFERENCE_MODEL')
    ThreadingHTTPServer(('0.0.0.0',int(os.getenv('REFERENCE_PORT','8098'))),Handler).serve_forever()
