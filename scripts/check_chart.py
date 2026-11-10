"""Exercise chart safety boundaries with the actual Helm renderer."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helm', default='helm')
    args = parser.parse_args()
    chart = ROOT / 'charts/pliqra'
    checks = []

    def render(values=None, reject=None):
        with tempfile.TemporaryDirectory(prefix='pliqra-chart-render-') as directory:
            path = Path(directory) / 'values.json'
            path.write_text(json.dumps(values or {}))
            result = subprocess.run([args.helm, 'template', 'pliqra', str(chart), '--namespace', 'chart-check',
                                     '--kube-version', '1.34.11', '-f', str(path)], text=True, capture_output=True)
        if reject:
            assert result.returncode != 0 and reject.lower() in result.stderr.lower(), result.stderr
            checks.append('rejected ' + reject)
            return []
        assert result.returncode == 0, result.stderr
        return [x for x in yaml.safe_load_all(result.stdout) if x]

    def get(objects, kind):
        return next(x for x in objects if x['kind'] == kind)

    plain = render()
    deployment = get(plain, 'Deployment')
    pod = deployment['spec']['template']['spec']
    container = pod['containers'][0]
    assert deployment['spec']['replicas'] == 1 and deployment['spec']['strategy']['type'] == 'Recreate'
    assert get(plain, 'PersistentVolumeClaim')['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
    assert not pod['automountServiceAccountToken']
    assert container['securityContext']['readOnlyRootFilesystem']
    assert container['readinessProbe']['httpGet']['path'] == '/readyz'
    assert container['livenessProbe']['httpGet']['path'] == '/livez'
    assert '@sha256:' in container['image']
    hook = get(plain, 'Pod')
    assert hook['metadata']['labels']['app.kubernetes.io/name'] != deployment['spec']['selector']['matchLabels']['app.kubernetes.io/name']
    assert not any(x['kind'] in ('Secret', 'Ingress', 'HorizontalPodAutoscaler') for x in plain)
    checks.append('single SQLite writer, retained PVC, pinned image and separate probes')
    existing = render({'persistence': {'existingClaim': 'retained'}})
    assert not any(x['kind'] == 'PersistentVolumeClaim' for x in existing)
    assert get(existing, 'Deployment')['spec']['template']['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] == 'retained'
    checks.append('existing PVC is referenced without taking ownership')
    pg = {'storage': {'mode': 'postgresql'}, 'replicaCount': 2,
          'database': {'existingSecret': 'backend', 'tls': {'existingSecret': 'db-ca'}},
          'encryption': {'existingSecret': 'backend'}}
    objects = render(pg)
    pod = get(objects, 'Deployment')['spec']['template']['spec']
    env = {x['name']: x for x in pod['containers'][0]['env']}
    assert env['PLIQRA_DATABASE_URL']['valueFrom']['secretKeyRef']['name'] == 'backend'
    assert env['PLIQRA_ENCRYPTION_KEY']['valueFrom']['secretKeyRef']['name'] == 'backend'
    assert pod['volumes'][0]['emptyDir'] == {} and not any(x['kind'] == 'PersistentVolumeClaim' for x in objects)
    checks.append('PostgreSQL uses existing database/key Secrets and independent scratch space')
    advanced = {**pg, 'topology': {'multiDC': True, 'site': 'west', 'region': 'eu'},
                'autoscaling': {'enabled': True}, 'podDisruptionBudget': {'enabled': True},
                'networkPolicy': {'enabled': True},
                'config': {'publicUrl': 'https://models.example.test', 'secureCookies': True},
                'ingress': {'enabled': True, 'host': 'models.example.test', 'tlsSecret': 'web-tls'}}
    objects = render(advanced)
    assert {'Ingress', 'HorizontalPodAutoscaler', 'PodDisruptionBudget', 'NetworkPolicy'} <= {x['kind'] for x in objects}
    assert 'replicas' not in get(objects, 'Deployment')['spec']
    checks.append('multi-DC, HTTPS ingress, HPA, PDB and network policy render together')
    for values, message in [
        ({'replicaCount': 2}, 'SQLite requires'),
        ({'autoscaling': {'enabled': True}}, 'SQLite cannot'),
        ({'topology': {'multiDC': True}}, 'Multi-DC requires'),
        ({'storage': {'mode': 'postgresql'}}, 'database.existingSecret'),
        ({'storage': {'mode': 'postgresql'}, 'database': {'existingSecret': 'backend'}}, 'encryption.existingSecret'),
        ({'ingress': {'enabled': True}}, 'ingress.host'),
        ({**advanced, 'config': {'publicUrl': 'http://models.example.test', 'secureCookies': False}}, 'config.publicUrl'),
        ({**advanced, 'config': {'publicUrl': 'https://models.example.test', 'secureCookies': False}}, 'secureCookies'),
        ({'extraEnv': [{'name': 'PLIQRA_DATABASE_URL', 'value': 'not-allowed'}]}, 'managed or duplicate'),
        ({'extraEnv': [{'name': 'MODELPORT_PORT', 'value': '80'}]}, 'managed or duplicate'),
        ({'extraEnv': [{'name': 'CUSTOM', 'value': 'a'}, {'name': 'CUSTOM', 'value': 'b'}]}, 'managed or duplicate'),
        ({'replicas': 2}, 'additional'),
        ({'database': {'url': 'inline-secret-is-not-supported'}}, 'additional'),
        ({'topology': {'site': 'shared'}}, 'not'),
        ({'extraVolumes': [{'name': 'data', 'emptyDir': {}}]}, 'reserved volume'),
    ]:
        render(values, reject=message)
    metadata = yaml.safe_load((chart / 'Chart.yaml').read_text())
    defaults = yaml.safe_load((chart / 'values.yaml').read_text())
    assert metadata['appVersion'] == defaults['image']['tag']
    print(json.dumps({'passed': True, 'checks': checks}, indent=2))


if __name__ == '__main__':
    main()
