# Reporting Security Issues

We take all security reports seriously. If you discover a security issue in this
sample, please **do not** create a public GitHub issue.

Instead, report it via the AWS vulnerability reporting page at
<https://aws.amazon.com/security/vulnerability-reporting/> or e-mail
[aws-security@amazon.com](mailto:aws-security@amazon.com).

## Scope notes for this sample

This repository is **sample code for demonstration purposes and is not intended
for production use without further review and hardening**. The security posture,
accepted residual risks, and the controls that are deliberately absent are
documented in:

- [`docs/threat-model.md`](docs/threat-model.md) — STRIDE threat model with
  per-threat status and named residual risks
- [`docs/security-model.md`](docs/security-model.md) — the control set actually
  implemented by the CDK stacks

Please read those documents before deploying, and re-assess them against your
own requirements before using any part of this sample in a production system.
