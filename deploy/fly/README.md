# Fly deployment — stateful backing services

This is the operator runbook for the two stateful backing services created by
plan 14-04: `vantage-chroma` (self-hosted ChromaDB, D-07) and `vantage-redis`
(self-hosted Redis, D-06 as revised by research). Plans 14-08, 14-09, and
14-10 execute the commands in this file against those two apps and against
the main `vantage` app (repo-root `fly.toml`, plan 14-03).

Neither app is publicly reachable. Both are private-network-only Fly apps,
addressable from the main app at their `.flycast` names.

## Volume creation (BEFORE the first deploy)

Volumes must exist before the first deploy of each app — a deploy with no
pre-created volume fails to attach a mount. There must be exactly one volume
per source name: a duplicate name means a rescheduled machine may attach the
empty one and appear to have lost all data.

```bash
fly volumes create chroma_data --region iad --size 3 -a vantage-chroma
fly volumes create redis_data --region iad --size 1 -a vantage-redis
```

## Deploy

Each app is deployed with its own manifest path, since these are separate
Fly apps from the main `vantage` app.

```bash
fly deploy --config deploy/fly/chroma/fly.toml --app vantage-chroma
fly deploy --config deploy/fly/redis/fly.toml --app vantage-redis
```

## Scale

Pin each app to exactly one machine — these are single-instance stateful
services, not horizontally-scaled process groups.

```bash
fly scale count 1 --app vantage-chroma
fly scale count 1 --app vantage-redis
```

## Diagnosing the silent-persistence-loss failure

A `chroma_data` volume still showing `0` bytes used after a successful
ingestion is the primary warning sign that the manifest is writing to the
ephemeral root filesystem instead of the mounted volume.

```bash
fly volumes list --app vantage-chroma
fly volumes list --app vantage-redis
```

The second diagnostic: shell onto the Chroma machine and measure the actual
size of its persistence directory.

```bash
fly ssh console --app vantage-chroma -C "du -sh /chroma/chroma"
```

## Restart (the DEPLOY-04 acceptance test)

Plan 14-10's acceptance test depends on this command: restart a specific
machine by id, then confirm a previously-ingested document is still
retrievable. Config inspection alone is not accepted as proof of
persistence — a live restart is required.

```bash
fly machine restart <machine-id> --app vantage-chroma
```

## Connection coordinates

Both addresses below are private-network-only Fly `.flycast` names — they
have no public DNS entry and are unreachable from outside Fly's network.

| Service | Address | Port | Carried by |
|---------|---------|------|------------|
| ChromaDB | `vantage-chroma.flycast` | 8000 | `CHROMADB_HOST` / `CHROMADB_PORT` in the main app's `fly.toml` `[env]` |
| Redis | `vantage-redis.flycast` | 6379 | `REDIS_URL` secret on the main app (set via `fly secrets set`, plan 14-08) |

## Accepted trade-offs

**Redis durability.** This single Redis instance is simultaneously the
Celery broker, the Celery result backend, the revoked-token blocklist, and
the research-progress publish channel. Append-only persistence means a
machine restart replays rather than losing state, but a restart during a
write window can still drop the most recent entries — a recently-revoked
token may be accepted again until its natural 24-hour expiry, and an
in-flight research task may need to be resubmitted. This is accepted at this
deployment's scale. The managed alternative, if restarts prove disruptive in
practice, is Upstash's Fixed 250MB plan at roughly $10/mo — unlimited
commands, durable, zero ops — chosen against here for cost.

**Revised monthly cost.** The realistic monthly cost of this topology is
roughly **$15-18/mo** on Fly across the five machines (api, worker, beat,
vantage-chroma, vantage-redis) and two volumes (`chroma_data`, `redis_data`),
with the database (Neon) and frontend (Vercel) on free tiers. This
supersedes the $8-12/mo figure in the phase CONTEXT, which under-counted the
worker's memory requirement (1gb, not 256mb, for the sentence-transformers +
torch load).
