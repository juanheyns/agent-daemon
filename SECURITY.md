# Security policy

## Supported versions

`blemeesd` is pre-1.0. Only the latest `0.x` release receives security
fixes; older `0.x` tags are not patched.

| Version | Supported |
| ------- | --------- |
| Latest `0.x` | yes |
| Older `0.x` | no, upgrade to latest |

## Reporting a vulnerability

Please **do not** open a public issue for suspected vulnerabilities.

Use GitHub's private vulnerability reporting:
<https://github.com/blemees/blemees-daemon/security/advisories/new>

Include enough detail for us to reproduce (OS, daemon version, minimal
trigger). You should get an acknowledgement within a few days. We aim
to ship a fix or mitigation in the next patch release once the issue is
confirmed.

## Scope

`blemeesd` is a **per-user** daemon by design: one instance per UID,
socket permissions `0600`, no peer-UID allowlist. Anyone who can
`connect()` the socket has full access to the owning user's Claude
subscription. This is intentional — see [the README](README.md#7-security)
— and reports of "another user on the same machine can't reach the
socket" are not vulnerabilities.

In scope for reports:

- Any path that lets a non-owning UID reach or spoof the socket.
- Subprocess escape / command injection from crafted wire frames.
- Log or event-log poisoning across sessions.
- Credential leakage in logs, event streams, or error paths.
- Denial-of-service against a legitimate owner (resource exhaustion
  beyond documented backpressure behavior).
