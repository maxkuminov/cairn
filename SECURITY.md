# Security Policy

Cairn is a file-integrity and notarization tool, so its own integrity matters. If you
find a vulnerability, please report it privately rather than opening a public issue.

## Reporting a vulnerability

- **Preferred:** open a [GitHub private security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  on this repository ("Security" → "Report a vulnerability").
- Please include a description, affected version/commit, and reproduction steps.

This is a hobby project maintained on a best-effort basis — there is no SLA, but
security reports are taken seriously and triaged when received.

## Scope and design notes

A few invariants worth knowing when assessing impact:

- **Watched folders are mounted read-only** (`:ro`). Cairn cannot modify or delete the
  bytes it monitors; the durable guarantee is the file bytes plus their `.ots` proofs,
  not the SQLite index (which is rebuildable).
- **Single-user mode has no in-app login wall.** Do not expose a single-user deployment
  directly to the internet — front it with a reverse proxy that enforces authentication
  (the example compose uses Traefik with OAuth). Multi-user login is Phase 2 and not yet
  shipped. Keep `/healthz` as the only unauthenticated route if you need an external
  monitor to poll it.
- **Verification trust model.** The default `explorer` backend trusts a block explorer's
  canonical block at a given height. For full trustlessness, point Cairn at your own
  Bitcoin node (`node` backend).

## Supported versions

Cairn is pre-1.0. Only the latest `main` is supported.
