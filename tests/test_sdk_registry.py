"""Publication retries must verify immutable bytes, never mask registry failures."""
import hashlib
import json
from urllib.error import HTTPError

import pytest

from scripts.check_sdk_registry import read, verify


@pytest.fixture(params=['npm','pypi'])
def published(tmp_path,request):
    registry=request.param
    names=['eturkok-pliqra-0.1.0-rc.1.tgz'] if registry=='npm' else ['pliqra_client-0.1.0rc1-py3-none-any.whl','pliqra_client-0.1.0rc1.tar.gz']
    artifacts=[];bodies={}
    for name in names:
        data=('tested immutable bytes: '+name).encode()
        (tmp_path/name).write_bytes(data)
        artifacts.append({'file':name,'sha256':hashlib.sha256(data).hexdigest()})
        bodies['https://'+('registry.npmjs.org' if registry=='npm' else 'files.pythonhosted.org')+'/'+name]=data
    (tmp_path/'SDK-MANIFEST.json').write_text(json.dumps({'sdk_version':'0.1.0-rc.1','python_version':'0.1.0rc1','artifacts':artifacts}))
    urls=list(bodies)
    metadata={'name':'@eturkok/pliqra','version':'0.1.0-rc.1','dist':{'tarball':urls[0]}} if registry=='npm' else {'info':{'name':'pliqra-client','version':'0.1.0rc1'},'urls':[{'filename':name,'url':url} for name,url in zip(names,urls)]}
    def reader(url):return bodies[url] if url in bodies else json.dumps(metadata).encode()
    return registry,tmp_path,bodies,metadata,reader


def test_matching_published_bytes_make_a_retry_successful(published):
    registry,root,_,_,reader=published
    result=verify(registry,root,reader)
    assert result['already_published'] and result['verified_artifacts']


def test_different_published_bytes_are_never_accepted(published):
    registry,root,bodies,_,reader=published
    bodies[next(iter(bodies))]=b'different published artifact'
    with pytest.raises(ValueError,match='differs from the tested release'):verify(registry,root,reader)


def test_changed_local_artifact_is_rejected_before_registry_access(published):
    registry,root,_,_,_=published
    artifact=json.loads((root/'SDK-MANIFEST.json').read_text())['artifacts'][0]['file']
    (root/artifact).write_bytes(b'changed after release verification')
    def no_network(url):raise AssertionError('must fail before network access')
    with pytest.raises(ValueError,match='Local SDK artifact'):verify(registry,root,no_network)


def test_only_a_real_version_404_allows_initial_publication(published):
    registry,root,_,_,_=published
    def missing(url):raise HTTPError(url,404,'Not Found',{},None)
    assert verify(registry,root,missing)['already_published'] is False
    for code in (401,403,429,500):
        def denied(url):raise HTTPError(url,code,'Registry error',{},None)
        with pytest.raises(HTTPError) as error:verify(registry,root,denied)
        assert error.value.code==code


def test_incomplete_or_wrong_registry_identity_is_not_treated_as_absent(published):
    registry,root,_,metadata,reader=published
    if registry=='npm':
        metadata['name']='@someone-else/pliqra'
        with pytest.raises(ValueError,match='Unexpected npm package identity'):verify(registry,root,reader)
    else:
        metadata['urls']=metadata['urls'][:1]
        with pytest.raises(ValueError,match='artifact set is incomplete'):verify(registry,root,reader)


@pytest.mark.parametrize('url',['http://registry.npmjs.org/pkg','https://untrusted.example/pkg','https://user:secret@pypi.org/pkg'])
def test_registry_verification_rejects_unexpected_destinations(url):
    with pytest.raises(ValueError,match='Unexpected SDK registry'):read(url)
