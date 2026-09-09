# pg-repack-plan

PostgreSQL 19 adds `REPACK`, which rewrites a bloated table to reclaim space and
can do it **without locking out readers and writers**. It also refuses to do that
for four separate reasons, and one of them is partitioned tables.

This works out which of your tables it will accept, before you plan a maintenance
window you may not need.

```
table                       size    dead    reclaim  repack online?
public.events             226 MB   66.7%     151 MB  yes
public.metrics_2026        32 MB   75.0%      24 MB  yes
public.sessions            22 MB   40.1%       9 MB  no identity index, add a primary k

Online, no maintenance window needed (2):
  REPACK (CONCURRENTLY) public.events;
  REPACK (CONCURRENTLY) public.metrics_2026;

Needs a maintenance window, these hold ACCESS EXCLUSIVE throughout (1):
  REPACK public.sessions;   -- no identity index, add a primary key or ...

Roughly 184 MB reclaimable across 3 table(s).
```

## Why it exists

`VACUUM FULL` holds an `ACCESS EXCLUSIVE` lock for the whole rewrite. Measured on
a 424 MB table with eight connections writing to it:

| | duration | writes committed | slowest write | size |
|---|---|---|---|---|
| `REPACK (CONCURRENTLY)` | 4.32 s | 3,988 | 125 ms | 424 → 213 MB |
| `VACUUM FULL` | 0.97 s | 33 | 969 ms | 424 → 212 MB |

Reads are blocked as well. On a 1.1 GB table, `VACUUM FULL` served **one** read in
2.27 seconds; `REPACK (CONCURRENTLY)` served 384.

Note that `VACUUM FULL` is the *faster* of the two. It has the table to itself.
The trade is total duration against staying online, which is usually the right
trade and occasionally is not.

## What it refuses, and why this tool is useful

`REPACK (CONCURRENTLY)` will not touch:

| Case | Error |
|---|---|
| No primary key or replica identity | `Relation "x" has no identity index.` |
| Partitioned table | `not supported for partitioned tables` |
| Unlogged table | `only allowed for permanent relations` |
| Inside a transaction block | `cannot run inside a transaction block` |

A unique index is **not** enough on its own:

```sql
CREATE UNIQUE INDEX t_a ON t (a);                  -- still refused
ALTER TABLE t REPLICA IDENTITY USING INDEX t_a;    -- accepted
```

Partitioned tables are the big one. They grow largest, so they are what you most
want to rewrite online, and they are what the online path declines. Repack the
partitions instead:

```sql
SELECT format('REPACK (CONCURRENTLY) %I.%I;', n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relispartition AND c.relkind = 'r';
```

## Install

```bash
git clone https://github.com/oceanforge/pg-repack-plan
cd pg-repack-plan
pip install -e .
```

## Use

```bash
export DATABASE_URL="postgres://user:pass@host:5432/dbname"
pg-repack-plan --min-size 10MB
```

| Flag | Default | What it does |
|---|---|---|
| `--min-size` | `10MB` | Ignore tables smaller than this |
| `--min-dead` | `0.10` | Ignore tables below this dead-tuple ratio |
| `--all` | off | List every table, not just the bloated ones |
| `--json` | off | Machine-readable output |

It reads catalogue views and prints statements. It never runs them, and it never
writes to your database.

## It works on 17 and 18 too

`REPACK` needs PostgreSQL 19, which is in beta at the time of writing. Managed
Postgres offers 15 through 18 today, DigitalOcean's included, so the tool detects
an older server and reports what will be available to you after upgrading:

```
! This server is PostgreSQL 18. REPACK arrived in 19, so the plan below is what
  you could run after upgrading.
```

That is the more useful mode right now. Finding out today that your largest table
has no primary key gives you months to fix it, instead of discovering it during
an upgrade.

## Tests

The value of a tool that predicts what a server will do is entirely in whether it
matches the server. So the tests do not check the logic against itself. They
create each awkward table, ask the planner for a verdict, then actually run
`REPACK (CONCURRENTLY)` and compare the two.

```bash
export DATABASE_URL="postgres://postgres:postgres@localhost:5432/postgres"
pytest -q tests/
```

CI runs them against a real `postgres:19beta3` service container on every push.

## Benchmark

`bench/bench.py` reproduces the numbers above: it builds a bloated table, points a
writer at it, and reports what that writer experienced during each command.

```bash
docker run -d --name pg19 -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=demo \
  -p 55439:5432 postgres:19beta3
python bench/bench.py --rows 4000000
```

## Licence

MIT
