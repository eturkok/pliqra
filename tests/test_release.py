"""Publishing boundaries: reject ambiguous tags and mismatched installation files."""
from pathlib import Path
import pytest
from scripts.release_metadata import metadata
from scripts.sdk_metadata import metadata as sdk_metadata
from scripts.sdk_metadata import release as sdk_release
from scripts.fetch_sdk_release import validate_tag, validate_source
from scripts.customer_acceptance import validate_stable
import copy
import json
import subprocess


def release_tree(tmp_path: Path, version: str) -> Path:
    (tmp_path / "pliqra").mkdir()
    (tmp_path / "pliqra/__init__.py").write_text(f'__version__ = "{version}"\n')
    (tmp_path / "docs/releases").mkdir(parents=True)
    (tmp_path / f"docs/releases/v{version}.md").write_text("Release notes\n")
    (tmp_path / "compose.release.yaml").write_text("image: ${PLIQRA_IMAGE:-ghcr.io/eturkok/pliqra:" + version + "}\n")
    (tmp_path / 'sdk').mkdir()
    (tmp_path / 'sdk/VERSION').write_text('0.1.0\n')
    (tmp_path / 'packaging/python').mkdir(parents=True)
    (tmp_path / 'packaging/python/pyproject.toml').write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / 'packaging/javascript').mkdir(parents=True)
    (tmp_path / 'packaging/javascript/package.json').write_text(json.dumps({'version': '0.1.0', 'publishConfig': {'tag': 'latest'}}))
    return tmp_path


@pytest.mark.parametrize("version,prerelease", [("2.1.0", "false"), ("2.1.0-rc.1", "true"), ("2.1.0-beta.2", "true")])
def test_release_classification(tmp_path, version, prerelease):
    result = metadata("v" + version, release_tree(tmp_path, version))
    assert result["version"] == version and result["prerelease"] == prerelease


@pytest.mark.parametrize("tag", ["2.1.0", "v2.1", "v02.1.0", "v2.1.0-rc.0", "v2.1.0-rc.01", "v2.1.0\nlatest=true"])
def test_invalid_release_tag_never_reaches_filesystem(tag, tmp_path):
    with pytest.raises(ValueError, match="Expected"):
        metadata(tag, tmp_path)


def test_release_requires_matching_version_notes_and_installation(tmp_path):
    root = release_tree(tmp_path, "2.1.0-rc.1")
    with pytest.raises(ValueError, match="does not match"):
        metadata("v2.1.0", root)
    notes = root / "docs/releases/v2.1.0-rc.1.md"
    notes.unlink()
    with pytest.raises(ValueError, match="Missing release notes"):
        metadata("v2.1.0-rc.1", root)
    notes.write_text("Notes")
    (root / "compose.release.yaml").write_text("image: ${PLIQRA_IMAGE:-ghcr.io/eturkok/pliqra:2.1.0-rc.10}\n")
    with pytest.raises(ValueError, match="exact release version"):
        metadata("v2.1.0-rc.1", root)


def set_sdk(root, version, channel):
    (root / 'sdk/VERSION').write_text(version)
    (root / 'packaging/python/pyproject.toml').write_text('[project]\nversion = "' + version.replace('-rc.', 'rc') + '"\n')
    (root / 'packaging/javascript/package.json').write_text(json.dumps({'version': version, 'publishConfig': {'tag': channel}}))


def test_stable_gateway_rejects_prerelease_sdk(tmp_path):
    root = release_tree(tmp_path, '2.1.0')
    set_sdk(root, '0.1.0-rc.2', 'next')
    with pytest.raises(ValueError, match='stable packaged SDK'):
        metadata('v2.1.0', root)


def test_sdk_channels_follow_canonical_versions_and_reject_mismatch(tmp_path):
    root = release_tree(tmp_path, '2.1.0')
    assert sdk_metadata(root)['npm_tag'] == 'latest'
    set_sdk(root, '0.1.0-rc.2', 'next')
    assert sdk_metadata(root)['npm_tag'] == 'next'
    set_sdk(root, '0.1.0', 'next')
    with pytest.raises(ValueError, match='publication channel'):
        sdk_metadata(root)
    set_sdk(root, '0.1.0', 'latest')
    (root / 'sdk/VERSION').write_text('0.2.0')
    with pytest.raises(ValueError, match='language versions differ'):
        sdk_metadata(root)


