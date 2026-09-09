"""How long does maintenance keep writers out?

VACUUM FULL holds ACCESS EXCLUSIVE for the whole rewrite. PostgreSQL 19's
REPACK CONCURRENTLY only takes it to swap the files at the end. This measures
what a writer actually experiences during each.
"""
import argparse, json, statistics, threading, time
import psycopg

DSN = "host=127.0.0.1 port=55439 user=postgres password=demo dbname=demo"


def setup(rows=400_000, dead_fraction=0.5):
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS bloat")
        c.execute("""CREATE TABLE bloat (
                       id bigint PRIMARY KEY,
                       payload text NOT NULL,
                       n int NOT NULL,
                       updated_at timestamptz NOT NULL DEFAULT now())""")
        c.execute("""INSERT INTO bloat (id, payload, n)
                     SELECT g, repeat('x', 200), g FROM generate_series(1, %s) g""", (rows,))
        c.execute("CREATE INDEX bloat_n_idx ON bloat (n)")
        # Make real bloat: delete a fraction, so the pages keep dead tuples.
        c.execute("DELETE FROM bloat WHERE id %% 2 = 0" if dead_fraction == 0.5
                  else "DELETE FROM bloat WHERE random() < %s", () if dead_fraction == 0.5 else (dead_fraction,))
        c.execute("ANALYZE bloat")


def size():
    with psycopg.connect(DSN, autocommit=True) as c:
        total = c.execute("SELECT pg_total_relation_size('bloat')").fetchone()[0]
        pretty = c.execute("SELECT pg_size_pretty(pg_total_relation_size('bloat'))").fetchone()[0]
        live = c.execute("SELECT count(*) FROM bloat").fetchone()[0]
    return total, pretty, live


class Writer(threading.Thread):
    """Hammers the table with small updates and records how long each one took."""

    def __init__(self, interval=0.005):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []       # (timestamp, duration_seconds, ok)
        self.stop_flag = threading.Event()

    def run(self):
        c = psycopg.connect(DSN, autocommit=True)
        i = 0
        while not self.stop_flag.is_set():
            i += 1
            started = time.time()          # wall clock at START, so a blocked
            t0 = time.perf_counter()       # write is attributed to the window
            ok = True                      # it was actually waiting in
            try:
                c.execute("UPDATE bloat SET n = n + 1, updated_at = now() WHERE id = %s",
                          (1 + (i * 2) % 4_000_000,))
            except Exception:
                ok = False
                try:
                    c.close()
                except Exception:
                    pass
                c = psycopg.connect(DSN, autocommit=True)
            self.samples.append((started, time.perf_counter() - t0, ok))
            time.sleep(self.interval)
        c.close()


def run_maintenance(sql, settle=1.0):
    w = Writer()
    w.start()
    time.sleep(settle)              # let the writer establish a baseline
    t0 = time.time()
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(sql)
    t1 = time.time()
    time.sleep(settle)
    w.stop_flag.set()
    w.join()

    during = [(ts, d, ok) for ts, d, ok in w.samples if t0 <= ts <= t1]
    before = [d for ts, d, ok in w.samples if ts < t0]
    baseline = statistics.median(before) if before else 0.0
    stalls = [d for ts, d, ok in during if d > max(0.05, baseline * 20)]
    return {
        'sql': sql,
        'maintenance_seconds': round(t1 - t0, 2),
        'writes_attempted_during': len(during),
        'baseline_write_ms': round(baseline * 1000, 2),
        'max_write_ms': round(max((d for _, d, _ in during), default=0) * 1000, 1),
        'stalled_writes': len(stalls),
        'total_stalled_seconds': round(sum(stalls), 2),
        'failed_writes': sum(1 for _, _, ok in during if not ok),
    }


def report(label, before, after, res):
    print(f"\n=== {label} ===")
    print(f"  table size   {before[1]} -> {after[1]}   ({before[2]:,} live rows)")
    print(f"  maintenance took        {res['maintenance_seconds']} s")
    print(f"  writes attempted during {res['writes_attempted_during']}")
    print(f"  normal write latency    {res['baseline_write_ms']} ms")
    print(f"  slowest write           {res['max_write_ms']} ms")
    print(f"  writes stalled          {res['stalled_writes']}  ({res['total_stalled_seconds']} s blocked in total)")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--rows', type=int, default=400_000)
    args = ap.parse_args()
    out = {}

    for label, sql in [('VACUUM FULL', 'VACUUM FULL bloat'),
                       ('REPACK (CONCURRENTLY)', 'REPACK (CONCURRENTLY) bloat')]:
        setup(args.rows)
        b = size()
        res = run_maintenance(sql)
        a = size()
        res['size_before'] = b[1]
        res['size_after'] = a[1]
        res['bytes_before'] = b[0]
        res['bytes_after'] = a[0]
        report(label, b, a, res)
        out[label] = res

    with open('repack_results.json', 'w') as f:
        json.dump(out, f, indent=1)
    print('\nwrote repack_results.json')
