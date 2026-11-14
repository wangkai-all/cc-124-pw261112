"""内置本地示例：一份会暴露真实差异的订单数据，以及 8 组对照案例。

每个案例包含：
- divergence：同一份 SQL 两边跑出真实不同结果（或一侧报错），用于定位迁移问题；
- fixed：针对双方言分别改写后结果一致的版本（左右 SQL 可以不同）。
所有“差异”均由 scripts/probe*.py 实测确认，未用预设结果替代。
"""
from __future__ import annotations

SAMPLE_CSV = """order_id,cust,region,amount,qty,status,order_date,note
1001,Alice,East,120.5,2,paid,2026-01-05,
1002,bob,West,,1,paid,2026-01-06,refund pending
1003,Carol,East,80,4,cancelled,2026-02-01,
1004,alice,North,300.25,1,paid,2026-02-14,VIP
1005,Bob,West,55.75,3,paid,2026-03-01,
1006,Dave,South,,2,pending,2026-03-08,
1007,Carol,East,210,5,paid,2026-03-15,
1008,erin,North,14.9,1,cancelled,2026-03-20,test order
1008,erin,North,14.9,1,cancelled,2026-03-20,test order
1009,Frank,South,990,10,paid,2026-04-01,
1010,Grace,West,42,2,pending,,missing date
"""

# 主键候选：order_id（注意 1008 故意重复，用于展示多重集合/重复次数）
SAMPLE_TABLE = "orders"
SAMPLE_KEYS = ["order_id"]

