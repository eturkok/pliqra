"""Protocol 1.0 negotiation and real, frozen 0.1.1 client interoperability."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from http.server import BaseHTTPRequestHandler
from sdk.pliqra_client import Pliqra, PliqraError
from jsonschema import Draft202012Validator
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_app import workspace, connect, key
from tests.test_live_conformance import gateway_server, http_server, Upstream, Reference

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'tests/compat/sdk011'


def module(path, name):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_frozen_clients_are_the_unmodified_published_sources():
    manifest=json.loads((FROZEN/'manifest.json').read_text())
    for entry in manifest['files']:
        assert hashlib.sha256((FROZEN/entry['file']).read_bytes()).hexdigest()==entry['sha256']


def test_gateway_canonical_messages_match_the_frozen_schema(workspace):
    client,app,_=workspace;service=connect(client)['services'][0]
    schema=json.loads((ROOT/'docs/protocol/1.0/schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    request={'service':service['name'],'requirements':['chat','streaming'],'ttl_seconds':60}
    selected={'Pliqra-Protocol':'pliqra/1.0'}
    for definition,body in [('discovery',client.get('/.well-known/pliqra',headers=selected).json()),('catalog',client.get('/protocol/v1/services',headers=selected).json()),('bindRequest',request),('binding',client.post('/protocol/v1/bindings',headers=selected,json=request).json())]:
        Draft202012Validator({**schema,'$ref':'#/$defs/'+definition}).validate(body)


def test_version_selection_honors_mounted_workspace_paths_and_duplicate_rejection(workspace):
    _,app,_=workspace
    parent=FastAPI();parent.mount('/workspace',app)
    with TestClient(parent) as client:
        metadata=client.get('/workspace/.well-known/pliqra',headers={'Pliqra-Protocol':'pliqra/1.0'})
        assert metadata.json()['protocol']=='pliqra/1.0'
        assert metadata.headers['Pliqra-Protocol']=='pliqra/1.0'
        response=client.get('/workspace/.well-known/pliqra',headers=[('Pliqra-Protocol','pliqra/1.0'),('Pliqra-Protocol','pliqra/0.1-draft')])
        assert response.status_code==406 and 'Pliqra-Protocol' not in response.headers


@pytest.mark.parametrize('protocol,supported',[('pliqra/1.0',['pliqra/1.0']),('pliqra/0.1-draft',[]),('pliqra/2.0',[]),('pliqra/0.1-draft',['pliqra/1.0'])])
def test_python_negotiation_rejects_unknown_and_inconsistent_versions_repeatedly(protocol,supported):
    class MetadataHandler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            assert self.headers['Pliqra-Protocol']=='pliqra/1.0'
            raw=json.dumps({'protocol':protocol,'supported_protocols':supported,'discovery':'/protocol/v1/services','bind':'/protocol/v1/bindings'}).encode()
            self.send_response(200);self.end_headers();self.wfile.write(raw)
    with http_server(MetadataHandler) as url:
        instance=Pliqra(url,'synthetic-key')
        rejected=protocol=='pliqra/2.0' or protocol=='pliqra/0.1-draft' and 'pliqra/1.0' in supported
        for _ in range(2):
            if rejected:
                with pytest.raises(PliqraError):instance.metadata()
                assert instance.protocol is None
            else:assert instance.metadata()['protocol']==protocol


def test_reference_expiry_identity_and_contract_guards(monkeypatch):
    with http_server(Upstream) as upstream:
        monkeypatch.setenv('REFERENCE_UPSTREAM',upstream+'/v1');monkeypatch.setenv('REFERENCE_MODEL','synthetic');monkeypatch.setenv('REFERENCE_TOKEN','synthetic-first')
        with http_server(Reference) as url:
            instance=Pliqra(url,'synthetic-first');bound=instance.connect('reference-chat')
            Reference.bindings[bound.binding['id']]['expires']=0
            with pytest.raises(PliqraError) as expired:bound.complete([])
            assert expired.value.status==403
            Reference.bindings[bound.binding['id']]['expires']=9999999999
            monkeypatch.setenv('REFERENCE_TOKEN','synthetic-second')
            another=Pliqra(url,'synthetic-second')
            with pytest.raises(PliqraError) as identity:another.request(bound.binding['invoke'],{'model':'reference-chat','messages':[]})
            assert identity.value.status==403
            monkeypatch.setenv('REFERENCE_TOKEN','synthetic-first');monkeypatch.setenv('REFERENCE_MODEL','changed')
            with pytest.raises(PliqraError) as changed:bound.complete([])
            assert changed.value.status==409


@pytest.mark.parametrize('language',['python','javascript','go'])
@pytest.mark.parametrize('target',['gateway','reference','legacy-reference'])
@pytest.mark.parametrize('generation',['current','sdk011'])
def test_cross_version_real_http(workspace, monkeypatch, tmp_path, language, target, generation):
    if language in ('javascript','go') and not shutil.which('node' if language=='javascript' else 'go'):
        if os.getenv('PLIQRA_REQUIRE_LANGUAGES')=='1':pytest.fail('Required CI language tool is missing: '+language)
        pytest.skip('Language compiler/runtime is unavailable locally: '+language)
    client,app,_=workspace
    legacy=generation=='sdk011'
    expected='pliqra/0.1-draft' if legacy or target=='legacy-reference' else 'pliqra/1.0'
    # New clients talk to the frozen old reference; actual frozen clients talk
    # to both new implementations. All paths perform real HTTP complete/stream.

    def consume(url,token,service):
        config={'url':url,'token':token,'service':service,'protocol':expected}
        if language=='python':
            sdk=module(FROZEN/'python.py' if legacy else ROOT/'sdk/pliqra_client.py','compat_python')
            instance=sdk.Pliqra(url,token)
            assert instance.metadata()['protocol']==expected
            binding=instance.connect(service,['chat','streaming'])
            messages=[{'role':'user','content':'Synthetic compatibility check'}]
            assert binding.complete(messages)['choices']
            assert list(binding.stream(messages))
        else:
            if language=='javascript':
                source=FROZEN/'javascript.mjs' if legacy else ROOT/'sdk/javascript/pliqra.mjs'
                command=['node',str(ROOT/'tests/compat/consumer.mjs'),str(source)]
                directory=ROOT
            else:
                directory=tmp_path/'go-consumer';directory.mkdir()
                (directory/'cmd').mkdir()
                shutil.copyfile(FROZEN/'go.go' if legacy else ROOT/'sdk/go/pliqra.go',directory/'client.go')
                shutil.copyfile(ROOT/'tests/compat/consumer.go',directory/'cmd/main.go')
                (directory/'go.mod').write_text('module compatclient\n\ngo 1.23\n')
                command=['go','run','./cmd']
            result=subprocess.run(command,input=json.dumps(config),text=True,capture_output=True,cwd=directory,timeout=120)
            assert result.returncode==0,result.stderr
            assert json.loads(result.stdout)['passed']

    if target=='gateway':
        service=connect(client)['services'][0]
        token=key(client,rpm=100,allowed_ports=[service['id']])['token']
        with gateway_server(app) as url:consume(url,token,service['name'])
    else:
        handler=Reference if target=='reference' else module(FROZEN/'reference_server.py','compat_reference').Handler
        with http_server(Upstream) as upstream:
            monkeypatch.setenv('REFERENCE_UPSTREAM',upstream+'/v1')
            monkeypatch.setenv('REFERENCE_MODEL','synthetic')
            monkeypatch.setenv('REFERENCE_TOKEN','synthetic-compatibility-key')
            with http_server(handler) as url:consume(url,'synthetic-compatibility-key','reference-chat')


@pytest.mark.parametrize('version',[None,'pliqra/0.1-draft','pliqra/1.0'])
def test_selected_version_is_consistent_and_does_not_change_contracts(workspace,version):
    client,app,_=workspace;service=connect(client)['services'][0]
    before=client.get('/protocol/v1/services').json()['services'][0]['fingerprint']
    headers={'Pliqra-Protocol':version} if version else {}
    metadata=client.get('/.well-known/pliqra',headers=headers)
    selected=version or 'pliqra/0.1-draft'
    assert metadata.json()['protocol']==selected and metadata.headers['Pliqra-Protocol']==selected
    assert metadata.json()['preferred_protocol']=='pliqra/1.0'
    assert metadata.json()['status']==('stable' if version=='pliqra/1.0' else 'compatibility')
    assert 'pliqra-protocol' in metadata.headers['vary'].lower()
    catalog=client.get('/protocol/v1/services',headers=headers)
    assert catalog.json()['protocol']==selected
    assert catalog.json()['services'][0]['protocol']==selected
    assert catalog.json()['services'][0]['fingerprint']==before
    binding=client.post('/protocol/v1/bindings',headers=headers,json={'service':service['name']}).json()
    for invocation_version in ('pliqra/1.0','pliqra/0.1-draft'):
        assert client.post(binding['invoke'],headers={'Pliqra-Protocol':invocation_version},json={'model':service['name'],'messages':[{'role':'user','content':'Same accepted contract'}]}).status_code==200


def test_unsupported_version_cannot_create_bindings_or_start_inference(workspace):
    client,app,captured=workspace;service=connect(client)['services'][0]
    binding=client.post('/protocol/v1/bindings',json={'service':service['name']}).json()
    before=len(captured);count=app.state.store.one('SELECT COUNT(*) AS n FROM bindings')['n']
    for path,body in [('/.well-known/pliqra',None),('/protocol/v1/services',None),('/protocol/v1/bindings',{'service':service['name']}),(binding['invoke'],{'model':service['name'],'messages':[]})]:
        headers={'Pliqra-Protocol':'pliqra/2.0'}
        response=client.get(path,headers=headers) if body is None else client.post(path,headers=headers,json=body)
        assert response.status_code==406 and response.json()['detail']['code']=='unsupported_protocol'
    assert len(captured)==before and app.state.store.one('SELECT COUNT(*) AS n FROM bindings')['n']==count


def test_stable_binding_expiry_identity_revocation_and_changed_contract(workspace):
    client,app,captured=workspace;service=connect(client)['services'][0]
    first=key(client,rpm=100);second=key(client,rpm=100)
    auth={'Authorization':'Bearer '+first['token'],'Pliqra-Protocol':'pliqra/1.0'}
    bound=client.post('/protocol/v1/bindings',headers=auth,json={'service':service['name']}).json()
    payload={'model':service['name'],'messages':[{'role':'user','content':'Must not reach a provider'}]}
    start=len(captured)
    assert client.post(bound['invoke'],headers={**auth,'Authorization':'Bearer '+second['token']},json=payload).status_code==403
    app.state.store.update('bindings',bound['id'],{'expires':0})
    assert client.post(bound['invoke'],headers=auth,json=payload).status_code==403
    app.state.store.update('bindings',bound['id'],{'expires':9999999999,'fingerprint':'changed-contract'})
    assert client.post(bound['invoke'],headers=auth,json=payload).status_code==409
    app.state.store.update('api_keys',first['id'],{'active':0})
    assert client.post(bound['invoke'],headers=auth,json=payload).status_code==401
    assert len(captured)==start
