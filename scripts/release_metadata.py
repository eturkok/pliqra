"""Validate a release tag before granting publishing jobs access to credentials."""
import argparse
import ast
import json
from pathlib import Path
import re
try:
    from scripts.sdk_metadata import metadata as sdk_metadata
    from scripts.fetch_sdk_release import validate_tag
except ModuleNotFoundError:
    from sdk_metadata import metadata as sdk_metadata
    from fetch_sdk_release import validate_tag

ROOT = Path(__file__).resolve().parents[1]
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(alpha|beta|rc)\.([1-9][0-9]*))?")


def metadata(tag: str, root: Path = ROOT) -> dict:
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError("Expected vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-{alpha,beta,rc}.N")
    version = tag[1:]
    assignments = ast.parse((root / "pliqra/__init__.py").read_text()).body
    actual = next(ast.literal_eval(node.value) for node in assignments
                  if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets))
    if version != actual:
        raise ValueError(f"Tag version {version} does not match application version {actual}")
    notes = f"docs/releases/{tag}.md"
    if not (root / notes).is_file() or not (root / notes).read_text().strip():
        raise ValueError(f"Missing release notes: {notes}")
    compose = (root / "compose.release.yaml").read_text()
    image = re.search(r"^\s*image:\s*\$\{PLIQRA_IMAGE:-([^}]+)\}\s*$", compose, re.MULTILINE)
    if not image or image[1] != f"ghcr.io/eturkok/pliqra:{version}":
        raise ValueError("Prebuilt Compose must default to the exact release version")
    sdk = sdk_metadata(root)
    if not match[4] and sdk['npm_tag'] != 'latest':
        raise ValueError('A stable gateway release requires a stable packaged SDK')
    policy = root / 'release-policy.json'
    sdk_release = json.loads(policy.read_text()).get('sdk_release', '') if policy.exists() else ''
    if sdk_release:
        validate_tag(sdk_release)
        if sdk_release == tag:
            raise ValueError('Reused SDKs must come from an already published release')
    return {"tag": tag, "version": version, "prerelease": "true" if match[4] else "false", "notes": notes,
            "sdk_version": sdk['sdk_version'], "sdk_release": sdk_release}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        result = metadata(args.tag)
    except (ValueError, StopIteration) as error:
        parser.exit(1, f"Release validation failed: {error}\n")
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write("".join(f"{key}={value}\n" for key, value in result.items()))
    print(json.dumps(result))