def test_independent_sdk_release_requires_matching_version_and_notes(tmp_path):
    root = release_tree(tmp_path, '2.1.0')
    with pytest.raises(ValueError, match='canonical SDK version'):
        sdk_release('sdk-v0.2.0', root)
    with pytest.raises(ValueError, match='Missing SDK release notes'):
        sdk_release('sdk-v0.1.0', root)
    (root / 'docs/releases/sdk-v0.1.0.md').write_text('SDK release')
    assert sdk_release('sdk-v0.1.0', root)['prerelease'] == 'false'


@pytest.mark.parametrize('tag', ['main', 'sdk-v0.1', 'v2.1.0\nnext=true', 'sdk-v0.1.0/../../main'])
def test_sdk_fetch_rejects_non_release_refs(tag):
    with pytest.raises(ValueError, match='published gateway or SDK release tag'):
        validate_tag(tag)


def test_reused_sdk_requires_original_revision_and_unchanged_build_inputs(tmp_path):
    root = release_tree(tmp_path, '2.2.2')
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Release Test', '-c', 'user.email=release@example.invalid', 'commit', '-qm', 'Original SDK')
    original = git('rev-parse', 'HEAD')
    git('tag', 'v2.2.1')
    manifest = {'sdk_version': '0.1.0', 'revision': original}
    (root / 'pliqra/__init__.py').write_text('__version__ = "2.2.3"\n')
    git('add', '.')
    git('-c', 'user.name=Release Test', '-c', 'user.email=release@example.invalid', 'commit', '-qm', 'Gateway only')
    with pytest.raises(ValueError, match='checked-out release source'):
        validate_source('v2.2.1', manifest, root)
    validate_source('v2.2.1', manifest, root, allow_unchanged_source=True)
    with pytest.raises(ValueError, match='original release revision'):
        validate_source('v2.2.1', dict(manifest, revision=git('rev-parse', 'HEAD')), root, True)
    (root / 'packaging/javascript/package.json').write_text('{"changed": true}')
    with pytest.raises(ValueError, match='SDK build inputs changed'):
        validate_source('v2.2.1', manifest, root, True)


@pytest.mark.parametrize('origin', ['main', 'v2.2.2', 'v2.2.1\nextra=true'])
def test_reused_sdk_release_rejects_ambiguous_or_self_references(tmp_path, origin):
    root = release_tree(tmp_path, '2.2.2')
    (root / 'release-policy.json').write_text(json.dumps({'sdk_release': origin}))
    with pytest.raises(ValueError):
        metadata('v2.2.2', root)


def stable_report():
    policy = {'soak_seconds': 1800, 'minimum_soak_requests': 8000, 'maximum_p95_ms': 2000,
              'maximum_rss_growth_kib': 65536}
    phase = {'requests': 9000, 'statuses': {'200': 9000}, 'p95_ms': 100,
             'duration_seconds': 1800, 'gateway_west_rss_kib': [100000, 101000]}
    return {'load': copy.deepcopy(phase), 'soak': copy.deepcopy(phase)}, policy


@pytest.mark.parametrize('failure', ['errors', 'latency', 'duration', 'count', 'memory', 'samples', 'missing_load'])
def test_stable_acceptance_rejects_incomplete_or_unhealthy_evidence(failure):
    report, policy = stable_report()
    validate_stable(report, policy)
    if failure == 'errors':report['soak']['statuses']['500'] = 1
    elif failure == 'latency':report['load']['p95_ms'] = 2001
    elif failure == 'duration':report['soak']['duration_seconds'] = 1799
    elif failure == 'count':report['soak']['requests'] = 7999; report['soak']['statuses'] = {'200': 7999}
    elif failure == 'memory':report['soak']['gateway_west_rss_kib'] = [100000, 165537]
    elif failure == 'samples':report['soak']['gateway_west_rss_kib'] = [100000]
    else:del report['load']
    with pytest.raises((AssertionError, KeyError)):
        validate_stable(report, policy)
