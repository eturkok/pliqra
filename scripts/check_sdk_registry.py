"""Verify immutable SDK bytes on a public registry before or after publication."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.parse import unquote, parse_qs
from urllib.request import build_opener, HTTPRedirectHandler
from html.parser import HTMLParser

MAX_BYTES=16*1024*1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def read(url):
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname not in {'pypi.org','files.pythonhosted.org','registry.npmjs.org'} or parts.username or parts.password:
        raise ValueError('Unexpected SDK registry download destination')
    with build_opener(NoRedirect).open(url,timeout=20) as response:
        if response.geturl()!=url:
            raise ValueError('SDK registry verification rejects redirects')
        body=response.read(MAX_BYTES+1)
    if len(body)>MAX_BYTES:raise ValueError('SDK registry response exceeded its bound')
    return body


def verify(registry, directory, reader=read):
    manifest=json.loads((directory/'SDK-MANIFEST.json').read_text())
    version=manifest['sdk_version'];python_version=manifest['python_version']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-rc\.[1-9][0-9]*)?',version):raise ValueError('Invalid SDK version')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:rc[1-9][0-9]*)?',python_version):raise ValueError('Invalid Python SDK version')
    if version.replace('-rc.','rc')!=python_version:raise ValueError('SDK language versions differ')
    expected_names=({f'eturkok-pliqra-{version}.tgz'} if registry=='npm' else {f'pliqra_client-{python_version}-py3-none-any.whl',f'pliqra_client-{python_version}.tar.gz'})
    selected={}
    for artifact in manifest['artifacts']:
        name=artifact['file']
        if name not in expected_names:continue
        if name in selected:raise ValueError('Duplicate SDK manifest artifact')
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=artifact['sha256']:raise ValueError('Local SDK artifact differs from release manifest')
        selected[name]=artifact['sha256']
    if selected.keys()!=expected_names:raise ValueError('Incomplete SDK release artifact set')
    endpoint=('https://registry.npmjs.org/'+quote('@eturkok/pliqra',safe='')+'/'+quote(version,safe='') if registry=='npm' else 'https://pypi.org/pypi/pliqra-client/'+python_version+'/json')
    try:metadata=json.loads(reader(endpoint))
    except HTTPError as error:
        if error.code==404:return {'registry':registry,'version':version,'already_published':False}
        raise
    if registry=='npm':
        if metadata.get('name')!='@eturkok/pliqra' or metadata.get('version')!=version:raise ValueError('Unexpected npm package identity')
        urls={next(iter(expected_names)):metadata['dist']['tarball']}
    else:
        if metadata['info']['name']!='pliqra-client' or metadata['info']['version']!=python_version:raise ValueError('Unexpected PyPI package identity')
        urls={f['filename']:f['url'] for f in metadata['urls'] if f['filename'] in expected_names}
        if urls.keys()!=expected_names:raise ValueError('Published Python artifact set is incomplete')
    for name,url in urls.items():
        if hashlib.sha256(reader(url)).hexdigest()!=selected[name]:
            raise ValueError('Published '+name+' differs from the tested release; refusing to overwrite or silently accept it')
    return {'registry':registry,'version':version,'already_published':True,'verified_artifacts':sorted(selected)}


def python_index_ready(directory, reader=read):
    """The version JSON and installer index can propagate at different times."""
    manifest=json.loads((directory/'SDK-MANIFEST.json').read_text())
    version=manifest['python_version']
    expected={f'pliqra_client-{version}-py3-none-any.whl',f'pliqra_client-{version}.tar.gz'}
    hashes={item['file']:item['sha256'] for item in manifest['artifacts'] if item['file'] in expected}
    if hashes.keys()!=expected:raise ValueError('Incomplete Python artifact set for installer index')
    found=set()
    class Links(HTMLParser):
        def handle_starttag(self,tag,attrs):
            if tag!='a':return
            url=dict(attrs).get('href','');parts=urlsplit(url)
            name=unquote(parts.path.rsplit('/',1)[-1])
            if name not in expected:return
            if parts.scheme!='https' or parts.hostname!='files.pythonhosted.org' or parts.username or parts.password:
                raise ValueError('Unexpected Python installer index destination')
            if parse_qs(parts.fragment).get('sha256')!=[hashes[name]]:
                raise ValueError('Python installer index differs from the tested release')
            found.add(name)
    parser=Links();parser.feed(reader('https://pypi.org/simple/pliqra-client/').decode('utf-8'));parser.close()
    return found==expected


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('registry',choices=['pypi','npm'])
    parser.add_argument('directory',type=Path)
    parser.add_argument('--github-output',type=Path)
    parser.add_argument('--require-existing',action='store_true')
    parser.add_argument('--wait-seconds',type=int,default=0)
    parser.add_argument('--require-install-index',action='store_true',help='Also require matching PyPI installer index links before a canonical pip install')
    args=parser.parse_args()
    if not 0<=args.wait_seconds<=300:parser.error('Registry visibility wait must be within 0–300 seconds')
    if args.require_install_index and (args.registry!='pypi' or not args.require_existing):parser.error('Installer index verification requires pypi and --require-existing')
    deadline=time.monotonic()+args.wait_seconds
    while True:
        result=verify(args.registry,args.directory)
        if result['already_published'] and args.require_install_index:result['install_index_ready']=python_index_ready(args.directory)
        ready=result['already_published'] and (not args.require_install_index or result.get('install_index_ready'))
        if ready or not args.require_existing:break
        if time.monotonic()>=deadline:raise SystemExit('Published SDK files or installer index are not publicly available')
        time.sleep(min(3,max(0,deadline-time.monotonic())))
    if args.github_output:
        with args.github_output.open('a') as output:output.write('already_published='+str(result['already_published']).lower()+'\n')
    print(json.dumps(result))
