# Security Policy

Do not report a vulnerability with a real API key, conversation database,
memory export, process-control file, log archive, browser profile, or recording.
Use synthetic fixtures and redact local usernames and paths.

Until a public repository owner and contact channel are chosen, security reports
should be kept private. After publication, enable GitHub private vulnerability
reporting and replace this paragraph with the repository's supported contact
process.

Before every release, rebuild the public staging directory from the allowlist
and run `scripts/audit-public-tree.py`. A failed or incomplete audit blocks the
release. Deleting a secret after committing it is not sufficient because the
value remains in Git history; revoke the credential and rebuild from a clean
history.
