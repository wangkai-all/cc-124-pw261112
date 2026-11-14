"""补充验证：CSV 上传、类型覆盖、隔离、DuckDB 侧超时、边界类型。"""
import io
import json
import urllib.request

BASE = "http://127.0.0.1:8137"
PASS = FAIL = 0


def call(method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  ✓", name)
    else:
        FAIL += 1; print("  ✗", name, detail)


def multipart(path, fields: dict, filename: str, content: bytes):
    boundary = "----wbnd7m3x"
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n".encode())
    buf.write(b"Content-Type: text/csv\r\n\r\n")
    buf.write(content); buf.write(f"\r\n--{boundary}--\r\n".encode())
    return call("POST", path, raw=buf.getvalue(),
                ctype=f"multipart/form-data; boundary={boundary}")


_, d = call("POST", "/api/session"); sid = d["session_id"]

print("== 上传：布尔/时间戳/整数小数混合/不齐整行 ==")
csv_bytes = (
    "id,flag,score,ts,mixed\n"
    "1,true,1.5,2026-03-07 10:20:30,100\n"
    "2,false,2,2026-03-08T11:00:00,200.25\n"
    "3,true,,2026-03-09,\n"
    "4,false,4.0,2026-03-10 09:00,300,extra_col\n"
).encode()
st, d = multipart(f"/api/session/{sid}/upload", {}, "widgets.csv", csv_bytes)
check("上传 200", st == 200, str(d.get("error")))
tbl = d["loaded"]
check("表名 widgets", tbl["table"] == "widgets")
check("4 行导入（含不齐行）", tbl["row_count"] == 4)
types = {c["name"]: c["logical_type"] for c in tbl["columns"]}
check("布尔列识别", types["flag"] == "BOOLEAN", str(types))
check("时间戳列识别", types["ts"] == "TIMESTAMP", str(types))
check("整数小数混合列->REAL", types["mixed"] == "REAL", str(types))
check("score 含空值仍识别 REAL", types["score"] == "REAL", str(types))
check("不齐行有导入说明", any("列数" in n for n in tbl["import_notes"]))
dd = {c["name"]: c for c in tbl["columns"]}
check("SQLite 布尔声明 BOOLEAN 亲和", dd["flag"]["sqlite_declared"] == "BOOLEAN")
check("DuckDB 布尔原生 BOOLEAN", dd["flag"]["duckdb_declared"] == "BOOLEAN")
check("SQLite 时间戳 TEXT", dd["ts"]["sqlite_declared"] == "TEXT")
check("DuckDB 时间戳 TIMESTAMP", dd["ts"]["duckdb_declared"] == "TIMESTAMP")

# 真实查询验证布尔与时间戳的两边表现
st, d = call("POST", f"/api/session/{sid}/query", {
    "sqlite_sql": "SELECT id, flag, ts FROM widgets ORDER BY id",
    "duckdb_sql": "SELECT id, flag, ts FROM widgets ORDER BY id",
    "key_columns": ["id"], "ordered": True, "timeout_sec": 5})
rep = d["comparison"]
check("布尔 0/1 vs true 判定值等价", rep["status"] == "identical", rep["summary"])
check("标注了表示形式差异", rep["rep_only_count"] >= 1)
check("DuckDB ts 列类型 TIMESTAMP", "TIMESTAMP" in d["results"][1]["column_types"][2])

print("== 类型覆盖：把 score 强制成 TEXT ==")
_, d2 = call("POST", "/api/session"); sid2 = d2["session_id"]
st, d = multipart(f"/api/session/{sid2}/upload",
                  {"type_overrides": json.dumps({"score": "TEXT"})},
                  "w2.csv", csv_bytes)
check("覆盖上传 200", st == 200, str(d.get("error")))
t2 = {c["name"]: c["logical_type"] for c in d["loaded"]["columns"]}
check("score 被覆盖为 TEXT", t2["score"] == "TEXT", str(t2))
# TEXT 列做 SUM：DuckDB 应报错（强类型），SQLite 隐式转换成功（表名取自上传文件名 w2）
tname2 = d["loaded"]["table"]
st, d = call("POST", f"/api/session/{sid2}/query", {
    "sqlite_sql": f"SELECT SUM(score) AS s FROM {tname2}",
    "duckdb_sql": f"SELECT SUM(score) AS s FROM {tname2}", "timeout_sec": 5})
check("TEXT 聚合：SQLite 隐式转换成功", d["results"][0]["ok"], str(d["results"][0].get("error")))
check("TEXT 聚合：DuckDB 强类型报错", not d["results"][1]["ok"])

print("== 会话隔离 ==")
st, d = call("GET", f"/api/session/{sid}/schema")
check("会话 A 有 widgets", any(t["table"] == "widgets" for t in d["schema"]))
st, d = call("GET", f"/api/session/{sid2}/schema")
check("会话 B 看不到 widgets（独立表名 w2）",
      all(t["table"] != "widgets" for t in d["schema"]))
# 会话 B 无法查询会话 A 的表
st, d = call("POST", f"/api/session/{sid2}/query", {
    "sqlite_sql": "SELECT * FROM widgets", "duckdb_sql": "SELECT * FROM widgets", "timeout_sec": 5})
check("跨会话访问表失败", d["comparison"]["status"] == "error")

print("== 上传限制 ==")
big = ("a,b\n" + ("1,x\n" * 2500)).encode()
_, d3 = call("POST", "/api/session"); sid3 = d3["session_id"]
st, d = multipart(f"/api/session/{sid3}/upload", {}, "big.csv", big)
check("超行数截断到 2000 并带说明", st == 200 and d["loaded"]["row_count"] == 2000)
huge = b"a\n" + b"1\n" * 5
# 2MB 上限
_, d4 = call("POST", "/api/session"); sid4 = d4["session_id"]
st, d = multipart(f"/api/session/{sid4}/upload", {}, "huge.csv", b"x\n" + b"a" * (2 * 1024 * 1024 + 10))
check("超 2MB 拒绝", st == 500 and "上限" in d["error"])

print("== DuckDB 侧重查询超时 ==")
_, d5 = call("POST", "/api/session"); sid5 = d5["session_id"]
call("POST", f"/api/session/{sid5}/sample")
st, d = call("POST", f"/api/session/{sid5}/query", {
    "sqlite_sql": "SELECT 1",
    "duckdb_sql": "SELECT count(*) FROM orders a CROSS JOIN orders b CROSS JOIN orders c CROSS JOIN orders d CROSS JOIN orders e CROSS JOIN orders f",
    "timeout_sec": 1})
check("DuckDB 1s 超时不挂死", st == 200)
st, d = call("POST", f"/api/session/{sid5}/query", {
    "sqlite_sql": "SELECT count(*) AS n FROM orders",
    "duckdb_sql": "SELECT count(*) AS n FROM orders", "timeout_sec": 5})
check("DuckDB 超时后连接重建可复用", st == 200 and d["comparison"]["status"] == "identical",
      str(d.get("comparison", {}).get("summary")))

print("== 特殊值：NULL/Infinity/NaN 渲染 ==")
_, d6 = call("POST", "/api/session"); sid6 = d6["session_id"]
call("POST", f"/api/session/{sid6}/sample")
st, d = call("POST", f"/api/session/{sid6}/query", {
    "sqlite_sql": "SELECT 1.0/0 AS inf, 0.0/0 AS nan, NULL AS n",
    "duckdb_sql": "SELECT 1.0/0 AS inf, 0.0/0 AS nan, NULL AS n", "timeout_sec": 5})
rs, rd = d["results"]
check("SQLite 除零为 NULL", rs["rows"][0][0] is None)
check("DuckDB Infinity 特殊标记", rd["rows"][0][0] == {"__special__": "Infinity"})
check("DuckDB NaN 特殊标记", rd["rows"][0][1] == {"__special__": "NaN"})

print(f"\n补充验证: {PASS} 通过 / {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
