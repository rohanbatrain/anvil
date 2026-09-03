# ADR-0015: A VPS with systemd and Caddy, not a managed platform

- **Status:** Accepted
- **Date:** 2026-09-03
- **Supersedes:** the Fly.io and Render configurations added earlier the same day

## Context

The public demonstration needs to live at `anvil.rohanbatra.in`. The first
attempt configured Fly.io, with Render as a fallback — both Docker-native, both
handling TLS and custom domains automatically.

Two things argued against continuing down that road.

**Docker has failed twice on this project.** Once when the disk filled and
Docker Desktop would not restart (ADR-0013), and again when the image could not
be built locally at all for want of disk. A deployment path whose first step is
a Docker build is a deployment path that has already failed here, and the
demonstration is due in two days.

**A VPS was available and preferred.** Full control over the host, no platform
abstraction to learn, and — for a submission being assessed on engineering — a
deployment that shows the parts rather than hiding them behind a `fly deploy`.

## Decision

Deploy to a VPS with **systemd supervising a virtualenv** and **Caddy in front**.

- **systemd, not Docker on the VPS.** Fewer moving parts, and
  `journalctl -u anvil` is the complete story. The unit carries real hardening:
  `ProtectSystem=strict`, an empty `CapabilityBoundingSet`, a `SystemCallFilter`,
  and a single `ReadWritePaths`.
- **Caddy, not nginx with certbot.** Caddy obtains and renews the certificate
  itself. A renewal cron job that fails silently three months from now is a
  worse outcome than a slightly less familiar config file.
- **The application binds `127.0.0.1` only.** Caddy proxies over loopback, so
  the service is unreachable from the network even if the firewall is
  misconfigured. A service protected only by a firewall rule is protected by one
  mistake.
- **Atomic releases by symlink.** A release is built in full, its own test suite
  and the guided tour are run against it, and only then does `current` move.
- **Two accounts, neither of them root.** A service account with no shell that
  owns the process, and a deploy account whose passwordless sudo is scoped to
  exactly `systemctl restart anvil` and `systemctl status anvil`.

The Fly and Render configurations are deleted rather than left in place, because
dead deployment config invites someone to use it.

## Consequences

Deployment is legible. Every piece — the unit, the proxy config, the bootstrap,
the release script — is a file in `deploy/` that a reviewer can read, and the
security decisions are visible rather than implied by a platform's defaults.

CI and a human run **the same script**, so the automated and manual paths cannot
drift apart.

A failed release rolls itself back. The pre-flight runs the unit suite against
the new code before promoting it, so a release whose tests fail never becomes
the live one.

The costs are real. We own patching, and the bootstrap enables unattended
security upgrades and `fail2ban` rather than pretending otherwise. There is **one
machine and therefore an outage during a restart** — acceptable for a
demonstration, not for anything with users. And a compromised deploy key gets a
shell as `deploy` on a real host, which is a larger blast radius than a
compromised platform token; hence the scoped sudoers fragment and the pinned
host key in CI rather than `StrictHostKeyChecking=no`.

`shellcheck` runs in CI. Pointing it at these scripts the first time found four
genuine defects, which is a reasonable argument for it running every time.

## Alternatives considered

**Fly.io.** Configured, then removed. Zero-infrastructure and automatic TLS, but
the deploy begins with a Docker build, which is the step that has already broken
here twice.

**Render.** Same reasoning, plus less control over the host for a submission
being judged on engineering.

**Docker Compose on the VPS.** Reproducibility matching CI, and the compose file
already exists for local development. Rejected because it reintroduces the
Docker dependency on the one machine that must not fail during the demonstration,
for a benefit — isolation — that systemd's hardening directives largely provide.

**nginx with certbot.** Familiar and well documented. Rejected because automatic
renewal is the failure mode that bites months later, quietly, and Caddy removes
the class of problem rather than managing it.
