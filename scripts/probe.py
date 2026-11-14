import sqlite3, duckdb, json

def run(label, sql):
    s, d = sqlite3.connect(':memory:'), duckdb.connect(':memory:')
    out = {}
    for name, con in (('sqlite', s), ('duckdb', d)):
        try:
            cur = con.execute(sql)
            rows = cur.fetchall()
            out[name] = {'ok': True, 'rows': rows,
                         'desc': [c[0] for c in cur.description] if cur.description else None}
        except Exception as e:
            out[name] = {'ok': False, 'err': f'{type(e).__name__}: {e}'}
    print(f'### {label}\n  SQL: {sql}')
    print(' ', json.dumps(out, ensure_ascii=False, default=str))
    print()

run('integer division', "SELECT 7/2 AS r")
run('cast division', "SELECT CAST(7.0/2 AS DOUBLE) AS r")
run('string times string', "SELECT '10' * '3' AS r")
run('like case', "SELECT 'Apple' LIKE 'a%' AS r")
run('date minus int', "SELECT DATE '2026-01-05' - 3 AS r")
run('typeof', "SELECT typeof(1), typeof(1.5), typeof('x'), typeof(NULL), typeof(x'1f') FROM (SELECT 1) t(a)")
run('null is null', "SELECT NULL IS NULL AS a, NULL = NULL AS b")
run('empty agg', "SELECT COUNT(*) c, SUM(x) s, AVG(x) v, MIN(x) mn, MAX(x) mx FROM (SELECT 1 WHERE 0) t(x)")
run('empty group', "SELECT k, COUNT(*) FROM (SELECT 1 AS k WHERE 0) GROUP BY k")
run('div zero', "SELECT 1/0 AS r")
run('cast text num', "SELECT CAST('42' AS INTEGER) + 8 AS r")
run('strftime', "SELECT strftime('%Y-%m', DATE '2026-03-07') AS r")
run('date in sqlite text', "SELECT DATE('2026-01-05') - 3 AS r")
run('concat null', "SELECT 'a' || NULL || 'b' AS r")
run('is operator values', "SELECT 0 IS 0 AS a, 'x' IS 'x' AS b, 1 IS TRUE AS c")
run('bool cast', "SELECT CAST(1 AS BOOLEAN) AS b, typeof(CAST(1 AS BOOLEAN)) AS t")
