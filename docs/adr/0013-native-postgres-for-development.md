# ADR-0013: Native Postgres for development, not Docker

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

Development started on Docker Compose. Partway through, the machine's disk
filled, Docker Desktop wedged, and it would not restart — taking with it the
database, the ability to run migrations, and about an hour.

## Decision

Local development uses **native PostgreSQL 16 via Homebrew**.
`docker-compose.yml` remains in the repository as a submission deliverable and a
deployment reference.

More importantly, the system was restructured so that **almost nothing needs a
database at all**: the 203 unit tests, the guided tour, the batch experiment and
the entire web console run with no Postgres, no Docker and no credentials.
Postgres is needed only for migrations and the integration tests.

## Consequences

A reviewer can assess essentially the whole system with `pip install` and nothing
else. That is a better property than the original setup had, and it came from an
outage rather than from foresight.

Being forced to make the simulator and the console database-free produced a
cleaner boundary than would otherwise have been drawn — the graph's ports
(ADR-0008) are what made it possible.

**`docker-compose.yml` is committed but has never been verified end to end.**
That is a real gap, it is stated in the README, and judges may well run it.

Two environment paths now exist, which is a maintenance cost and a source of
"works on my machine".

## Alternatives considered

**Fixing Docker and continuing.** Attempted; Docker Desktop would not start after
the disk event. Time-boxed and abandoned rather than sunk more time into it.

**Testcontainers.** A good answer for integration tests specifically, and it
still needs a working Docker daemon, which was the thing that was broken.

**SQLite for development.** Rejected because the schema uses JSONB, identity
columns and `SELECT … FOR UPDATE SKIP LOCKED`, and because the append-only
guarantee in ADR-0002 is enforced by Postgres triggers. Developing against a
database that cannot express the invariants would defeat their purpose.
