# Pliqra SDK 0.1.1

Stable Python and JavaScript/TypeScript clients for Pliqra 2.1 and the documented draft connection protocol. SDK and gateway versions have independent lifecycles.

This patch corrects package-page installation examples to select the stable client: `python -m pip install pliqra-client==0.1.1` and `npm install @eturkok/pliqra@0.1.1`, or omit the version to use the registry's stable default. Candidate `--pre` / `@next` examples in the 0.1.0 bundled documentation are superseded. Client implementation/API and wire behavior are unchanged.

The release carries tested wheel, source distribution, npm tarball, manifest and checksums. These exact bytes are then published through the existing package-scoped OIDC workflow. Original gateway/SDK release tags and artifacts are preserved. Python 3.10/3.14 and Node 22/24 clean-install checks, protocol tests and the gateway regression suite gate this release.

The 2.1.0 gateway also carries its original tested SDK 0.1.0 assets; use this independent patch for the corrected package documentation. The connection protocol remains `pliqra/0.1-draft`; passing the profile does not establish external certification.

[Client guide](https://github.com/eturkok/pliqra/blob/main/docs/SDK.md) · [Gateway stable release](https://github.com/eturkok/pliqra/releases/tag/v2.1.0) · [Support envelope](https://github.com/eturkok/pliqra/blob/main/docs/SUPPORT.md)
