"""Disposable synthetic customer lab: public APIs, load/soak, TLS and fault recovery.

No real models, customer data, private credentials or production capacity claims.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, UTC
import ipaddress
import json
import os
from pathlib import Path
import platform
import secrets
import ssl
import statistics
import subprocess
import time
import uuid

import httpx
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

ROOT=Path(__file__).resolve().parents[1]


def validate_stable(report, policy):
    """Fail a release on an incomplete or unhealthy synthetic run, never relabel it."""
    for phase in ('load','soak'):
        result=report[phase]
        assert result['requests']>0 and result['statuses']=={'200':result['requests']},(phase,'unsuccessful requests')
        assert result['p95_ms']<=policy['maximum_p95_ms'],(phase,'synthetic latency gate exceeded')
    soak=report['soak']
    assert soak['duration_seconds']>=policy['soak_seconds'],'Incomplete stable soak'
    assert soak['requests']>=policy['minimum_soak_requests'],'Insufficient stable soak requests'
    rss=soak['gateway_west_rss_kib']
    assert len(rss)>=2,'Insufficient memory samples'
    assert max(rss)-rss[0]<=policy['maximum_rss_growth_kib'],'Synthetic memory growth gate exceeded'


def certificates(directory):
    directory.mkdir(parents=True,exist_ok=True)
    instant=datetime.now(UTC)
    ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Pliqra synthetic acceptance CA')])
    ca=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(instant-timedelta(minutes=5)).not_valid_after(instant+timedelta(days=2)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).add_extension(x509.KeyUsage(digital_signature=True,content_commitment=False,key_encipherment=False,data_encipherment=False,key_agreement=False,key_cert_sign=True,crl_sign=True,encipher_only=False,decipher_only=False),critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False).add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False).sign(ca_key,hashes.SHA256())
    (directory/'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for label,names in [('gateway',['gateway-west','gateway-east','gateway-branch','localhost']),('database',['database','localhost'])]:
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,names[0])])
        cert=x509.CertificateBuilder().subject_name(subject).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(instant-timedelta(minutes=5)).not_valid_after(instant+timedelta(days=2)).add_extension(x509.SubjectAlternativeName([*[x509.DNSName(n) for n in names],x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.KeyUsage(digital_signature=True,content_commitment=False,key_encipherment=True,data_encipherment=False,key_agreement=False,key_cert_sign=False,crl_sign=False,encipher_only=False,decipher_only=False),critical=True).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False).add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False).sign(ca_key,hashes.SHA256())
        (directory/(label+'.crt')).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (directory/(label+'.key')).write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
        # Synthetic, disposable keys must be readable by the non-root fixture containers.
        (directory/(label+'.key')).chmod(0o644)


class Session:
    def __init__(self,url,verify=True):
        self.url=url
        self.client=httpx.Client(base_url=url,verify=verify,timeout=20,trust_env=False)

    def api(self,path,body=None,method=None,status=200,headers=None):
        response=self.client.request(method or ('POST' if body is not None else 'GET'),path,json=body,headers=headers)
        assert response.status_code==status,(path,response.status_code,response.text[:500])
        return response.json()

    def initialize(self,email,password,name):
        data=self.api('/api/v1/setup',{'name':name,'email':email,'password':password})
        self.client.headers['X-Pliqra-CSRF']=data['csrf']
        return data['user']['id']

    def login(self,email,password):
        data=self.api('/api/v1/login',{'email':email,'password':password})
        self.client.headers['X-Pliqra-CSRF']=data['csrf']


class Lab:
    def __init__(self,directory,image):
        self.directory=directory.resolve()
        self.directory.mkdir(parents=True,exist_ok=False)
        self.project='pliqra-accept-'+uuid.uuid4().hex[:10]
        certificates(self.directory/'pki')
        self.env=self.directory/'fixture.env'
        self.env.write_text('PLIQRA_ACCEPTANCE_STATE='+str(self.directory)+'\nPLIQRA_ACCEPTANCE_IMAGE='+image+'\nPLIQRA_ACCEPTANCE_KEY='+Fernet.generate_key().decode()+'\nPLIQRA_ACCEPTANCE_BRANCH_KEY='+Fernet.generate_key().decode()+'\n')
        self.env.chmod(0o600)
        self.verify=ssl.create_default_context(cafile=str(self.directory/'pki/ca.pem'))
        self.west=Session('https://127.0.0.1:18091',self.verify)
        self.east=Session('https://127.0.0.1:18092',self.verify)
        self.branch=Session('https://127.0.0.1:18093',self.verify)
        self.controller=Session('http://127.0.0.1:18094')
        self.sessions=[self.west,self.east,self.branch,self.controller]
        self.report={'synthetic':True,'real_customer_reference':False,'environment':{'host':platform.platform(),'machine':platform.machine(),'physical_hosts':1,'database':'PostgreSQL 18, one primary with verified fixture TLS','site_regions_are_synthetic':True,'image':image},'checks':[]}
        self.secrets={}
        image_details=json.loads(subprocess.check_output(['docker','image','inspect',image],text=True))[0]
        self.report['environment'].update(image_id=image_details['Id'],source_revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),source_dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True)))
        self.report['environment']['image_revision']=image_details['Config']['Labels'].get('org.opencontainers.image.revision')
        runtime=json.loads(subprocess.check_output(['docker','info','--format','{{json .}}'],text=True))
        self.report['environment'].update(docker_cpus=runtime['NCPU'],docker_memory_bytes=runtime['MemTotal'])
        (self.directory/'lab.json').write_text(json.dumps({'project':self.project,'directory':str(self.directory)},indent=2)+'\n')

    @classmethod
    def resume(cls,directory):
        lab=object.__new__(cls)
        lab.directory=directory.resolve()
        saved=json.loads((lab.directory/'lab.json').read_text())
        if not saved['project'].startswith('pliqra-accept-'):raise ValueError('Not an owned acceptance lab')
        lab.project=saved['project'];lab.env=lab.directory/'fixture.env'
        lab.verify=ssl.create_default_context(cafile=str(lab.directory/'pki/ca.pem'))
        lab.west=Session('https://127.0.0.1:18091',lab.verify);lab.east=Session('https://127.0.0.1:18092',lab.verify)
        lab.branch=Session('https://127.0.0.1:18093',lab.verify);lab.controller=Session('http://127.0.0.1:18094')
        lab.sessions=[lab.west,lab.east,lab.branch,lab.controller]
        lab.report=json.loads((lab.directory/'report.json').read_text());lab.report.pop('failure',None)
        lab.secrets=json.loads((lab.directory/'.secrets.json').read_text())
        for session in (lab.west,lab.east,lab.branch):session.login('operator@example.test',lab.secrets['password'])
        lab.controller.login('author@example.test',lab.secrets['password'])
        lab.service=next(p for p in lab.west.api('/api/v1/ports') if p['name']=='customer-chat')
        lab.branch_service=lab.branch.api('/api/v1/ports')[0]
        lab.responses_service=lab.secrets['responses_service'];lab.messages_service=lab.secrets['messages_service'];lab.key=lab.secrets['key']
        return lab

    def compose(self,*args,**kwargs):
        return subprocess.run(['docker','compose','--project-name',self.project,'--env-file',str(self.env),'-f',str(ROOT/'tests/acceptance/compose.yaml'),*args],cwd=ROOT,check=True,**kwargs)

    def mark(self,name,**fields):
        self.report['checks'].append({'check':name,'passed':True,**fields})
        self.save()
        print(json.dumps({'phase':name,'passed':True}),flush=True)

    def save(self):
        (self.directory/'report.json').write_text(json.dumps(self.report,indent=2)+'\n')

    def wait(self,session):
        deadline=time.monotonic()+120
        while True:
            try:
                response=session.client.get('/readyz')
                if response.status_code==200:return
            except httpx.HTTPError as error:
                if 'CERTIFICATE_VERIFY_FAILED' in str(error):raise
            if time.monotonic()>deadline:raise RuntimeError('Fixture gateway did not become ready: '+session.url)
            time.sleep(1)

    def setup(self):
        self.compose('up','-d','--pull','missing')
        for session in self.sessions:self.wait(session)
        password=secrets.token_urlsafe(24)
        for session,label,email in [(self.west,'Synthetic shared workspace','operator@example.test'),(self.branch,'Synthetic independent branch','operator@example.test'),(self.controller,'Synthetic fleet author','author@example.test')]:
            session.initialize(email,password,label)
        self.east.login('operator@example.test',password)
        self.controller.api('/api/v1/users',{'name':'Synthetic reviewer','email':'reviewer@example.test','password':password,'role':'admin','project_ids':[]},status=201)
        self.secrets['password']=password
        deployments=[]
        for session,site,endpoint,name,provider,model in [(self.west,'dc-west','http://model-west:8099/v1','Synthetic west','openai',None),(self.west,'dc-east','http://model-east:8099/v1','Synthetic east','openai',None),(self.west,'dc-west','http://model-west:8099/v1','Synthetic Responses','responses','synthetic-responses'),(self.west,'dc-west','http://model-west:8099/v1','Synthetic Messages','anthropic','synthetic-messages'),(self.branch,'branch','http://model-east:8099/v1','Synthetic branch','openai',None)]:
            body={'name':name,'provider':provider,'endpoint':endpoint,'site':site,'api_key':'synthetic-provider-credential'}
            if model:body['model']=model
            created=session.api('/api/v1/connections',body,status=201)
            connection=next(c for c in session.api('/api/v1/connections') if c['id']==created['id'])
            deployment=connection['models'][0]
            session.api('/api/v1/deployments/'+deployment['id'],{'capabilities':['chat','streaming','tools'],'input_price':2,'output_price':8},method='PATCH')
            if provider=='openai' and session is self.west:deployments.append(deployment['id'])
            if provider=='responses':self.responses_service=created['services'][0]['name']
            if provider=='anthropic':self.messages_service=created['services'][0]['name']
            if session is self.branch:self.branch_service=created['services'][0]
        self.service=self.west.api('/api/v1/ports',{'name':'customer-chat','deployments':deployments,'requirements':['chat'],'placement':{'routing':'local_first'}},status=201)
        self.key=self.west.api('/api/v1/keys',{'name':'Synthetic customer application','rpm':100000,'allowed_ports':[]},status=201)['token']
        self.secrets.update(key=self.key,service='customer-chat',responses_service=self.responses_service,messages_service=self.messages_service)
        self.write_secrets()
        self.mark('onboarding_endpoint_discovery_pricing_and_scoped_key')

    def write_secrets(self):
        path=self.directory/'.secrets.json'
        path.write_text(json.dumps(self.secrets,indent=2)+'\n')
        path.chmod(0o600)

    def call(self,session=None,key=None,stream=False,text='Synthetic customer request',model='customer-chat'):
        return (session or self.west).client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+(key or self.key)},json={'model':model,'messages':[{'role':'user','content':text}],'max_tokens':64,'stream':stream})

    def routing_and_quotas(self):
        for session,site in [(self.west,'dc-west'),(self.east,'dc-east')]:
            response=self.call(session)
            assert response.status_code==200,response.text
            assert response.headers['X-Pliqra-Site']==site and response.headers['X-Pliqra-Model-Site']==site,response.headers
        self.mark('two_site_local_routing')
        profile=self.west.api('/api/v1/gateways',{'name':'Synthetic shared quota','slug':'acceptance-quota-'+uuid.uuid4().hex[:6],'config':{'services':[self.service['id']],'limits':[{'metric':'requests','scope':'gateway','window_seconds':60,'limit':8}]}},status=201)
        token=self.west.api('/api/v1/keys',{'name':'Concurrent quota acceptance','gateway_id':profile['id'],'rpm':1000,'allowed_ports':[self.service['id']]},status=201)['token']
        with ThreadPoolExecutor(max_workers=16) as pool:
            responses=list(pool.map(lambda i:self.call(self.west if i%2 else self.east,token),range(16)))
        assert sum(r.status_code==200 for r in responses)==8,[r.status_code for r in responses]
        assert sum(r.status_code==429 for r in responses)==8
        self.mark('global_quota_exact_under_concurrency',accepted=8,rejected=8)

    def fleet(self):
        members=[]
        shared_token=None
        for session,label,endpoint,service in [(self.west,'Synthetic shared lab','https://gateway-west:8080',self.service),(self.branch,'Synthetic independent branch','https://gateway-branch:8080',self.branch_service)]:
            grant=session.api('/api/v1/fleet/trusts',{'name':'Synthetic fleet controller','scopes':['status','policy']},status=201)
            member=next((m for m in self.controller.api('/api/v1/fleet')['members'] if m['endpoint']==endpoint),None)
            if not member:member=self.controller.api('/api/v1/fleet/members',{'name':label,'endpoint':endpoint,'token':grant['token']},status=201)
            members.append({'member_id':member['id'],'mapping':{'services':{'chat':service['id']}}})
            if session is self.west:shared_token=grant['token']
        duplicate=self.controller.client.post('/api/v1/fleet/members',json={'name':'Same shared workspace, other replica','endpoint':'https://gateway-east:8080','token':shared_token})
        assert duplicate.status_code==409,duplicate.text
        body={'name':'Synthetic customer shared policy','document':{'name':'Customer applications','slug':'fleet-customer','config':{'services':['chat'],'limits':[{'metric':'requests','scope':'gateway','window_seconds':60,'limit':1000}]}},'targets':members}
        proposal=self.controller.api('/api/v1/fleet/proposals',body,status=201)
        response=self.controller.client.post('/api/v1/fleet/proposals/'+proposal['id']+'/decision',json={'fingerprint':proposal['fingerprint']})
        assert response.status_code==403
        self.controller.login('reviewer@example.test',self.secrets['password'])
        self.controller.api('/api/v1/fleet/proposals/'+proposal['id']+'/decision',{'fingerprint':proposal['fingerprint']})
        deployed=self.controller.api('/api/v1/fleet/proposals/'+proposal['id']+'/deploy',{})
        assert all(t['status']=='applied' for t in deployed['targets']),deployed
        self.controller.api('/api/v1/fleet/sync',{})
        summary=self.controller.api('/api/v1/fleet/summary')
        assert len(summary['included_members'])==2 and not summary['excluded_members'],summary
        self.mark('independent_fleet_pairing_separate_approval_and_delivery',members=2)
        self.controller.login('author@example.test',self.secrets['password'])

    def native(self):
        cases=[]
        for service in (self.responses_service,self.messages_service,'customer-chat'):
            for interface in ('responses','messages'):
                body={'model':service,'stream':True,**({'input':'Synthetic native application'} if interface=='responses' else {'messages':[{'role':'user','content':'Synthetic native application'}],'max_tokens':64})}
                started=time.perf_counter();first=None;events=[]
                with self.west.client.stream('POST','/v1/'+interface,headers={'Authorization':'Bearer '+self.key},json=body) as response:
                    assert response.status_code==200
                    for line in response.iter_lines():
                        if line.startswith('data:'):
                            packet=json.loads(line[5:]);events.append(packet)
                            if first is None and packet['type'] in ('response.output_text.delta','content_block_delta'):first=time.perf_counter()-started
                elapsed=time.perf_counter()-started
                terminal='response.completed' if interface=='responses' else 'message_stop'
                assert events[-1]['type']==terminal and first is not None and first<elapsed*.9,(service,interface,first,elapsed,events[-1])
                cases.append({'service':service,'interface':interface,'first_delta_ms':round(first*1000,2),'complete_ms':round(elapsed*1000,2)})
        response=self.west.client.post('/v1/responses',headers={'Authorization':'Bearer '+self.key},json={'model':self.responses_service,'input':'fixture:truncate','stream':True})
        assert 'event: error\n' in response.text and 'event: response.completed\n' not in response.text,response.text
        self.mark('native_incremental_delivery_and_incomplete_stream_rejection',cases=cases)

    def measurements(self,duration,concurrency,rate=None):
        started=time.monotonic();samples=[];statuses={};rss=[]
        def request(index):
            instant=time.perf_counter()
            try:
                response=self.call(self.west if index%2 else self.east)
                return response.status_code,(time.perf_counter()-instant)*1000
            except httpx.HTTPError:return 0,(time.perf_counter()-instant)*1000
        next_log=started+30;next_rss=started
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            index=0
            while time.monotonic()-started<duration:
                batch_started=time.monotonic()
                replies=list(pool.map(request,range(index,index+concurrency)));index+=concurrency
                for status,latency in replies:samples.append(latency);statuses[str(status)]=statuses.get(str(status),0)+1
                if time.monotonic()>=next_rss:
                    code="from pathlib import Path; print(next(line.split()[1] for line in Path('/proc/1/status').read_text().splitlines() if line.startswith('VmRSS:')))"
                    result=self.compose('exec','-T','gateway-west','python','-c',code,capture_output=True,text=True)
                    rss.append(int(result.stdout.strip()));next_rss=time.monotonic()+60
                if time.monotonic()>=next_log:
                    print(json.dumps({'phase':'soak' if rate else 'load','elapsed_seconds':round(time.monotonic()-started),'requests':len(samples),'statuses':statuses}),flush=True);next_log=time.monotonic()+30
                if rate:time.sleep(max(0,concurrency/rate-(time.monotonic()-batch_started)))
        elapsed=time.monotonic()-started;ordered=sorted(samples)
        assert set(statuses)=={'200'},statuses
        percentile=lambda q:round(ordered[min(len(ordered)-1,int((len(ordered)-1)*q))],2)
        return {'duration_seconds':round(elapsed,2),'concurrency':concurrency,'target_requests_per_second':rate,'requests':len(samples),'statuses':statuses,'observed_requests_per_second':round(len(samples)/elapsed,2),'p50_ms':percentile(.5),'p95_ms':percentile(.95),'p99_ms':percentile(.99),'gateway_west_rss_kib':rss,'synthetic_model_delay_ms':5,'not_a_production_capacity_claim':True}

    def faults(self):
        self.compose('stop','model-west')
        try:
            response=self.call()
            assert response.status_code==200 and response.headers['X-Pliqra-Model-Site']=='dc-east',(response.status_code,response.text)
        finally:self.compose('start','model-west')
        self.mark('pre_stream_model_failure_falls_back_to_allowed_remote_site')
        before=self.west.api('/api/v1/platform/status')['current_node']
        count_before=self.model_count()
        self.compose('stop','database')
        try:
            try:response=self.call();assert response.status_code>=500,response.text
            except httpx.HTTPError:pass
            count_after=self.model_count()
            assert count_after==count_before,(count_before,count_after)
        finally:self.compose('start','database')
        for session in (self.west,self.east,self.branch):self.wait(session)
        after=self.west.api('/api/v1/platform/status')['current_node']
        assert after!=before,'The fenced process did not rejoin with a fresh node identity'
        assert self.call().status_code==200
        self.mark('database_restart_fail_closed_no_new_model_calls_and_fresh_process_rejoin',primary_promotion_tested=False)

    def model_count(self):
        total=0
        with httpx.Client(timeout=5,trust_env=False) as direct:
            for port in (18095,18096):
                deadline=time.monotonic()+10
                while True:
                    try:
                        total+=direct.get('http://127.0.0.1:'+str(port)+'/fixture/counts').json()['accepted']
                        break
                    except httpx.HTTPError:
                        if time.monotonic()>deadline:raise
                        time.sleep(.1)
        return total

    def close(self,keep=False):
        for session in self.sessions:session.client.close()
        if not keep:self.compose('down','--volumes')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--image',default='pliqra:acceptance')
    parser.add_argument('--load-seconds',type=int,default=30)
    parser.add_argument('--soak-seconds',type=int,default=600)
    parser.add_argument('--concurrency',type=int,default=8)
    parser.add_argument('--soak-rate',type=float,default=5)
    parser.add_argument('--keep',action='store_true')
    parser.add_argument('--resume',action='store_true',help='Resume only a prior disposable lab created by this script')
    parser.add_argument('--stable',action='store_true',help='Enforce the committed stable-release synthetic acceptance policy')
    args=parser.parse_args()
    policy=json.loads((ROOT/'release-policy.json').read_text())['stable_acceptance']
    if args.stable:args.soak_seconds=policy['soak_seconds']
    if not (1<=args.concurrency<=32 and 0<=args.load_seconds<=600 and 0<=args.soak_seconds<=3600 and 0<args.soak_rate<=50):parser.error('Acceptance bounds exceeded')
    lab=Lab.resume(args.state) if args.resume else Lab(args.state,args.image)
    try:
        if not args.resume:lab.setup()
        lab.routing_and_quotas();lab.fleet();lab.native()
        if args.load_seconds:lab.report['load']=lab.measurements(args.load_seconds,args.concurrency);lab.mark('load_measurement')
        if args.soak_seconds:lab.report['soak']=lab.measurements(args.soak_seconds,args.concurrency,args.soak_rate);lab.mark('soak_measurement')
        if args.stable:
            validate_stable(lab.report,policy)
            lab.mark('stable_synthetic_envelope',policy=policy)
        lab.faults()
        lab.report['passed']=True;lab.save()
    except BaseException as exc:
        lab.report.update(passed=False,failure=type(exc).__name__);lab.save()
        raise
    finally:lab.close(args.keep)
