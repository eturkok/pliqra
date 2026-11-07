"""Clean-install an SDK, tolerating only bounded new-version index propagation."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time


def install(command, registry, cwd=None, wait_seconds=300, runner=subprocess.run,
            clock=time.monotonic, sleep=time.sleep):
    if registry not in ('pypi','npm') or not 0<=wait_seconds<=300:
        raise ValueError('Invalid registry installation bounds')
    deadline=clock()+wait_seconds
    while True:
        result=runner(command,cwd=cwd,capture_output=True,text=True,timeout=60)
        output=result.stdout+result.stderr
        if result.returncode==0:
            print(output,end='')
            return
        unavailable=('No matching distribution found for pliqra-client==' in output if registry=='pypi'
                     else bool(re.search(r'npm (?:error|ERR!) code ETARGET',output)))
        if re.search(r'CERTIFICATE_VERIFY_FAILED|\bSSL\b|\bTLS\b|\bE401\b|\bE403\b|\bEINTEGRITY\b|(?:401|403) (?:Client Error|Unauthorized|Forbidden)|hash mismatch|DO NOT MATCH THE HASHES',output,re.I):
            unavailable=False
        if not unavailable or clock()>=deadline:
            print(output,file=sys.stderr,end='')
            raise subprocess.CalledProcessError(result.returncode,command)
        print('New SDK version is not visible to the canonical installer yet; retrying within the fixed visibility bound.',flush=True)
        sleep(min(10,max(0,deadline-clock())))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('registry',choices=['pypi','npm'])
    parser.add_argument('manifest_directory',type=Path)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--directory',type=Path)
    args=parser.parse_args()
    manifest=json.loads((args.manifest_directory/'SDK-MANIFEST.json').read_text())
    version=manifest['sdk_version']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-rc\.[1-9][0-9]*)?',version) or version.replace('-rc.','rc')!=manifest['python_version']:
        parser.error('Invalid SDK manifest versions')
    if args.registry=='pypi':
        command=[args.python,'-m','pip','--isolated','install','--index-url','https://pypi.org/simple',
                 '--no-cache-dir','--no-deps','pliqra-client=='+manifest['python_version']]
    else:
        if args.directory is None:parser.error('npm requires an independent consumer directory')
        command=['npm','install','--ignore-scripts','--no-audit','--no-fund','--prefer-online',
                 '--registry','https://registry.npmjs.org','@eturkok/pliqra@'+version]
    install(command,args.registry,cwd=args.directory)
