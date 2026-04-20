# JD fetch throughput and success-rate design

## Goals

- **Throughput**: scale aggregate JD fetches toward cluster-level targets (e.g. ~200/s) by raising Celery worker concurrency and tuning env vars — always validated with load tests, not assumed.
- **Reliability**: target **≥ 98%** “healthy outcomes” (successful JD stored, or an explicit terminal state such as gone/blocked after bounded retries — define in product).

## Mechanisms (implemented)

### 1. `JarvisFetchGate` (`apps/harvest/http_limits.py`)

- **Global semaphore** (`JARVIS_HTTP_MAX_GLOBAL`, default **200** per **process**): caps concurrent outbound HTTP across all hosts in that worker.
- **Per-host semaphore** (`JARVIS_HTTP_MAX_PER_HOST`, default **12**): reduces 429/blocks when many URLs hit the same ATS hostname.
- **Retries** (transient only): timeouts, connection errors, **429**, **502/503/504**, with exponential backoff + jitter (`JARVIS_HTTP_RETRY_MAX`, `JARVIS_HTTP_RETRY_BASE_SEC`).

Effective cluster concurrency is approximately:

`workers × processes × JARVIS_HTTP_MAX_GLOBAL` (upper bound; real throughput depends on latency and remote limits).

### 2. Django settings (`config/settings.py`)

| Env / setting | Default | Role |
|----------------|---------|------|
| `JARVIS_HTTP_MAX_GLOBAL` | 200 | Max concurrent HTTP in one worker process |
| `JARVIS_HTTP_MAX_PER_HOST` | 12 | Max concurrent HTTP to one hostname |
| `JARVIS_HTTP_RETRY_MAX` | 3 | Max extra attempts after first failure |
| `JARVIS_HTTP_RETRY_BASE_SEC` | 0.5 | Base delay for backoff |
| `HARVEST_BACKFILL_INTER_JOB_DELAY_SEC` | 0.05 | Sleep between jobs in a backfill chunk (Jarvis limits do most shaping) |

### 3. Backfill task

Uses `HARVEST_BACKFILL_INTER_JOB_DELAY_SEC` instead of a fixed 0.3s delay so throughput can rise when limits allow.

## Operations checklist

1. **PostgreSQL** for backfill row claiming (`SKIP LOCKED`).
2. **Celery**: `celery -A config worker -c N` with **N** large enough for parallel chunks (see harvest backfill docs); avoid `-c 1` if you want multiple chunk tasks at once.
3. **Tune** env vars after measuring: success rate, p95 latency, 429/403 counts per platform.
4. **SLO**: define “success” vs “expected failure” (404 job gone) before promising 98%.

## Parallel backfill without PostgreSQL `SKIP LOCKED`

If the database does **not** support `SELECT … FOR UPDATE SKIP LOCKED` (e.g. SQLite), backfill still runs **multiple chunk tasks** by **sharding on primary key**: `MOD(pk, shard_count) = shard_index`, so chunks never claim the same row. PostgreSQL with `SKIP LOCKED` remains preferable for fair queueing under heavy contention.

## What this does *not* guarantee

- A fixed jobs/sec in all conditions (single tenant, heavy HTML, IP blocks).
- Cross-process coordination of per-host limits without shared state — each process has its own gate; total per-host concurrency scales with worker processes.
