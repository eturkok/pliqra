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
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError


class Handler(BaseHTTPRequestHandler):
    bindings={}
    lock=threading.RLock()

    def log_message(self,*_):pass

    def send_json(self,status,data):
        raw=json.dumps(data).encode()
        self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

    def authenticated(self):
        expected=os.environ.get('REFERENCE_TOKEN','')
        actual=self.headers.get('Authorization','').removeprefix('Bearer ')
        return bool(expected) and secrets.compare_digest(actual,expected)

    def fingerprint(self):
        value=[os.environ.get('REFERENCE_UPSTREAM'),os.environ.get('REFERENCE_MODEL'),'reference-chat',1]
        return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

    def do_GET(self):
        if self.path=='/.well-known/pliqra':return self.send_json(200,{'protocol':'pliqra/0.1-draft','discovery':'/protocol/v1/services','bind':'/protocol/v1/bindings','extensions':{},'conformance_profile':'pliqra-core/1'})
        if not self.authenticated():return self.send_json(401,{'detail':'Access key required'})
        if self.path=='/protocol/v1/services':return self.send_json(200,{'protocol':'pliqra/0.1-draft','services':[{'name':'reference-chat','id':'reference-chat','version':1,'fingerprint':self.fingerprint(),'requirements':['chat'],'deployments':[{'model':os.environ.get('REFERENCE_MODEL'),'capabilities':['chat','streaming'],'verified':[]}],'readiness':'ready','invocation':{'interface':'openai-chat-completions','model':'reference-chat','path':'/v1/chat/completions'}}]})
        self.send_json(404,{'detail':'Not found'})

    def do_POST(self):
        if not self.authenticated():return self.send_json(401,{'detail':'Access key required'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>1_000_000:raise ValueError()
            body=json.loads(self.rfile.read(length))
        except (ValueError,TypeError):return self.send_json(400,{'detail':'Invalid JSON request'})
        if self.path=='/protocol/v1/bindings':
            if body.get('required_extensions'):return self.send_json(422,{'detail':{'code':'unsupported_extensions','supported':[]}})
            required=body.get('requirements',[])
            if body.get('service')!='reference-chat':return self.send_json(404,{'detail':'Service not found'})
            if not isinstance(required,list) or set(required)-{'chat','streaming'}:return self.send_json(422,{'detail':'Unsupported capability'})
            record_id='bind_'+secrets.token_hex(12)
            record={'id':record_id,'service':'reference-chat','version':1,'fingerprint':self.fingerprint(),'expires':time.time()+min(86400,max(60,int(body.get('ttl_seconds',3600)))),'invoke':f'/protocol/v1/bindings/{record_id}/invoke','requirements':required}
            record['extensions']=[]
            with self.lock:self.bindings[record_id]=record
            return self.send_json(201,record)
        if self.path.startswith('/protocol/v1/bindings/') and self.path.endswith('/invoke'):
            record_id=self.path.split('/')[4]
            with self.lock:record=self.bindings.get(record_id)
            if not record or record['expires']<=time.time():return self.send_json(403,{'detail':'Binding expired or missing'})
            if record['fingerprint']!=self.fingerprint():return self.send_json(409,{'detail':'Service contract changed'})
            if body.get('model')!='reference-chat' or not isinstance(body.get('messages'),list):return self.send_json(422,{'detail':'Use the bound service name and messages'})
            body['model']=os.environ['REFERENCE_MODEL']
            auth={'Content-Type':'application/json'}
            if os.getenv('REFERENCE_UPSTREAM_KEY'):auth['Authorization']='Bearer '+os.environ['REFERENCE_UPSTREAM_KEY']
            request=Request(os.environ['REFERENCE_UPSTREAM'].rstrip('/')+'/chat/completions',json.dumps(body).encode(),headers=auth,method='POST')
            try:
                with urlopen(request,timeout=90) as response:
                    self.send_response(200);self.send_header('Content-Type','text/event-stream' if body.get('stream') else 'application/json');self.send_header('Connection','close');self.end_headers()
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
