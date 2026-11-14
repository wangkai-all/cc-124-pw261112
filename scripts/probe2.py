import sqlite3, duckdb, json

s = sqlite3.connect(':memory:')
d = duckdb.connect(':memory:')
s.execute("CREATE TABLE t(id INTEGER, name TEXT, amt REAL, flag BOOLEAN, d TEXT, ts TEXT)")
s.executemany("INSERT INTO t VALUES(?,?,?,?,?,?)",
    [(1,'Alice',1.5,1,'2026-01-05','2026-01-05 10:20:30'),(2,'bob',None,0,'2026-02-01',None),(3,None,3.0,1,None,None)])
d.execute("CREATE TABLE t(id INTEGER, name VARCHAR, amt DOUBLE, flag BOOLEAN, d DATE, ts TIMESTAMP)")
d.executemany("INSERT INTO t VALUES(?,?,?,?,?,?)",
    [(1,'Alice',1.5,True,'2026-01-05','2026-01-05 10:20:30'),(2,'bob',None,False,'2026-02-01',None),(3,None,3.0,True,None,None)])

def both(label, sql):
    out={}
    for n,c in (('sqlite',s),('duckdb',d)):
        try:
            cur=c.execute(sql); out[n]={'ok':True,'rows':cur.fetchall()}
        except Exception as e:
            out[n]={'ok':False,'err':str(e).split('\n')[0]}
    print(f'### {label}: {sql}\n  {json.dumps(out, ensure_ascii=False, default=str)}\n')

both('empty agg', "SELECT COUNT(*) c, SUM(amt) sm, AVG(amt) av, MIN(amt) mn FROM (SELECT amt FROM t WHERE id>99) z")
both('count col vs star', "SELECT COUNT(*), COUNT(name), COUNT(amt) FROM t")
both('distinct from', "SELECT id FROM t WHERE name IS NOT DISTINCT FROM 'Alice' OR name IS NOT DISTINCT FROM NULL")
both('strftime sqlite order', "SELECT strftime('%Y-%m', d) FROM t WHERE id=1")
both('strftime duck order', "SELECT strftime(d, '%Y-%m') FROM t WHERE id=1")
both('date arithmetic sqlite', "SELECT DATE(d, '-3 day') FROM t WHERE id=1")
both('date arithmetic duck', "SELECT CAST(d - INTERVAL 3 DAY AS VARCHAR) FROM t WHERE id=1")
both('date compare literal', "SELECT id FROM t WHERE d >= '2026-02-01'")
both('ilike duck', "SELECT id FROM t WHERE name ILIKE 'a%'")
both('lower like', "SELECT id FROM t WHERE LOWER(name) LIKE 'a%'")
both('real cast div', "SELECT CAST(id AS REAL)/2 FROM t WHERE id=1")
both('nullif', "SELECT NULLIF(name,'bob') FROM t ORDER BY id")
both('interval literal sqlite?', "SELECT INTERVAL 3 DAY")
both('multiset dup', "SELECT name FROM t")
both('bool select', "SELECT id, flag FROM t ORDER BY id")
both('string minus date', "SELECT d - 3 FROM t WHERE id=1")
both('div zero col', "SELECT 10/0, 10.0/0")
both('sum all null', "SELECT SUM(amt) FROM t WHERE id=2")

# introspection APIs
print('### sqlite pragma:', s.execute("PRAGMA table_info(t)").fetchall())
print('### duck info_schema:', d.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_name='t' ORDER BY ordinal_position").fetchall())
rel = d.sql("SELECT id, amt, flag, d, ts FROM t")
print('### duck relation.types:', rel.types)
print('### duck description:', rel.description)
cur = d.execute("SELECT id, amt FROM t")
print('### duck cursor desc attr:', cur.description, [type(x[1]) for x in cur.description])
cur2 = s.execute("SELECT id, amt, flag, d FROM t")
print('### sqlite desc:', cur2.description)
print('### sqlite typeof row:', s.execute("SELECT typeof(id),typeof(amt),typeof(flag),typeof(d) FROM t LIMIT 1").fetchall())
# multi statement guard behavior
try:
    s.execute("SELECT 1; SELECT 2")
    print('### sqlite multi: no error?!')
except Exception as e:
    print('### sqlite multi err:', type(e).__name__, str(e)[:80])
try:
    d.execute("SELECT 1; SELECT 2")
    print('### duck multi: no error?! rows=', d.fetchall())
except Exception as e:
    print('### duck multi err:', type(e).__name__, str(e)[:80])
# duckdb typed string insert cast
d.execute("CREATE TABLE t2(d DATE)")
d.execute("INSERT INTO t2 VALUES (?)", ['2026-03-07'])
print('### duck varchar->date insert:', d.execute("SELECT * FROM t2").fetchall())
