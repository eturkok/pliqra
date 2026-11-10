"""Executable pliqra-core/1 conformance profile, independent of gateway internals."""
import argparse
import json
import os
import time
from urllib.request import Request, build_opener
from urllib.error import HTTPError

try:
    from .pliqra_client import Pliqra,PliqraError,NoRedirect
except ImportError:
    from pliqra_client import Pliqra,PliqraError,NoRedirect


def run(client,service,invoke=False):
    results=[];state={}
    def check(identifier,operation):
        try:operation();results.append({'id':identifier,'passed':True})
        except Exception as exc:results.append({'id':identifier,'passed':False,'error':type(exc).__name__,'http_status':getattr(exc,'status',None)})
    def metadata():
        document=client.metadata()
        assert document['protocol'] in ('pliqra/1.0','pliqra/0.1-draft')
        for field in ('bind','discovery'):assert document[field].startswith('/') and not document[field].startswith('//')
        state['metadata']=document
    def discovery():
        available=client.services()
        selected=next(item for item in available if service in (item['id'],item['name']))
        assert isinstance(selected['fingerprint'],str) and len(selected['fingerprint'])>=16
        state['manifest']=selected
    def binding():
        connected=client.connect(service,['chat'])
        assert connected.binding['fingerprint']==state['manifest']['fingerprint']
        assert connected.binding['expires']>time.time() and connected.binding['invoke'].startswith('/')
        state['connection']=connected
    def rejected(body):
        try:
            with client.request(client.metadata()['bind'],body):pass
        except PliqraError as exc:assert exc.status==422
        else:raise AssertionError('Unsupported contract was accepted')
    def optional():
        connected=client.connect(service,['chat'],optional_extensions=['pliqra.example-unsupported/999'])
        assert 'pliqra.example-unsupported/999' not in connected.binding.get('extensions',[])
    def authentication():
        invalid=Pliqra(client.base_url,'invalid-conformance-key')
        try:invalid.services()
        except PliqraError as exc:assert exc.status in (401,403)
        else:raise AssertionError('Invalid credentials were accepted')
    check('CORE-01-discovery-metadata',metadata)
    check('CORE-02-service-contract',discovery)
    check('CORE-03-explicit-binding',binding)
    check('CORE-04-unknown-capability',lambda:rejected({'service':service,'requirements':['pliqra-unsupported-capability']}))
    check('CORE-05-required-extension',lambda:rejected({'service':service,'requirements':['chat'],'required_extensions':['pliqra.example-unsupported/999']}))
    check('CORE-06-optional-extension',optional)
    check('CORE-07-authentication',authentication)
    if state.get('metadata',{}).get('protocol')=='pliqra/1.0':
        def versioning():
            document=state['metadata']
            assert document['status']=='stable' and document['preferred_protocol'] in document['supported_protocols']
            assert 'pliqra/1.0' in document['supported_protocols']
            opener=build_opener(NoRedirect())
            with opener.open(Request(client.base_url+'/.well-known/pliqra',headers={'Pliqra-Protocol':'pliqra/1.0'}),timeout=client.timeout) as response:
                assert response.headers['Pliqra-Protocol']=='pliqra/1.0'
                assert 'pliqra-protocol' in response.headers.get('Vary','').lower()
            try:opener.open(Request(client.base_url+'/.well-known/pliqra',headers={'Pliqra-Protocol':'pliqra/999.0'}),timeout=client.timeout)
            except HTTPError as exc:assert exc.code==406
            else:raise AssertionError('Unknown protocol was accepted')
        check('CORE-10-version-negotiation',versioning)
        check('CORE-11-invalid-ttl',lambda:rejected({'service':service,'requirements':['chat'],'ttl_seconds':0}))
        def missing_binding():
            try:
                with client.request('/protocol/v1/bindings/bind_conformance_missing/invoke',{'model':service,'messages':[{'role':'user','content':'Must not reach a provider'}]}):pass
            except PliqraError as exc:assert exc.status==404
            else:raise AssertionError('Missing binding was accepted')
        check('CORE-12-missing-binding',missing_binding)
        def bound_service():
            try:
                with client.request(state['connection'].binding['invoke'],{'model':'pliqra-conformance-wrong-service','messages':[{'role':'user','content':'Must not reach a provider'}]}):pass
            except PliqraError as exc:assert exc.status==409
            else:raise AssertionError('A binding was used for another service')
        check('CORE-13-bound-service',bound_service)
    if invoke:
        def completion():
            response=state['connection'].complete([{'role':'user','content':'Synthetic conformance request. Reply with OK.'}],max_tokens=32)
            assert isinstance(response['choices'][0]['message'],dict)
        def streaming():
            chunks=list(state['connection'].stream([{'role':'user','content':'Synthetic conformance request. Reply with OK.'}],max_tokens=32))
            assert chunks and all(isinstance(item,dict) for item in chunks)
        check('CORE-08-completion',completion)
        check('CORE-09-stream-terminal',streaming)
    return {'profile':'pliqra-core/1','protocol':state.get('metadata',{}).get('protocol'),'stability':'stable' if state.get('metadata',{}).get('protocol')=='pliqra/1.0' else 'compatibility','target':client.base_url,'service':service,'tested_at':int(time.time()),'inference_performed':invoke,
            'passed':all(result['passed'] for result in results),'results':results,
            'scope':'Core interoperability only. Does not certify model quality, safety, real-vendor compatibility, deployment resilience or third-party adoption.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True);parser.add_argument('--service',required=True)
    parser.add_argument('--key-env',default='PLIQRA_API_KEY');parser.add_argument('--invoke',action='store_true',help='Perform two real model calls, including a stream')
    args=parser.parse_args();token=os.getenv(args.key_env)
    if not token:parser.error('Set '+args.key_env+' to a scoped access key')
    report=run(Pliqra(args.url,token),args.service,args.invoke)
    print(json.dumps(report,indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
