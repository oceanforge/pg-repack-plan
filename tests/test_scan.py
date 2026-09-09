"""The planner's whole value is that its verdict matches the server's.

So the tests do not check the logic against itself. They create each awkward
table, ask the planner, then actually run REPACK (CONCURRENTLY) and compare.
Needs a PostgreSQL 19 server: set DATABASE_URL. CI starts one.
"""
import os

import psycopg
import pytest

from repack_plan.scan import eligibility, plan_for, scan


@pytest.fixture(scope='module')
def conn():
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        pytest.skip('DATABASE_URL not set')
    c = psycopg.connect(dsn)
    with c.cursor() as cur:
        cur.execute('SHOW server_version_num')
        if int(cur.fetchone()[0]) < 190000:
            c.close()
            pytest.skip('REPACK needs PostgreSQL 19')
    c.commit()
    yield c
    c.close()


@pytest.fixture(scope='module')
def fixtures(conn):
    ddl = [
        "DROP TABLE IF EXISTS ok_tbl, nopk_tbl, unlogged_tbl, part_tbl CASCADE",
        "CREATE TABLE ok_tbl (id int PRIMARY KEY, v text)",
        "INSERT INTO ok_tbl SELECT g, repeat('x', 50) FROM generate_series(1, 5000) g",
        "CREATE TABLE nopk_tbl (a int)",
        "INSERT INTO nopk_tbl SELECT g FROM generate_series(1, 5000) g",
        "CREATE UNLOGGED TABLE unlogged_tbl (id int PRIMARY KEY)",
        "CREATE TABLE part_tbl (id int, d date, PRIMARY KEY (id, d)) PARTITION BY RANGE (d)",
        "CREATE TABLE part_tbl_2026 PARTITION OF part_tbl "
        "  FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
        "ANALYZE ok_tbl", "ANALYZE nopk_tbl",
    ]
    with conn.cursor() as cur:
        for s in ddl:
            cur.execute(s)
    conn.commit()
    return {r['table']: r for r in scan(conn, min_bytes=0, min_dead_ratio=0.0)}


def server_accepts(dsn, name):
    """Does REPACK (CONCURRENTLY) actually work on this table?"""
    c = psycopg.connect(dsn, autocommit=True)
    try:
        c.execute(f'REPACK (CONCURRENTLY) {name}')
        return True, ''
    except Exception as e:
        return False, str(e).strip().split('\n')[0]
    finally:
        c.close()


@pytest.mark.parametrize('table,expected', [
    ('ok_tbl', True),
    ('nopk_tbl', False),
    ('unlogged_tbl', False),
    ('part_tbl', False),
    ('part_tbl_2026', True),
])
def test_prediction_matches_the_server(fixtures, table, expected):
    row = fixtures[table]
    assert row['concurrent_ok'] is expected, f"planner said {row['concurrent_ok']}, expected {expected}"
    actual, msg = server_accepts(os.environ['DATABASE_URL'], f"public.{table}")
    assert actual is expected, f'server disagreed for {table}: {msg}'
    assert row['concurrent_ok'] is actual, f'planner and server disagree on {table}: {msg}'


def test_refusals_come_with_a_reason(fixtures):
    for name in ('nopk_tbl', 'unlogged_tbl', 'part_tbl'):
        assert fixtures[name]['reasons'], f'{name} refused with no explanation'


def test_partitioned_parent_is_routed_to_its_partitions(fixtures):
    stmt, mode = plan_for(fixtures['part_tbl'])
    assert stmt is None
    assert 'partition' in mode


def test_eligible_table_gets_a_concurrent_statement(fixtures):
    stmt, mode = plan_for(fixtures['ok_tbl'])
    assert stmt.startswith('REPACK (CONCURRENTLY)')
    assert mode == 'online'


def test_ineligible_table_falls_back_to_plain_repack(fixtures):
    stmt, mode = plan_for(fixtures['nopk_tbl'])
    assert stmt.startswith('REPACK ')
    assert 'CONCURRENTLY' not in stmt
    assert 'window' in mode


def test_replica_identity_index_is_accepted_in_place_of_a_primary_key(conn):
    """A unique index plus REPLICA IDENTITY USING INDEX is the documented way out."""
    with conn.cursor() as cur:
        cur.execute('DROP TABLE IF EXISTS ri_tbl')
        cur.execute('CREATE TABLE ri_tbl (a int NOT NULL, b text)')
        cur.execute("INSERT INTO ri_tbl SELECT g, 'x' FROM generate_series(1, 2000) g")
        cur.execute('CREATE UNIQUE INDEX ri_tbl_a ON ri_tbl (a)')
    conn.commit()
    before = {r['table']: r for r in scan(conn, min_bytes=0, min_dead_ratio=0.0)}['ri_tbl']
    assert before['concurrent_ok'] is False        # unique index alone is not enough

    with conn.cursor() as cur:
        cur.execute('ALTER TABLE ri_tbl REPLICA IDENTITY USING INDEX ri_tbl_a')
    conn.commit()
    after = {r['table']: r for r in scan(conn, min_bytes=0, min_dead_ratio=0.0)}['ri_tbl']
    assert after['concurrent_ok'] is True

    actual, msg = server_accepts(os.environ['DATABASE_URL'], 'public.ri_tbl')
    assert actual is True, msg
