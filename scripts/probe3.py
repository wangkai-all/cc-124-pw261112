import sqlite3, duckdb, json
s = sqlite3.connect(':memory:'); d = duckdb.connect(':memory:')
def chk(label, fn):
    try: print(label, '->', fn())
    except Exception as e: print(label, '!!', type(e).__name__, str(e).split('\n')[0])
chk('duck cast real', lambda: d.execute("SELECT CAST(7 AS REAL)/2").fetchall())
chk('duck total()', lambda: d.execute("SELECT TOTAL(x) FROM (SELECT 1 AS x WHERE 0)").fetchall())
chk('sqlite total()', lambda: s.execute("SELECT TOTAL(x) FROM (SELECT 1 AS x WHERE 0)").fetchall())
chk('duck group_concat', lambda: d.execute("SELECT group_concat(x) FROM (SELECT 'a' x UNION SELECT 'b')").fetchall())
chk('duck string_agg', lambda: d.execute("SELECT string_agg(x,',') FROM (SELECT 'a' x UNION SELECT 'b')").fetchall())
chk('sqlite string_agg', lambda: s.execute("SELECT string_agg(x,',') FROM (SELECT 'a' x)").fetchall())
chk('duck interrupt attr', lambda: hasattr(d,'interrupt'))
chk('sqlite interrupt attr', lambda: hasattr(s,'interrupt'))
chk('duck stddev empty', lambda: d.execute("SELECT stddev(x) FROM (SELECT 1 AS x WHERE 0)").fetchall())
chk('sqlite stddev', lambda: s.execute("SELECT stddev(x) FROM (SELECT 1 AS x WHERE 0)").fetchall())
# timeout/interrupt behavior quick test
import threading, time
d.execute("CREATE TABLE n AS SELECT range i FROM range(10000000)")
def slow():
    try:
        t0=time.time(); d.execute("SELECT count(*) FROM n a CROSS JOIN n b CROSS JOIN n c").fetchall(); print('slow done', time.time()-t0)
    except Exception as e: print('slow interrupted:', type(e).__name__, str(e)[:100])
th=threading.Thread(target=slow); th.start(); time.sleep(0.5); d.interrupt(); th.join(3)
print('thread alive after interrupt:', th.is_alive())
# is connection reusable after interrupt?
chk('duck reusable', lambda: d.execute("SELECT 1").fetchall())
# sqlite interrupt
s.execute("CREATE TABLE n(i)"); s.executemany("INSERT INTO n VALUES(?)",[(i,) for i in range(200000)])
def slow2():
    try: s.execute("SELECT count(*) FROM n a CROSS JOIN n b CROSS JOIN n c").fetchall()
    except Exception as e: print('sqlite slow interrupted:', type(e).__name__, str(e)[:80])
th2=threading.Thread(target=slow2); th2.start(); time.sleep(0.5); s.interrupt(); th2.join(3)
print('sqlite thread alive:', th2.is_alive())
chk('sqlite reusable', lambda: s.execute("SELECT 1").fetchall())
# decimal/hugeint
chk('duck hugeint', lambda: d.execute("SELECT 9223372036854775807+1").fetchall())
chk('duck decimal', lambda: d.execute("SELECT 1.23").description)
# parameter style both
chk('duck ? param', lambda: d.execute("SELECT ?", [42]).fetchall())
chk('sqlite ? param', lambda: s.execute("SELECT ?", [42]).fetchall())
