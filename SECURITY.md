# Security policy

The maintained community release line is **2.1.x**. Security fixes are delivered as new patch versions and immutable image tags. Upgrade to the current patch; earlier candidates and unsupported lines do not receive backported fixes. This is a community policy, without a contractual response or remediation SLA.

For a suspected vulnerability, use the repository's **Security → Report a vulnerability** option when available. If private reporting is unavailable, open an issue requesting a private maintainer contact **without** exploit details, credentials or customer data. Acknowledge the affected version and whether the issue is reproducible in an isolated workspace. Do not place credentials, captured prompts or unredacted backups in public issues.

The release pipeline checks known Python runtime dependency advisories with [PyPA pip-audit](https://github.com/pypa/pip-audit), without vulnerability suppressions. The dated report is attached to each release. OCI images carry an SBOM and build provenance. These controls do not constitute a penetration test or a guarantee against undiscovered vulnerabilities; operators also maintain the host, ingress, database and upstream providers.

Use the deployment boundaries in [support and compatibility](docs/SUPPORT.md), [operations](docs/OPERATIONS.md) and [multi-DC](docs/MULTI_DC.md). Keep the full encryption key with recoverable backups, restrict administrator access and configure verified HTTPS before external exposure.
