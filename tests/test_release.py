"""Publishing boundaries: reject ambiguous tags and mismatched installation files."""
from pathlib import Path
import pytest
from scripts.release_metadata import metadata
from scripts.sdk_metadata import metadata as sdk_metadata
from scripts.customer_acceptance import validate_stable
import copy
import json


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