EXAMPLES = [
    {
        "id": "int_division",
        "title": "① 整数除法：7/2 得 3 还是 3.5",
        "focus": "整数除法",
        "description": "SQLite 的 / 在两个整数操作数上做整数除法（含整数字面量和整数列）；DuckDB 总是返回小数。迁移后比率类指标最常见的对不上来源。",
        "divergence": {
            "sqlite_sql": "SELECT order_id, qty, 7 / 2 AS lit_div, qty / 2 AS qty_half FROM orders ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, qty, 7 / 2 AS lit_div, qty / 2 AS qty_half FROM orders ORDER BY order_id",
        },
        "fixed": {
            "sqlite_sql": "SELECT order_id, qty, CAST(7 AS REAL) / 2 AS lit_div, CAST(qty AS REAL) / 2 AS qty_half FROM orders ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, qty, CAST(7 AS DOUBLE) / 2 AS lit_div, CAST(qty AS DOUBLE) / 2 AS qty_half FROM orders ORDER BY order_id",
        },
        "keys": ["order_id"],
        "abs_tol": 0.0,
        "rel_tol": 0.0,
        "ordered": True,
    },
    {
        "id": "null_compare",
        "title": "② 空值比较：= NULL、IS 与 IS NOT DISTINCT FROM",
        "focus": "空值比较",
        "description": "x = NULL 在两边都得 NULL（过滤掉行），这是标准三值逻辑；但 SQLite 还支持 x IS y 做空值安全比较（连 NULL 一起匹配），DuckDB 遇到 IS <字面量> 直接报语法错。可移植写法是 IS NOT DISTINCT FROM。",
        "divergence": {
            "sqlite_sql": "SELECT order_id, cust, note FROM orders WHERE cust IS 'Alice' OR note IS NULL ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, cust, note FROM orders WHERE cust IS 'Alice' OR note IS NULL ORDER BY order_id",
        },
        "fixed": {
            "sqlite_sql": "SELECT order_id, cust, note FROM orders WHERE cust IS NOT DISTINCT FROM 'Alice' OR note IS NOT DISTINCT FROM NULL ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, cust, note FROM orders WHERE cust IS NOT DISTINCT FROM 'Alice' OR note IS NOT DISTINCT FROM NULL ORDER BY order_id",
        },
        "keys": ["order_id"],
        "abs_tol": 0.0,
        "rel_tol": 0.0,
        "ordered": True,
        "dialect_note": "DuckDB 不支持 SQLite 风格的 `expr IS expr`（仅支持 IS NULL / IS NOT NULL）；IS NOT DISTINCT FROM 两边都支持。",
    },
    {
        "id": "string_numeric",
        "title": "③ 字符串与数值隐式转换：'10' * '3'",
        "focus": "字符串/数值转换",
        "description": "SQLite 动态类型会把文本数字隐式转成数值参与运算；DuckDB 强类型，拒绝为字符串选择算术函数，直接 Binder 报错。报错也是真实结果，迁移时应显式 CAST。",
        "divergence": {
            "sqlite_sql": "SELECT order_id, '10' * '3' AS text_mul, qty * '2' AS qty_mul FROM orders ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, '10' * '3' AS text_mul, qty * '2' AS qty_mul FROM orders ORDER BY order_id",
        },
        "fixed": {
            "sqlite_sql": "SELECT order_id, CAST('10' AS INTEGER) * CAST('3' AS INTEGER) AS text_mul, qty * CAST('2' AS INTEGER) AS qty_mul FROM orders ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, CAST('10' AS INTEGER) * CAST('3' AS INTEGER) AS text_mul, qty * CAST('2' AS INTEGER) AS qty_mul FROM orders ORDER BY order_id",
        },
        "keys": ["order_id"],
        "abs_tol": 0.0,
        "rel_tol": 0.0,
        "ordered": True,
    },
    {
        "id": "empty_agg",
        "title": "④ 聚合空集：COUNT / SUM / AVG 在零行输入上",
        "focus": "聚合空集",
        "description": "标准 SQL 行为两边一致：COUNT(*) 为 0，SUM/AVG/MIN/MAX 为 NULL。另演示 SQLite 专有 TOTAL() 在空集上返回 0.0 而 DuckDB 无此函数——这是真实的方言缺口。",
        "divergence": {
            "sqlite_sql": "SELECT COUNT(*) AS cnt, TOTAL(amount) AS total_amt, SUM(amount) AS sum_amt, AVG(amount) AS avg_amt FROM orders WHERE region = 'Nowhere'",
            "duckdb_sql": "SELECT COUNT(*) AS cnt, TOTAL(amount) AS total_amt, SUM(amount) AS sum_amt, AVG(amount) AS avg_amt FROM orders WHERE region = 'Nowhere'",
        },
        "fixed": {
            "sqlite_sql": "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0.0) AS total_amt, SUM(amount) AS sum_amt, AVG(amount) AS avg_amt FROM orders WHERE region = 'Nowhere'",
            "duckdb_sql": "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0.0) AS total_amt, SUM(amount) AS sum_amt, AVG(amount) AS avg_amt FROM orders WHERE region = 'Nowhere'",
        },
        "keys": [],
        "abs_tol": 0.0,
        "rel_tol": 0.0,
        "ordered": False,
        "dialect_note": "TOTAL() 是 SQLite 专有函数；DuckDB 对应写法是 COALESCE(SUM(x), 0)。",
    },
    {
        "id": "time_fields",
        "title": "⑤ 时间字段：日期存储类型、日期加减与 strftime 参数顺序",
        "focus": "时间字段",
        "description": "SQLite 无原生日期类型，order_date 以 TEXT 存储，文本相减被强制转成数字（2026-01-05 - 3 得 2023）；DuckDB 是原生 DATE，日期减整数得到前 3 天。strftime 两边参数顺序也相反。",
        "divergence": {
            "sqlite_sql": "SELECT order_id, order_date, order_date - 3 AS minus_three, strftime('%Y-%m', order_date) AS ym FROM orders WHERE order_date IS NOT NULL ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, order_date, order_date - 3 AS minus_three, strftime('%Y-%m', order_date) AS ym FROM orders WHERE order_date IS NOT NULL ORDER BY order_id",
        },
        "fixed": {
            "sqlite_sql": "SELECT order_id, order_date, DATE(order_date, '-3 day') AS minus_three, strftime('%Y-%m', order_date) AS ym FROM orders WHERE order_date IS NOT NULL ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, order_date, CAST(CAST(order_date - INTERVAL 3 DAY AS DATE) AS VARCHAR) AS minus_three, strftime(order_date, '%Y-%m') AS ym FROM orders WHERE order_date IS NOT NULL ORDER BY order_id",
        },
        "keys": ["order_id"],
        "abs_tol": 0.0,
        "rel_tol": 0.0,
        "ordered": True,
        "dialect_note": "SQLite 用 DATE(d, '-3 day') 修饰符；DuckDB 用 d - INTERVAL 3 DAY，注意其结果是 TIMESTAMP，要与 SQLite 的 DATE 文本对齐需再 CAST 成 DATE/VARCHAR。SQLite 的 strftime 是 (格式, 日期)，DuckDB 原生签名是 (日期, 格式)。",
    },
    {
        "id": "like_case",
        "title": "⑥ LIKE 大小写与布尔表示：'alice' 匹配与 0/1 vs true/false",
        "focus": "字符串比较/类型表示",
        "description": "SQLite 的 LIKE 默认对 ASCII 不区分大小写；DuckDB 的 LIKE 区分大小写（不区分要用 ILIKE，而 ILIKE 在 SQLite 是语法错）。另外 SQLite 布尔是整数 0/1，DuckDB 是原生 true/false——值等价但类型与显示不同。",
        "divergence": {
            "sqlite_sql": "SELECT order_id, cust, (status = 'paid') AS is_paid FROM orders WHERE cust LIKE 'a%' ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, cust, (status = 'paid') AS is_paid FROM orders WHERE cust LIKE 'a%' ORDER BY order_id",
        },
        "fixed": {
            "sqlite_sql": "SELECT order_id, cust, CASE WHEN status = 'paid' THEN 1 ELSE 0 END AS is_paid FROM orders WHERE LOWER(cust) LIKE 'a%' ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, cust, CASE WHEN status = 'paid' THEN TRUE ELSE FALSE END AS is_paid FROM orders WHERE LOWER(cust) LIKE 'a%' ORDER BY order_id",
        },
        "keys": ["order_id"],
        "abs_tol": 0.0,
        "rel_tol": 0.0,
        "ordered": True,
        "dialect_note": "可移植的不区分大小写匹配是 LOWER(x) LIKE 'a%'；ILIKE 仅 DuckDB 支持。",
    },
    {
        "id": "div_zero",
        "title": "⑦ 除零：NULL 还是 Infinity",
        "focus": "数值边界",
        "description": "整数除零时 SQLite 返回 NULL，DuckDB 返回 Infinity（浮点）。下游若用 IS NULL 做除零保护，迁移到 DuckDB 会失效。统一办法是显式 NULLIF 分母。",
        "divergence": {
            "sqlite_sql": "SELECT order_id, amount, qty, amount / (qty - qty) AS bad_div FROM orders ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, amount, qty, amount / (qty - qty) AS bad_div FROM orders ORDER BY order_id",
        },
        "fixed": {
            "sqlite_sql": "SELECT order_id, amount, qty, amount / NULLIF(qty - qty, 0) AS bad_div FROM orders ORDER BY order_id",
            "duckdb_sql": "SELECT order_id, amount, qty, amount / NULLIF(qty - qty, 0) AS bad_div FROM orders ORDER BY order_id",
        },
        "keys": ["order_id"],
        "abs_tol": 0.0,
        "rel_tol": 0.0,
        "ordered": True,
    },
    {
        "id": "multiset_dup",
        "title": "⑧ 无序结果与重复行：集合比较而非逐行硬比",
        "focus": "多重集合/重复次数",
        "description": "无 ORDER BY 的结果 SQL 标准不保证返回顺序，order_id=1008 还是故意重复的行。集合模式按多重集合比较（重复次数也纳入），行序不同不会报假差异；但真正的重复次数变化（这里 DuckDB 侧过滤掉两条 1008，只剩 1009 一行）一定会被捕获。注意条件要写 OR note IS NULL，否则 NULL 行会被三值逻辑一起过滤。",
        "divergence": {
            "sqlite_sql": "SELECT order_id FROM orders WHERE order_id IN (1008, 1009)",
            "duckdb_sql": "SELECT order_id FROM orders WHERE order_id IN (1008, 1009) AND (note <> 'test order' OR note IS NULL)",
        },
        "fixed": {
            "sqlite_sql": "SELECT order_id FROM orders WHERE order_id IN (1008, 1009) ORDER BY order_id",
            "duckdb_sql": "SELECT order_id FROM orders WHERE order_id IN (1008, 1009) ORDER BY order_id",
        },
        "keys": [],
        "abs_tol": 0.0,
        "rel_tol": 0.0,
        "ordered": False,
        "dialect_note": "无 ORDER BY 时 SQL 标准不保证行序；要逐行比较必须两边都显式 ORDER BY 相同的键。集合模式下重复行按次数参与比较，不会被 DISTINCT 式去重掩盖。",
    },
]

