"""Fetch tested SDKs from a published gateway or independent SDK release."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def validate_tag(tag):
    if not re.fullmatch(r'(?:sdk-)?v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)\.[1-9][0-9]*)?', tag):
        raise ValueError('Use a published gateway or SDK release tag')


def fetch(tag, output, source=ROOT):
    validate_tag(tag)
    release = json.loads(subprocess.check_output(['gh', 'release', 'view', tag, '--repo', 'eturkok/pliqra', '--json', 'tagName,isDraft'], text=True))
    if release['isDraft'] or release['tagName'] != tag:
        raise ValueError('Release must be published')
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(['gh', 'release', 'download', tag, '--repo', 'eturkok/pliqra', '--pattern', 'SDK-MANIFEST.json', '--dir', str(output)], check=True)
    manifest = json.loads((output / 'SDK-MANIFEST.json').read_text())
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if manifest['revision'] != revision or manifest['sdk_version'] != (source / 'sdk/VERSION').read_text().strip():
        raise ValueError('SDK manifest differs from the checked-out release source')
    if tag.startswith('sdk-v') and tag[5:] != manifest['sdk_version']:
        raise ValueError('SDK release tag differs from its manifest version')
    expected = {f"pliqra_client-{manifest['python_version']}-py3-none-any.whl", f"pliqra_client-{manifest['python_version']}.tar.gz", f"eturkok-pliqra-{manifest['sdk_version']}.tgz"}
    if {item['file'] for item in manifest['artifacts']} != expected:
        raise ValueError('SDK artifact set is incomplete or unexpected')
    for item in manifest['artifacts']:
        name = item['file']
        if Path(name).name != name or not re.fullmatch(r'[a-zA-Z0-9_.-]+', name):
            raise ValueError('Invalid SDK filename')
        subprocess.run(['gh', 'release', 'download', tag, '--repo', 'eturkok/pliqra', '--pattern', name, '--dir', str(output)], check=True)
        source = output / name
        if hashlib.sha256(source.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('SDK download checksum mismatch')
        target = output / ('npm' if name.endswith('.tgz') else 'python')
        target.mkdir(exist_ok=True)
        shutil.copy2(source, target / name)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    parser.add_argument('--output', type=Path, default=Path('dist/publish'))
    parser.add_argument('--source', type=Path, default=ROOT, help='Separate checkout of the immutable release source')
    args = parser.parse_args()
    print(json.dumps(fetch(args.tag, args.output, args.source)))
