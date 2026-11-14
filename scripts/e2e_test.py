"""端到端冒烟测试：走真实 HTTP API，覆盖全部示例、对照模式、安全拦截、下钻。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8137"
PASS, FAIL = 0, 0


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


st, data = call("POST", "/api/session")
sid = data["session_id"]
print(f"会话: {sid[:8]}")

st, data = call("POST", f"/api/session/{sid}/sample")
check("示例导入 200", st == 200)
check("表名 orders", data["loaded"]["table"] == "orders")
check("11 行数据（含重复 1008）", data["loaded"]["row_count"] == 11, str(data["loaded"]["row_count"]))
schema = data["schema"][0]
check("SQLite 日期列为 TEXT", next(c for c in schema["columns"] if c["name"] == "order_date")["sqlite_declared"] == "TEXT")
check("DuckDB 日期列为 DATE", next(c for c in schema["columns"] if c["name"] == "order_date")["duckdb_declared"] == "DATE")
check("DDL 双引擎都在", "CREATE TABLE" in schema["ddl"]["sqlite"] and "CREATE TABLE" in schema["ddl"]["duckdb"])

st, meta = call("GET", "/api/meta")
examples = {e["id"]: e for e in meta["examples"]}


def run_example(ex_id, version):
    ex = examples[ex_id]
    v = ex[version]
    return call("POST", f"/api/session/{sid}/query", {
        "sqlite_sql": v["sqlite_sql"], "duckdb_sql": v["duckdb_sql"],
        "key_columns": ex["keys"], "abs_tol": ex["abs_tol"], "rel_tol": ex["rel_tol"],
        "ordered": ex["ordered"], "timeout_sec": 5,
    })


print("\n== 8 个案例：差异版必须 different/error，修复版必须 identical ==")
for ex_id in ["int_division", "null_compare", "string_numeric", "empty_agg",
              "time_fields", "like_case", "div_zero", "multiset_dup"]:
    st, d = run_example(ex_id, "divergence")
    rep = d["comparison"]
    check(f"[{ex_id}] 差异版状态={rep['status']}", rep["status"] in ("different", "error"),
          rep["summary"])
    st, f = run_example(ex_id, "fixed")
    frep = f["comparison"]
    check(f"[{ex_id}] 修复版 identical", frep["status"] == "identical",
          f"{frep['status']}: {frep['summary']}")

print("\n== 关键差异内容抽查 ==")
st, d = run_example("int_division", "divergence")
rs, rd = d["results"]
# 列：order_id, qty, lit_div(7/2), qty_half(qty/2)
check("整数除法 SQLite 7/2=3", any(r[2] == 3 for r in rs["rows"]), str(rs["rows"][0]))
r_s = next(r for r in rs["rows"] if r[0] == 1003)  # qty=4
r_d = next(r for r in rd["rows"] if r[0] == 1003)
check("4/2 SQLite 整数 2", r_s[3] == 2 and isinstance(r_s[3], int), str(r_s))
check("4/2 DuckDB 小数 2.0", r_d[3] == 2.0 and isinstance(r_d[3], float), str(r_d))
check("DuckDB 结果类型含 DOUBLE", any("DOUBLE" in t or "FLOAT" in t for t in rd["column_types"]), str(rd["column_types"]))

st, d = run_example("div_zero", "divergence")
rs, rd = d["results"]
check("除零 SQLite=NULL", all(r[3] is None for r in rs["rows"] if r[2] is not None))
check("除零 DuckDB=Infinity 特殊标记",
      any(isinstance(r[3], dict) and r[3].get("__special__") == "Infinity" for r in rd["rows"]),
      str(rd["rows"][:2]))

st, d = run_example("string_numeric", "divergence")
rs, rd = d["results"]
check("字符串乘法 SQLite 成功=30", rs["ok"] and rs["rows"][0][1] == 30)
check("字符串乘法 DuckDB 报错保留", not rd["ok"] and "Binder" in (rd["error_kind"] or ""), rd.get("error_kind"))
check("一边报错时比较状态 error", d["comparison"]["status"] == "error")

st, d = run_example("empty_agg", "divergence")
rs, rd = d["results"]
check("TOTAL() SQLite 空集=0.0", rs["ok"] and rs["rows"][0][1] == 0.0)
check("TOTAL() DuckDB Catalog 报错", not rd["ok"] and rd["error_kind"] == "CatalogException")

st, d = run_example("time_fields", "divergence")
rs, rd = d["results"]
s1001 = next(r for r in rs["rows"] if r[0] == 1001)
d1001 = next(r for r in rd["rows"] if r[0] == 1001)
check("SQLite 文本日期减3=2023", s1001[2] == 2023, str(s1001))
check("DuckDB 日期减3=2026-01-02", d1001[2] == "2026-01-02", str(d1001))
check("DuckDB 列类型 DATE", rd["column_types"][2] == "DATE", str(rd["column_types"]))

st, d = run_example("like_case", "divergence")
rs, rd = d["results"]
s_ids = {r[0] for r in rs["rows"]}
d_ids = {r[0] for r in rd["rows"]}
check("SQLite LIKE 匹配 alice/Alice 两条", s_ids == {1001, 1004}, str(s_ids))
check("DuckDB LIKE 只匹配小写 alice(1004)", d_ids == {1004}, str(d_ids))
check("SQLite 布尔显示 0/1 整数", all(v[2] in (0, 1) for v in rs["rows"]))
check("DuckDB 布尔为 true/false", all(isinstance(v[2], bool) for v in rd["rows"]))

print("\n== 容差与集合模式 ==")
# 手工构造：两边差 0.001，abs_tol=0.01 应一致
body = {
    "sqlite_sql": "SELECT order_id, CAST(amount AS REAL) + 0.001 AS a FROM orders WHERE amount IS NOT NULL ORDER BY order_id",
    "duckdb_sql": "SELECT order_id, amount AS a FROM orders WHERE amount IS NOT NULL ORDER BY order_id",
    "key_columns": ["order_id"], "abs_tol": 0.01, "rel_tol": 0, "ordered": True, "timeout_sec": 5,
}
st, d = call("POST", f"/api/session/{sid}/query", body)
check("绝对容差内判定一致", d["comparison"]["status"] == "identical", d["comparison"]["summary"])
body["abs_tol"] = 0.0001
st, d = call("POST", f"/api/session/{sid}/query", body)
check("超出容差判定 different", d["comparison"]["status"] == "different", d["comparison"]["summary"])

# 集合模式：不同顺序 + 重复行，不应报假差异
body = {
    "sqlite_sql": "SELECT order_id, region FROM orders",
    "duckdb_sql": "SELECT order_id, region FROM orders ORDER BY order_id DESC",
    "key_columns": [], "abs_tol": 0, "rel_tol": 0, "ordered": False, "timeout_sec": 5,
}
st, d = call("POST", f"/api/session/{sid}/query", body)
check("无序集合模式忽略行序（含重复行）", d["comparison"]["status"] == "identical", d["comparison"]["summary"])

# 删一行造成真实多重集合差异
body = {
    "sqlite_sql": "SELECT order_id FROM orders",
    "duckdb_sql": "SELECT order_id FROM orders WHERE order_id <> 1010",
    "key_columns": [], "ordered": False, "timeout_sec": 5,
}
st, d = call("POST", f"/api/session/{sid}/query", body)
rep = d["comparison"]
check("缺少行报 only_in_sqlite", rep["status"] == "different" and
      any(x["kind"] == "only_in_sqlite" for x in rep["row_diffs"]), rep["summary"])

# 重复次数差异：SQLite 侧 1008 出现两次（表内故意重复）；
# DuckDB 侧过滤掉 test order，但要 OR note IS NULL 保住 NULL 的 1009（三值逻辑）
body = {
    "sqlite_sql": "SELECT order_id FROM orders WHERE order_id IN (1008, 1009)",
    "duckdb_sql": "SELECT order_id FROM orders WHERE order_id IN (1008, 1009) AND (note <> 'test order' OR note IS NULL)",
    "key_columns": [], "ordered": False, "timeout_sec": 5,
}
st, d = call("POST", f"/api/session/{sid}/query", body)
cnt_s = d["results"][0]
cnt_d = d["results"][1]
check("重复行 SQLite 3 行（1008×2+1009）", len(cnt_s["rows"]) == 3, str(cnt_s["rows"]))
check("重复行 DuckDB 1 行（仅 1009）", len(cnt_d["rows"]) == 1 and cnt_d["rows"][0][0] == 1009, str(cnt_d["rows"]))
check("重复次数差异被捕获", d["comparison"]["status"] == "different")

# 有序模式下行序不同必报差异
body = {
    "sqlite_sql": "SELECT order_id FROM orders ORDER BY order_id",
    "duckdb_sql": "SELECT order_id FROM orders ORDER BY order_id DESC",
    "key_columns": [], "ordered": True, "timeout_sec": 5,
}
st, d = call("POST", f"/api/session/{sid}/query", body)
check("有序模式捕获行序差异", d["comparison"]["status"] == "different")

print("\n== 主键下钻源行 ==")
body = {
    "sqlite_sql": "SELECT order_id, amount, qty FROM orders ORDER BY order_id",
    "duckdb_sql": "SELECT order_id, amount, qty + 1 AS qty FROM orders ORDER BY order_id",
    "key_columns": ["order_id"], "ordered": True, "timeout_sec": 5,
}
st, d = call("POST", f"/api/session/{sid}/query", body)
diff = next(x for x in d["comparison"]["row_diffs"] if x["kind"] == "value_diff")
kv = diff["key_values"]
st, src = call("POST", f"/api/session/{sid}/source-row",
               {"table": "orders", "key_columns": ["order_id"], "key_values": [kv["order_id"]]})
check("下钻接口 200", st == 200)
check("两引擎都返回源行", len(src["source"]["sqlite"]["rows"]) >= 1 and
      len(src["source"]["duckdb"]["rows"]) >= 1)
check("源行带类型信息", all(src["source"][e]["types"] for e in ("sqlite", "duckdb")))
st, bad = call("POST", f"/api/session/{sid}/source-row",
               {"table": "orders", "key_columns": ["nope"], "key_values": [1]})
check("下钻非法主键报错", st == 500 and ("不在表" in bad["error"] or "不存在" in bad["error"]), bad.get("error", ""))

print("\n== SQL Guard 安全拦截 ==")
guarded = [
    ("DELETE", "DELETE FROM orders"),
    ("多语句", "SELECT 1; DROP TABLE orders"),
    ("PRAGMA", "PRAGMA database_list"),
    ("ATTACH", "ATTACH '/tmp/x.db' AS x"),
    ("read_csv", "SELECT * FROM read_csv('x.csv')"),
    ("路径片段", "SELECT * FROM orders WHERE note = ../etc"),
    ("非 SELECT", "WITH x AS (SELECT 1) CREATE TABLE y AS SELECT * FROM x"),
]
for name, sql in guarded:
    st, d = call("POST", f"/api/session/{sid}/query",
                 {"sqlite_sql": sql, "duckdb_sql": "SELECT 1", "timeout_sec": 5})
    check(f"拦截 {name}", st == 400 and "安全检查" in d["error"], str(d.get("error")))
# 注释和字符串里的关键字是惰性内容，剥离后不应误杀；除法 / 也不应被路径规则误伤
st, d = call("POST", f"/api/session/{sid}/query", {
    "sqlite_sql": "SELECT 1 /* INSERT is inert in a comment */ AS x, amount / qty AS r FROM orders LIMIT 1",
    "duckdb_sql": "SELECT 1 /* INSERT is inert in a comment */ AS x, amount / qty AS r FROM orders LIMIT 1",
    "timeout_sec": 5})
check("注释内关键字与除法运算符不误杀", st == 200)

print("\n== 超时与隔离 ==")
st, d = call("POST", f"/api/session/{sid}/query", {
    "sqlite_sql": "SELECT count(*) FROM orders a CROSS JOIN orders b CROSS JOIN orders c CROSS JOIN orders d CROSS JOIN orders e",
    "duckdb_sql": "SELECT 1", "timeout_sec": 1})
check("1 秒超时被中断/重建，不挂死", st == 200)
# 超时后连接仍可用（重建或 interrupt 后复用）；显式别名避免 DuckDB 的 count_star() 列名规范化
st, d = call("POST", f"/api/session/{sid}/query", {
    "sqlite_sql": "SELECT count(*) AS n FROM orders",
    "duckdb_sql": "SELECT count(*) AS n FROM orders",
    "timeout_sec": 5})
check("超时后引擎仍可查询", st == 200 and d["comparison"]["status"] == "identical",
      d.get("comparison", {}).get("summary"))

st, d = call("GET", "/api/session/badid/schema")
check("非法会话 id 拒绝", st == 404)

print("\n== 页面静态资源 ==")
for path in ("/", "/static/style.css", "/static/app.js"):
    with urllib.request.urlopen(BASE + path) as r:
        check(f"GET {path} -> 200", r.status == 200)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
