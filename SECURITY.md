# Security policy

## Supported versions

Security fixes are considered for the latest 0.1.x release. This policy may
change as the project matures.

## Report a vulnerability

GitHub private vulnerability reporting is enabled for sensitive reports.

Never post tokens, credentials, prompts, conversations, databases, logs, or
other sensitive data in a public issue, discussion, pull request, or commit.
Provide the smallest synthetic reproducer that demonstrates impact, affected
version, and relevant platform details.

Do not disclose vulnerability details publicly. No response or remediation SLA
is promised.

## Security boundary

The default adapter stores bodies as plaintext in local SQLite; version 0.1 has
no encryption at rest. The library provides no protection against a malicious
local administrator or compromised host. See
[docs/security-guarantees.md](docs/security-guarantees.md) and
[docs/threat-model.md](docs/threat-model.md) for the complete boundary.