# 方言速查（全部经探针实测），用于页面“不伪装成通用标准”的说明
DIALECT_NOTES = [
    {"topic": "整数除法", "sqlite": "整数/整数 -> 整数（截断）", "duckdb": "整数/整数 -> 小数", "portable": "先 CAST 成 REAL/DOUBLE 再除"},
    {"topic": "除零", "sqlite": "返回 NULL", "duckdb": "返回 Infinity / -Infinity", "portable": "分母包 NULLIF(x, 0)"},
    {"topic": "字符串算术", "sqlite": "文本数字隐式转换后计算", "duckdb": "Binder 报错，要求显式 CAST", "portable": "CAST(text AS 数值类型)"},
    {"topic": "LIKE 大小写", "sqlite": "ASCII 不区分大小写", "duckdb": "区分大小写；不区分用 ILIKE", "portable": "LOWER(x) LIKE 'a%'"},
    {"topic": "空值安全相等", "sqlite": "支持 IS、IS NOT DISTINCT FROM", "duckdb": "仅 IS NULL；支持 IS NOT DISTINCT FROM", "portable": "统一用 IS NOT DISTINCT FROM"},
    {"topic": "日期类型", "sqlite": "无原生类型，TEXT 存 ISO 字符串", "duckdb": "原生 DATE / TIMESTAMP", "portable": "统一 ISO-8601 文本语义，用各自日期函数运算"},
    {"topic": "日期加减", "sqlite": "DATE(d, '-3 day')", "duckdb": "d - INTERVAL 3 DAY", "portable": "无共同写法，需按引擎分别构造"},
    {"topic": "strftime 参数", "sqlite": "strftime(格式, 日期)", "duckdb": "strftime(日期, 格式)", "portable": "按各引擎原生参数顺序书写"},
    {"topic": "空集聚合", "sqlite": "COUNT=0；SUM/AVG=NULL；专有 TOTAL()=0.0", "duckdb": "COUNT=0；SUM/AVG=NULL；无 TOTAL()", "portable": "COALESCE(SUM(x), 0)"},
    {"topic": "布尔类型", "sqlite": "无原生布尔，存 0/1 整数", "duckdb": "原生 BOOLEAN true/false", "portable": "比较时 1 与 true 等价，注意展示差异"},
    {"topic": "字符串聚合", "sqlite": "GROUP_CONCAT(x)", "duckdb": "STRING_AGG(x, sep)（也兼容 GROUP_CONCAT）", "portable": "用 GROUP_CONCAT 两边均可"},
    {"topic": "统计函数", "sqlite": "无内置 STDDEV", "duckdb": "stddev/stddev_samp", "portable": "SQLite 需自行实现或迁移后替换"},
    {"topic": "无别名列名", "sqlite": "保留表达式原文，如 count(*)", "duckdb": "规范化为函数名，如 count_star()", "portable": "统一显式 AS 起别名"},
]
