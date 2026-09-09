"""pg-repack-plan: which of your bloated tables can PostgreSQL 19 repack online?"""
import argparse, json, os, sys

import psycopg

from .scan import plan_for, scan


def human(n):
    for unit in ('B', 'kB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024:
            return f'{n:.0f} {unit}'
        n /= 1024
    return f'{n:.0f} PB'


def server_supports_repack(conn):
    with conn.cursor() as cur:
        cur.execute('SHOW server_version_num')
        num = int(cur.fetchone()[0])
    conn.rollback()
    return num >= 190000, num


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='pg-repack-plan',
        description='Find bloated tables and say which ones REPACK (CONCURRENTLY) will accept.')
    p.add_argument('--dsn', default=os.environ.get('DATABASE_URL'),
                   help='Postgres connection string (or set DATABASE_URL)')
    p.add_argument('--min-size', default='10MB',
                   help='ignore tables smaller than this (default 10MB)')
    p.add_argument('--min-dead', type=float, default=0.10,
                   help='ignore tables with a dead-tuple ratio below this (default 0.10)')
    p.add_argument('--all', action='store_true', help='list every table, not just the bloated ones')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    args = p.parse_args(argv)

    if not args.dsn:
        p.error('no --dsn and no DATABASE_URL')

    units = {'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}
    s = args.min_size.upper().strip()
    min_bytes = int(float(s[:-2]) * units[s[-2:]]) if s[-2:] in units else int(s)

    conn = psycopg.connect(args.dsn)
    ok, ver = server_supports_repack(conn)
    rows = scan(conn, min_bytes=min_bytes, min_dead_ratio=args.min_dead)
    shown = [r for r in rows if r['bloated'] or args.all]

    if args.json:
        print(json.dumps(shown, indent=1, default=str))
        return 0

    major = ver // 10000
    if not ok:
        print(f'! This server is PostgreSQL {major}. REPACK arrived in 19, so the plan '
              f'below is what you could run after upgrading.\n')

    if not shown:
        print('No tables past the bloat thresholds. Nothing to plan.')
        return 0

    online, window, partitioned = [], [], []
    for r in shown:
        stmt, mode = plan_for(r)
        (partitioned if mode.startswith('repack partitions')
         else online if mode == 'online' else window).append((r, stmt))

    print(f"{'table':44s} {'size':>10s} {'dead':>6s} {'reclaim':>10s}  repack online?")
    print('-' * 92)
    for r in shown:
        name = f"{r['schema']}.{r['table']}"
        verdict = 'yes' if r['concurrent_ok'] else '; '.join(r['reasons'])[:34]
        print(f"{name[:44]:44s} {human(r['total_bytes']):>10s} "
              f"{r['dead_ratio']*100:5.1f}% {human(r['reclaimable_bytes']):>10s}  {verdict}")

    print()
    if online:
        print(f'Online, no maintenance window needed ({len(online)}):')
        print('  -- the swap at the end still needs ACCESS EXCLUSIVE, so it queues')
        print('  -- behind a slow query and everything else queues behind it.')
        print('  SET lock_timeout = \'5s\';')
        for r, stmt in online:
            print('  ' + stmt)
    if window:
        print(f'\nNeeds a maintenance window, these hold ACCESS EXCLUSIVE throughout ({len(window)}):')
        for r, stmt in window:
            print(f"  {stmt}   -- {'; '.join(r['reasons'])}")
    if partitioned:
        print(f'\nPartitioned, repack each partition instead ({len(partitioned)}):')
        for r, _ in partitioned:
            print(f"  {r['schema']}.{r['table']}")

    total = sum(r['reclaimable_bytes'] for r in shown)
    print(f'\nRoughly {human(total)} reclaimable across {len(shown)} table(s).')
    print('REPACK rewrites the table, so it needs free space of about the table '
          'size plus its indexes while it runs.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
