# Security policy

## Reporting a vulnerability

Please report security issues privately, through GitHub's **Report a vulnerability** button on
this repository's [Security tab](https://github.com/flam7791/policy-evidence-mcp/security), not in a public
issue. You will get an acknowledgement within five working days and a fix or a decision within
thirty.

## Supported versions

Only the latest release receives fixes. This is a reference implementation maintained by one
person; it is not a supported product.

## Scope and design notes

- All examples use fictional organisations and synthetic data; no real personal or internal data
  is stored in the repository.
- Secrets are never committed: keys come from the environment or a secret store, and the
  repository is checked for key patterns on every push (`aief check`, rule ENG-04).
- Dependencies are scanned with `pip-audit` in CI and updated monthly by Dependabot.
- The threat model and the controls for each threat are described in the README and the system
  card ([SYSTEM_CARD.md](SYSTEM_CARD.md)), where the repository has one.
