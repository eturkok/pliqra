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
    parser.add_argument('--rules-directory', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    chart = ROOT / 'charts/pliqra'
    checks = []

    def render(values=None, reject=None, api_versions=(), kube_version='1.33.12'):
        with tempfile.TemporaryDirectory(prefix='pliqra-chart-render-') as directory:
            path = Path(directory) / 'values.json'
            path.write_text(json.dumps(values or {}))
            command = [args.helm, 'template', 'pliqra', str(chart), '--namespace', 'chart-check',
                       '--kube-version', kube_version, '-f', str(path)]
            for version in api_versions:
                command.extend(['--api-versions', version])
            result = subprocess.run(command, text=True, capture_output=True)
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
    release = json.loads((ROOT / 'docs/evidence/stable-2.5.0.json').read_text())['release']
    assert container['image'] == release['image'] + '@' + release['digest']
    assert pod['securityContext']['runAsUser'] == pod['securityContext']['fsGroup'] == 10001
    hook = get(plain, 'Pod')
    assert hook['metadata']['labels']['app.kubernetes.io/name'] != deployment['spec']['selector']['matchLabels']['app.kubernetes.io/name']
    assert not any(x['kind'] in ('Secret', 'Ingress', 'Route', 'HorizontalPodAutoscaler', 'SecurityContextConstraints', 'ClusterRole', 'ClusterRoleBinding') for x in plain)
    checks.append('single SQLite writer, retained PVC, pinned image and separate probes')
    render(kube_version='1.32.0', reject='kubeVersion')
    for version in ('1.34.11', '1.37.0'):
        assert get(render(kube_version=version), 'Deployment')['spec']['template']['spec']['securityContext'] == pod['securityContext']
    checks.append('Kubernetes 1.33 baseline accepts OpenShift 4.20 and retains 1.34/1.37 Kubernetes defaults')
    route_api = ['route.openshift.io/v1/Route']
    exposed = {'route': {'enabled': True, 'host': 'models.example.test',
                         'annotations': {'haproxy.router.openshift.io/timeout': '120s'}},
               'config': {'publicUrl': 'https://models.example.test', 'secureCookies': True}}
    routed = render({**exposed, 'fullnameOverride': 'custom-gateway'}, api_versions=route_api)
    route = get(routed, 'Route')
    assert route['spec']['host'] == 'models.example.test'
    assert route['spec']['to'] == {'kind': 'Service', 'name': get(routed, 'Service')['metadata']['name'], 'weight': 100}
    assert route['spec']['port']['targetPort'] == get(routed, 'Service')['spec']['ports'][0]['name'] == 'http'
    assert route['spec']['tls'] == {'termination': 'edge', 'insecureEdgeTerminationPolicy': 'Redirect'}
    assert route['metadata']['annotations']['haproxy.router.openshift.io/timeout'] == '120s'
    assert not any(x['kind'] in ('Secret', 'Ingress') for x in routed)
    checks.append('opt-in HTTPS edge Route targets the private named Service port and uses the router certificate')
    no_http = render({**exposed, 'route': {**exposed['route'], 'tls': {'insecureEdgeTerminationPolicy': 'None'}}}, api_versions=route_api)
    assert get(no_http, 'Route')['spec']['tls']['insecureEdgeTerminationPolicy'] == 'None'
    checks.append('Route can disable insecure HTTP instead of redirecting it')
    # Overlay adversarial/custom fixed identities too: Helm merges maps, so
    # clearing only the default pod UID is insufficient for a reliable profile.
    adapted = render({'openshift': {'enabled': True},
                      'podSecurityContext': {'runAsUser': 1234, 'runAsGroup': 1234, 'fsGroup': 1234,
                                             'supplementalGroups': [1234], 'seLinuxOptions': {'level': 's0'},
                                             'runAsNonRoot': False, 'seccompProfile': {'type': 'Unconfined'}},
                      'securityContext': {'runAsUser': 1234, 'runAsGroup': 1234, 'seLinuxOptions': {'level': 's0'},
                                          'privileged': True, 'allowPrivilegeEscalation': True,
                                          'readOnlyRootFilesystem': False, 'capabilities': {'add': ['SYS_ADMIN']},
                                          'procMount': 'Unmasked', 'seccompProfile': {'type': 'Unconfined'}}})
    for spec in (get(adapted, 'Deployment')['spec']['template']['spec'], get(adapted, 'Pod')['spec']):
        context = spec['securityContext']
        assert not {'runAsUser', 'runAsGroup', 'fsGroup', 'supplementalGroups', 'seLinuxOptions'} & context.keys()
        assert context['runAsNonRoot'] and context['seccompProfile'] == {'type': 'RuntimeDefault'}
        assert not spec['automountServiceAccountToken']
        for c in spec['containers']:
            context = c['securityContext']
            assert not {'runAsUser', 'runAsGroup', 'seLinuxOptions', 'procMount'} & context.keys()
            assert context['runAsNonRoot'] and context['readOnlyRootFilesystem']
            assert not context['allowPrivilegeEscalation'] and not context['privileged']
            assert context['capabilities'] == {'drop': ['ALL']}
            assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    assert not any(x['kind'] in ('SecurityContextConstraints', 'ClusterRole', 'ClusterRoleBinding') for x in adapted)
    checks.append('restricted-v2 profile delegates identities and SELinux, hardens Deployment and Helm hook without SCC grants')
    example = yaml.safe_load((chart / 'examples/openshift.yaml').read_text())
    assert not any(x['kind'] == 'Route' for x in render(example))
    checks.append('OpenShift example starts privately with no Route and needs no Route API for bootstrap')
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
    together = render({**pg, **exposed, 'openshift': {'enabled': True}}, api_versions=route_api)
    assert get(together, 'Deployment')['spec']['replicas'] == 2
    assert 'fsGroup' not in get(together, 'Pod')['spec']['securityContext']
    checks.append('OpenShift Route and restricted-v2 profile compose with shared PostgreSQL replicas')
    network = yaml.safe_load((chart / 'examples/openshift-networkpolicy.yaml').read_text())
    policy = get(render({**example, **network}), 'NetworkPolicy')['spec']
    assert policy['podSelector']['matchLabels'] == deployment['spec']['selector']['matchLabels']
    assert policy['policyTypes'] == ['Ingress']
    router = policy['ingress'][0]['from'][0]
    assert router == {'namespaceSelector': {'matchLabels': {'policy-group.network.openshift.io/ingress': ''}}, 'podSelector': {}}
    assert policy['ingress'][1]['from'][0]['podSelector']['matchLabels']['app.kubernetes.io/component'] == 'test'
    assert policy['ingress'][2]['from'][0]['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'] == 'openshift-user-workload-monitoring'
    assert all(rule['ports'] == [{'protocol': 'TCP', 'port': 8080}] for rule in policy['ingress'])
    checks.append('OpenShift network example scopes router, health hook and workload collectors without changing model/egress policy')
    recovery = yaml.safe_load((chart / 'examples/openshift-recovery-pod.yaml').read_text())['spec']
    assert not {'runAsUser', 'runAsGroup', 'fsGroup', 'seLinuxOptions'} & recovery['securityContext'].keys()
    assert recovery['securityContext']['runAsNonRoot'] and not recovery['automountServiceAccountToken']
    assert recovery['containers'][0]['image'] == container['image']
    assert recovery['containers'][0]['securityContext']['readOnlyRootFilesystem']
    assert recovery['volumes'][0]['persistentVolumeClaim']['claimName'] == 'pliqra'
    checks.append('restricted-v2 recovery example uses the same pinned image and delegates volume identity without starting a server')
    advanced = {**pg, 'topology': {'multiDC': True, 'site': 'west', 'region': 'eu'},
                'autoscaling': {'enabled': True}, 'podDisruptionBudget': {'enabled': True},
                'networkPolicy': {'enabled': True},
                'config': {'publicUrl': 'https://models.example.test', 'secureCookies': True},
                'ingress': {'enabled': True, 'host': 'models.example.test', 'tlsSecret': 'web-tls'}}
    objects = render(advanced)
    assert {'Ingress', 'HorizontalPodAutoscaler', 'PodDisruptionBudget', 'NetworkPolicy'} <= {x['kind'] for x in objects}
    assert 'replicas' not in get(objects, 'Deployment')['spec']
    checks.append('multi-DC, HTTPS ingress, HPA, PDB and network policy render together')
    spread = [{'maxSkew': 1, 'topologyKey': 'topology.kubernetes.io/zone',
               'whenUnsatisfiable': 'DoNotSchedule', 'minDomains': 2}]
    changed = render({**pg, 'config': {'revision': 'secret-2'}, 'topologySpreadConstraints': spread})
    template = get(changed, 'Deployment')['spec']['template']
    constraint = template['spec']['topologySpreadConstraints'][0]
    assert constraint['labelSelector']['matchLabels'] == deployment['spec']['selector']['matchLabels']
    assert constraint['minDomains'] == 2 and template['metadata']['annotations']['pliqra.io/config-revision'] == 'secret-2'
    assert get(objects, 'PodDisruptionBudget')['spec']['unhealthyPodEvictionPolicy'] == 'AlwaysAllow'
    checks.append('release-scoped topology spread, controlled Secret revision and unhealthy pod eviction')
    monitoring = {'monitoring': {'enabled': True, 'existingSecret': 'monitoring-token',
                                'prometheusRule': {'enabled': True}}}
    apis = ['monitoring.coreos.com/v1/ServiceMonitor', 'monitoring.coreos.com/v1/PrometheusRule']
    monitored = render(monitoring, api_versions=apis)
    monitor = get(monitored, 'ServiceMonitor')['spec']
    assert monitor['selector']['matchLabels'] == get(monitored, 'Service')['spec']['selector']
    assert monitor['namespaceSelector']['matchNames'] == ['chart-check']
    assert monitor['endpoints'][0]['authorization']['credentials'] == {'name': 'monitoring-token', 'key': 'token'}
    rules = get(monitored, 'PrometheusRule')['spec']
    assert len(rules['groups'][0]['rules']) == 3
    if args.rules_directory:
        args.rules_directory.mkdir(parents=True, exist_ok=True)
        (args.rules_directory / 'pliqra.rules.yaml').write_text(yaml.safe_dump(rules))
    checks.append('optional monitoring uses a scoped Secret reference and workspace-deduplicated alerts')
    render({'monitoring': {'enabled': True}}, reject='monitoring.existingSecret')
    render(monitoring, reject='ServiceMonitor CRD')
    render(monitoring, reject='PrometheusRule CRD', api_versions=apis[:1])
    render({'route': {'enabled': True}}, reject='Route requires route.host')
    render(exposed, reject='OpenShift Route API')
    render({**exposed, 'config': {'publicUrl': 'https://other.example.test', 'secureCookies': True}}, reject='config.publicUrl', api_versions=route_api)
    render({**exposed, 'config': {'publicUrl': 'https://models.example.test', 'secureCookies': False}}, reject='secureCookies', api_versions=route_api)
    render({**exposed, 'ingress': {'enabled': True}}, reject='Choose either', api_versions=route_api)
    render({'route': {'tls': {'termination': 'passthrough'}}}, reject='edge')
    render({'route': {'tls': {'termination': 'reencrypt'}}}, reject='edge')
    render({'route': {'tls': {'insecureEdgeTerminationPolicy': 'Allow'}}}, reject='Redirect')
    render({'route': {'host': 'https://bad.example.test/path'}}, reject='pattern')
    render({'route': {'tls': {'key': 'inline-private-key'}}}, reject='additional')
    render({'openshift': {'enabled': 'true'}}, reject='boolean')
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
        ({'podDisruptionBudget': {'enabled': True}}, 'PDB minAvailable'),
        ({**pg, 'autoscaling': {'enabled': True, 'minReplicas': 1}, 'podDisruptionBudget': {'enabled': True}}, 'PDB minAvailable'),
        ({'topologySpreadConstraints': [{**spread[0], 'whenUnsatisfiable': 'ScheduleAnyway'}]}, 'minDomains requires'),
        ({'monitoring': {'prometheusRule': {'enabled': True}}}, 'requires monitoring.enabled'),
        ({'monitoring': {'enabled': True, 'existingSecret': 'token', 'scrapeTimeout': '60s'}}, 'scrapeTimeout cannot exceed'),
        ({'monitoring': {'labels': {'app.kubernetes.io/instance': 'other'}}}, 'managed chart labels'),
        ({'podAnnotations': {'pliqra.io/config-revision': 'other'}}, 'Use config.revision'),
        ({'extraVolumeMounts': [{'name': 'other', 'mountPath': '/data/key'}]}, 'shadow managed paths'),
        ({'extraVolumeMounts': [{'name': 'other', 'mountPath': '/run'}]}, 'shadow managed paths'),
        ({'extraVolumeMounts': [{'name': 'other', 'mountPath': '/app'}]}, 'shadow managed paths'),
        ({'extraVolumeMounts': [{'name': 'other', 'mountPath': '/data/../tmp'}]}, 'shadow managed paths'),
    ]:
        render(values, reject=message)
    metadata = yaml.safe_load((chart / 'Chart.yaml').read_text())
    defaults = yaml.safe_load((chart / 'values.yaml').read_text())
    assert metadata['appVersion'] == defaults['image']['tag']
    report = {'passed': True, 'chart_version': metadata['version'], 'app_version': metadata['appVersion'], 'checks': checks}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
