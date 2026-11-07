"""Validate canonical SDK versions and select the publication channel."""
import json
from pathlib import Path
import re
import tomllib


def metadata(root: Path) -> dict:
    version = (root / 'sdk/VERSION').read_text().strip()
    match = re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-rc\.([1-9][0-9]*))?', version)
    if not match:
        raise ValueError('Invalid canonical SDK version')
    python_version = version.replace('-rc.', 'rc')
    python = tomllib.loads((root / 'packaging/python/pyproject.toml').read_text())['project']['version']
    javascript = json.loads((root / 'packaging/javascript/package.json').read_text())
    channel = 'next' if match[4] else 'latest'
    if python != python_version or javascript['version'] != version:
        raise ValueError('SDK language versions differ from sdk/VERSION')
    if javascript['publishConfig']['tag'] != channel:
        raise ValueError('SDK npm publication channel differs from its version')
    return {'sdk_version': version, 'python_version': python_version, 'npm_tag': channel}


def release(tag, root):
    result = metadata(root)
    if tag != 'sdk-v' + result['sdk_version']:
        raise ValueError('SDK release tag differs from the canonical SDK version')
    notes = 'docs/releases/' + tag + '.md'
    if not (root / notes).is_file() or not (root / notes).read_text().strip():
        raise ValueError('Missing SDK release notes')
    return {**result, 'notes': notes, 'prerelease': 'true' if result['npm_tag'] == 'next' else 'false'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--github-output', type=Path)
    parser.add_argument('--release-tag')
    args = parser.parse_args()
    result = release(args.release_tag, args.source) if args.release_tag else metadata(args.source)
    if args.github_output:
        with args.github_output.open('a') as output:
            output.write(''.join(f'{key}={value}\n' for key, value in result.items()))
    print(json.dumps(result))
