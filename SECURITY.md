# Security policy

## Supported versions

Security fixes go into the latest release. Please update to it before reporting.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately instead:
**Security** tab → **Report a vulnerability** on
[github.com/YaShDsOnI/clipwatcher](https://github.com/YaShDsOnI/clipwatcher/security/advisories/new).

Include what you found, how to reproduce it and what an attacker could do. You'll get a reply within a
few days, and credit in the release notes if you'd like.

## How clipwatcher protects you

- The dashboard listens on `127.0.0.1` only and refuses requests whose `Host` isn't `localhost` /
  `127.0.0.1` (stops DNS rebinding) and requests that change something from any other website
  (`Origin` and `Sec-Fetch-Site` checks, JSON-only bodies).
- AI keys stay in `.env` on your PC and are only sent to the AI provider you chose; the dashboard never
  shows a full key.
- Nothing is downloaded or installed without your OK; downloads come from PyPI, Hugging Face and the
  sherpa-onnx GitHub releases only.
- File names from the browser are validated, so the dashboard can only read and delete clip files.
