"""Find bloated tables and work out which ones REPACK CONCURRENTLY can actually take.

PostgreSQL 19 adds REPACK, which replaces VACUUM FULL and CLUSTER. Its
CONCURRENTLY option keeps writers running, but it refuses a table for four
separate reasons, and the tables most likely to be bloated in production
(partitioned ones) are among the refusals. This works out which is which
before you schedule a maintenance window you may not need.
"""

BLOAT_SQL = """
SELECT
  c.oid,
  n.nspname                                        AS schema,
  c.relname                                        AS table,
  c.relkind                                        AS kind,
  c.relpersistence                                 AS persistence,
  c.relispartition                                 AS is_partition,
  pg_total_relation_size(c.oid)                    AS total_bytes,
  pg_size_pretty(pg_total_relation_size(c.oid))    AS total_pretty,
  COALESCE(s.n_live_tup, 0)                        AS live_tuples,
  COALESCE(s.n_dead_tup, 0)                        AS dead_tuples,
  CASE WHEN COALESCE(s.n_live_tup, 0) + COALESCE(s.n_dead_tup, 0) = 0 THEN 0
       ELSE s.n_dead_tup::float / (s.n_live_tup + s.n_dead_tup)
  END                                              AS dead_ratio,
  s.last_autovacuum,
  (SELECT count(*) FROM pg_index i WHERE i.indrelid = c.oid) AS index_count,
  EXISTS (SELECT 1 FROM pg_index i
          WHERE i.indrelid = c.oid AND i.indisprimary)       AS has_pkey,
  EXISTS (SELECT 1 FROM pg_index i
          WHERE i.indrelid = c.oid AND i.indisreplident)     AS has_replident,
  c.relreplident                                   AS replident_setting
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY pg_total_relation_size(c.oid) DESC
"""


def eligibility(row):
    """Why REPACK (CONCURRENTLY) would refuse this table, if it would.

    Mirrors the checks Postgres 19 makes, so the plan matches what the server
    will actually do rather than what we hope it does.
    """
    reasons = []
    if row['kind'] == 'p':
        reasons.append('partitioned table, repack each partition instead')
    if row['persistence'] == 'u':
        reasons.append('unlogged, CONCURRENTLY needs a permanent relation')
    if row['persistence'] == 't':
        reasons.append('temporary table')
    # CONCURRENTLY needs an identity index: a primary key, or REPLICA IDENTITY USING INDEX
    if row['kind'] == 'r' and not (row['has_pkey'] or row['has_replident']):
        reasons.append('no identity index, add a primary key or set REPLICA IDENTITY USING INDEX')
    return reasons


def scan(conn, min_bytes=10 * 1024 * 1024, min_dead_ratio=0.10):
    with conn.cursor() as cur:
        cur.execute(BLOAT_SQL)
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.rollback()

    out = []
    for r in rows:
        r['reasons'] = eligibility(r)
        r['concurrent_ok'] = not r['reasons']
        r['bloated'] = (r['total_bytes'] >= min_bytes and r['dead_ratio'] >= min_dead_ratio)
        # Rough: dead tuples are proportional to reclaimable space.
        r['reclaimable_bytes'] = int(r['total_bytes'] * r['dead_ratio'])
        out.append(r)
    return out


def plan_for(row):
    """The statement to run, and whether it needs a window."""
    if row['kind'] == 'p':
        return None, 'repack partitions individually'
    if row['concurrent_ok']:
        return f"REPACK (CONCURRENTLY) {row['schema']}.{row['table']};", 'online'
    return f"REPACK {row['schema']}.{row['table']};", 'needs a maintenance window'
