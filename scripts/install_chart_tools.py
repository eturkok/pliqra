"""Install checksum-pinned official test tools inside a caller-owned directory."""
import argparse
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile

HELM_VERSION = 'v4.3.1'
KIND_VERSION = 'v0.33.0'
HELM_SHA256 = {
    ('Darwin', 'arm64'): '6b4e61c8cd27d99e5a801e63ff25dd5c70b36875c2cd659d3e9ce6b76fc23198',
    ('Linux', 'amd64'): '0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b',
}
KIND_SHA256 = 'aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d'
KUBECTL_SHA256 = {
    'v1.34.11': '8efbb9435132a190920eb65a47a8c1ecf755ad85ab57a600c9bedbab460bb7a8',
    'v1.37.0': '6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f',
}


def download(url, destination, expected):
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location',
                    '--retry', '2', '--max-time', '120', '--output', str(destination), url], check=True)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Official tool checksum did not match the pinned value')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--kind', action='store_true')
    parser.add_argument('--kubectl-version', choices=list(KUBECTL_SHA256))
    args = parser.parse_args()
    system = platform.system()
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    if (system, arch) not in HELM_SHA256:
        parser.error('Test-tool installer supports Linux AMD64 and macOS ARM64')
    if (args.kind or args.kubectl_version) and (system, arch) != ('Linux', 'amd64'):
        parser.error('The disposable Kubernetes CI environment uses Linux AMD64')
    destination = args.directory.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pliqra-chart-tools-', dir=destination) as directory:
        temporary = Path(directory)
        prefix = system.lower() + '-' + arch
        archive = temporary / 'helm.tgz'
        download(f'https://get.helm.sh/helm-{HELM_VERSION}-{prefix}.tar.gz', archive, HELM_SHA256[(system, arch)])
        with tarfile.open(archive) as package:
            member = package.getmember(prefix + '/helm')
            if not member.isfile():
                raise RuntimeError('Expected a regular Helm binary')
            with package.extractfile(member) as source, (destination / 'helm').open('wb') as output:
                shutil.copyfileobj(source, output)
        (destination / 'helm').chmod(0o755)
        if args.kind:
            download(f'https://github.com/kubernetes-sigs/kind/releases/download/{KIND_VERSION}/kind-linux-amd64',
                     destination / 'kind', KIND_SHA256)
            (destination / 'kind').chmod(0o755)
        if args.kubectl_version:
            download(f'https://dl.k8s.io/release/{args.kubectl_version}/bin/linux/amd64/kubectl',
                     destination / 'kubectl', KUBECTL_SHA256[args.kubectl_version])
            (destination / 'kubectl').chmod(0o755)
    print(f'Installed checksum-verified Helm {HELM_VERSION}' + (f' and kind {KIND_VERSION}' if args.kind else ''))


if __name__ == '__main__':
    main()
