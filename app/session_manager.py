"""隔离会话管理：每个会话拥有独立的内存引擎连接、独立工作目录和锁。

安全边界：
- 仅允许单条 SELECT/WITH 语句（sql_guard 做词法级检查）；
- 查询在工作线程执行，超时调用 interrupt()，线程仍不结束则重建连接；
- 上传数据写入隔离目录，引擎全部使用 :memory:，无法触达任何文件；
- 每个会话串行执行（engine lock），避免连接跨线程并发使用。
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

import duckdb
import sqlite3

from . import engine_abstraction as ea

MAX_UPLOAD_BYTES = 2 * 1024 * 1024      # 2MB 原始 CSV 上限
MAX_ROWS = 2000                         # 导入行数上限
MAX_RESULT_ROWS = 500                   # 单次结果保留行数上限
DEFAULT_TIMEOUT_SEC = 5
MAX_TIMEOUT_SEC = 15

# 禁止出现的关键字（词法匹配，注释/字符串内也会被去掉后再查）
_FORBIDDEN = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH",
    "DETACH", "REPLACE", "TRUNCATE", "PRAGMA", "VACUUM", "GRANT",
    "REVOKE", "CALL", "EXEC", "EXECUTE", "MERGE", "COPY", "EXPORT",
    "IMPORT", "LOAD", "INSTALL", "CHECKPOINT",
]
_FORBIDDEN_FUNCS = [
    "READ_CSV", "READ_PARQUET", "READ_JSON", "READ_TEXT", "READ_BLOB",
    "WRITE_CSV", "WRITE_PARQUET", "GLOB", "LIST_FILES", "READ_PDF",
    "SQLITE_SCANNER", "POSTGRES_SCANNER", "ATTACH",
]
# 真正的文件路径：相对路径前缀 ./ ../、Windows 盘符、或带已知文件扩展名后接分隔符。
# 不拦截裸 word/word 与 1.0/0 这类无空格除法；字符串字面量内的路径已在剥离阶段处理。
_PATH_TOKEN = re.compile(
    r"(\.\.[/\\]|\.[/\\]|[A-Za-z]:\\|\.(?:csv|tsv|parquet|json|txt|db|sqlite3?|sql|"
    r"xlsx?|xls|avro|orc|log|tmp|dat)(?=[/\\]))",
    re.IGNORECASE,
)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqlGuardError(ValueError):
    pass


def _strip_comments_and_strings(sql: str) -> str:
    """把字符串字面量和注释替换为空格，保留结构，用于关键字扫描。"""
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            quote = c
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif c == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _split_statements(plain: str) -> list[str]:
    return [part.strip() for part in plain.split(";") if part.strip()]


def sql_guard(sql: str) -> None:
    """对用户查询做只读/单语句/无路径检查。不通过直接抛 SqlGuardError。"""
    if not sql or not sql.strip():
        raise SqlGuardError("SQL 为空")
    if len(sql) > 20000:
        raise SqlGuardError("SQL 长度超过 20000 字符限制")
    plain = _strip_comments_and_strings(sql)
    stmts = _split_statements(plain)
    if len(stmts) != 1:
        raise SqlGuardError("只允许执行单条语句，检测到多个语句或多余的分号")
    head = stmts[0].lstrip("(").strip().upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise SqlGuardError("只允许 SELECT / WITH ... SELECT 只读查询")
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", plain))
    upper = {t.upper() for t in tokens}
    hit = [k for k in _FORBIDDEN if k in upper]
    if hit:
        raise SqlGuardError(f"包含被禁止的关键字: {', '.join(hit)}")
    hit_f = [k for k in _FORBIDDEN_FUNCS if k in upper]
    if hit_f:
        raise SqlGuardError(f"包含被禁止的函数（禁止访问文件/外部数据源）: {', '.join(hit_f)}")
    if _PATH_TOKEN.search(plain):
        raise SqlGuardError("查询中不允许出现路径（/、\\、..），本工具只能访问已导入的内存表")


@dataclass
class LoadedTable:
    name: str
    columns: list[ea.ColumnSpec]
    ddl: dict[str, str]
    row_count: int
    import_notes: list[str] = field(default_factory=list)


@dataclass
class EngineResult:
    engine: str
    ok: bool
    columns: list[str] = field(default_factory=list)
    column_types: list[str] = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False
    error: str | None = None
    error_kind: str | None = None
    duration_ms: float = 0.0


class Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.sqlite = sqlite3.connect(":memory:", check_same_thread=False)
        self.duckdb = duckdb.connect(":memory:")
        self.lock = threading.RLock()
        self.tables: dict[str, LoadedTable] = {}
        self.created_at = time.time()
        # 重建连接时需要重放的数据：table -> (columns, python rows)
        self._raw_data: dict[str, tuple[list[ea.ColumnSpec], list[tuple]]] = {}

    # ---------------- 数据导入 ----------------
    def load_csv(self, filename: str, content: bytes, table_name: str | None = None,
                 type_overrides: dict[str, str] | None = None) -> LoadedTable:
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(f"文件超过 {MAX_UPLOAD_BYTES // 1024}KB 上限")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError("CSV 为空，缺少表头")
        raw_rows = []
        for i, row in enumerate(reader):
            if i >= MAX_ROWS:
                break
            raw_rows.append(row)
        if not raw_rows:
            raise ValueError("CSV 只有表头，没有数据行")
        table = self._unique_table_name(table_name or filename)
        cols = self._build_columns(header, raw_rows, type_overrides or {})
        notes = []
        py_rows = []
        for r_idx, row in enumerate(raw_rows):
            if len(row) != len(cols):
                notes.append(f"第 {r_idx + 2} 行列数({len(row)})与表头({len(cols)})不一致，已按 NULL/截断对齐")
                row = (row + [None] * len(cols))[: len(cols)]
            py_rows.append(tuple(ea.parse_value(c, col.logical_type) for c, col in zip(row, cols)))
        notes.append(f"推断类型: " + ", ".join(f"{c.name}={c.logical_type}" for c in cols))
        with self.lock:
            ddl = ea.build_ddl(table, cols)
            self._create_and_insert(table, cols, py_rows, ddl)
            loaded = LoadedTable(table, cols, ddl, len(py_rows), notes)
            self.tables[table] = loaded
            self._raw_data[table] = (cols, py_rows)
            return loaded

    def _build_columns(self, header: list[str], raw_rows: list[list[str]],
                       overrides: dict[str, str]) -> list[ea.ColumnSpec]:
        names = []
        used = set()
        for idx, h in enumerate(header):
            name = (h or f"col_{idx + 1}").strip()
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                name = re.sub(r"[^A-Za-z0-9_]", "_", name) or f"col_{idx + 1}"
                if name[0].isdigit():
                    name = "c_" + name
            base, k = name, 1
            while name.lower() in used:
                k += 1
                name = f"{base}_{k}"
            used.add(name.lower())
            names.append(name)
        cols = []
        for idx, name in enumerate(names):
            sample = [r[idx] if idx < len(r) else "" for r in raw_rows]
            ltype = overrides.get(name) or ea.infer_column_type(sample)
            if ltype not in ea.LOGICAL_TYPES:
                raise ValueError(f"不支持的类型覆盖: {ltype}")
            cols.append(ea.ColumnSpec(name, ltype))
        return cols

    def _unique_table_name(self, requested: str) -> str:
        base = re.sub(r"\.[Cc][Ss][Vv]$", "", requested)
        base = re.sub(r"[^A-Za-z0-9_]", "_", base) or "upload"
        if base[0].isdigit():
            base = "t_" + base
        name, k = base, 1
        with self.lock:
            existing = {t.lower() for t in self.tables}
            while name.lower() in existing:
                k += 1
                name = f"{base}_{k}"
        return name

    def _create_and_insert(self, table: str, cols: list[ea.ColumnSpec],
                           py_rows: list[tuple], ddl: dict[str, str]) -> None:
        self.sqlite.execute(ddl["sqlite"])
        self.duckdb.execute(ddl["duckdb"])
        placeholders = ", ".join("?" * len(cols))
        qident = ea.quote_ident(table)
        s_sql = f"INSERT INTO {qident} VALUES ({placeholders})"
        s_rows = [tuple(ea.to_sqlite_value(v, c.logical_type) for v, c in zip(row, cols))
                  for row in py_rows]
        d_rows = [tuple(ea.to_duckdb_value(v, c.logical_type) for v, c in zip(row, cols))
                  for row in py_rows]
        self.sqlite.executemany(s_sql, s_rows)
        self.duckdb.executemany(s_sql, d_rows)

    # ---------------- 表结构自省 ----------------
    def schema_info(self) -> list[dict]:
        out = []
        with self.lock:
            for name, tbl in self.tables.items():
                sqlite_cols = self.sqlite.execute(
                    f'PRAGMA table_info({ea.quote_ident(name)})').fetchall()
                duck_cols = self.duckdb.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = ? ORDER BY ordinal_position", [name]).fetchall()
                out.append({
                    "table": name,
                    "row_count": tbl.row_count,
                    "ddl": tbl.ddl,
                    "columns": [
                        {
                            "name": col.name,
                            "logical_type": col.logical_type,
                            "sqlite_declared": sqlite_cols[i][2] if i < len(sqlite_cols) else col.sqlite_decl,
                            "duckdb_declared": duck_cols[i][1] if i < len(duck_cols) else col.duckdb_decl,
                        }
                        for i, col in enumerate(tbl.columns)
                    ],
                    "import_notes": tbl.import_notes,
                })
        return out

    def source_rows(self, table: str, key_columns: list[str], key_values: list,
                    limit: int = 20) -> dict:
        """按业务主键回查两引擎的源行，用于差异下钻。"""
        with self.lock:
            if table not in self.tables:
                raise ValueError(f"表 {table} 不存在")
            cols = self.tables[table].columns
            for k in key_columns:
                if not any(c.name == k for c in cols):
                    raise ValueError(f"主键列 {k} 不在表 {table} 中")
            # DuckDB 不支持 `col IS ?` 参数化，两边都支持 IS NOT DISTINCT FROM（含 NULL 语义）
            where = " AND ".join(f"{ea.quote_ident(k)} IS NOT DISTINCT FROM ?" for k in key_columns)
            sql = f"SELECT * FROM {ea.quote_ident(table)} WHERE {where} LIMIT {int(limit)}"
            params_s = [ea.to_sqlite_value(v, self._ltype(table, k))
                        for v, k in zip(key_values, key_columns)]
            params_d = [ea.to_duckdb_value(v, self._ltype(table, k))
                        for v, k in zip(key_values, key_columns)]
            res = {}
            for engine, con, params in (("sqlite", self.sqlite, params_s),
                                        ("duckdb", self.duckdb, params_d)):
                cur = con.execute(sql, params)
                names = [d[0] for d in cur.description]
                rows = cur.fetchall()
                res[engine] = {
                    "columns": names,
                    "types": self._result_types(engine, cur, names, rows),
                    "rows": [self._normalize_row(r) for r in rows],
                }
            return res

    def _ltype(self, table: str, col: str) -> str:
        return next(c.logical_type for c in self.tables[table].columns if c.name == col)

    # ---------------- 查询执行 ----------------
    def execute_both(self, sql_sqlite: str, sql_duckdb: str,
                     timeout_sec: int) -> list[EngineResult]:
        sql_guard(sql_sqlite)
        sql_guard(sql_duckdb)
        timeout_sec = max(1, min(int(timeout_sec), MAX_TIMEOUT_SEC))
        results = []
        with self.lock:
            results.append(self._run_one("sqlite", self.sqlite, sql_sqlite, timeout_sec))
            results.append(self._run_one("duckdb", self.duckdb, sql_duckdb, timeout_sec))
        return results

    def _run_one(self, engine: str, con, sql: str, timeout_sec: int) -> EngineResult:
        result = EngineResult(engine=engine, ok=False)
        box: dict = {}

        def work():
            start = time.perf_counter()
            try:
                cur = con.execute(sql)
                if cur.description is None:
                    box["error"] = "该语句没有返回结果集，请使用 SELECT 查询"
                    box["error_kind"] = "no_resultset"
                    return
                names = [d[0] for d in cur.description]
                rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
                truncated = len(rows) > MAX_RESULT_ROWS
                rows = rows[:MAX_RESULT_ROWS]
                box["columns"] = names
                box["types"] = self._result_types(engine, cur, names, rows)
                box["rows"] = [self._normalize_row(r) for r in rows]
                box["truncated"] = truncated
                box["ok"] = True
            except Exception as exc:  # noqa: BLE001 - 引擎报错要作为真实结果保留
                box["error"] = f"{type(exc).__name__}: {exc}".strip()
                box["error_kind"] = type(exc).__name__
            finally:
                box["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)

        th = threading.Thread(target=work, daemon=True)
        t0 = time.time()
        th.start()
        th.join(timeout_sec)
        if th.is_alive():
            try:
                con.interrupt()
            except Exception:  # noqa: BLE001
                pass
            th.join(min(timeout_sec, 5))
        if th.is_alive():
            # interrupt 无效（极端情况）：重建连接并重放全部数据
            self._rebuild_engine(engine)
            result.error = f"查询超过 {timeout_sec}s 硬超时，引擎连接已强制重建"
            result.error_kind = "TimeoutHard"
            result.duration_ms = round((time.time() - t0) * 1000, 1)
            return result
        result.duration_ms = box.get("duration_ms", round((time.time() - t0) * 1000, 1))
        if "ok" in box:
            result.ok = True
            result.columns = box["columns"]
            result.column_types = box["types"]
            result.rows = box["rows"]
            result.truncated = box["truncated"]
        else:
            result.error = box.get("error", "未知执行错误")
            result.error_kind = box.get("error_kind", "EngineError")
            if time.time() - t0 >= timeout_sec and result.error_kind not in ("TimeoutHard",):
                # interrupt 生效的超时通常抛 OperationalError/InterruptException
                if "interrupt" in result.error.lower() or "interrupted" in result.error.lower():
                    result.error = f"查询超过 {timeout_sec}s 被中断（interrupt）。原始报错：{result.error}"
                    result.error_kind = "Timeout"
        return result

    def _result_types(self, engine: str, cur, names: list[str], rows: list) -> list[str]:
        if engine == "duckdb":
            return [str(d[1]) if d[1] is not None else "UNKNOWN" for d in cur.description]
        # SQLite 的 cursor.description 不带类型：对每列取首条非 NULL 值的 typeof
        types = ["NULL"] * len(names)
        for row in rows:
            for i, val in enumerate(row):
                if types[i] == "NULL" and val is not None:
                    types[i] = cur.connection.execute(
                        "SELECT typeof(?)", [val]).fetchone()[0]
        return types

    @staticmethod
    def _normalize_row(row: tuple) -> list:
        out = []
        for v in row:
            if isinstance(v, bytes):
                out.append({"__blob__": v.hex()})
            elif isinstance(v, _dt.datetime):
                out.append(v.isoformat(sep=" "))
            elif isinstance(v, _dt.date):
                out.append(v.isoformat())
            elif isinstance(v, float):
                if v == float("inf"):
                    out.append({"__special__": "Infinity"})
                elif v == float("-inf"):
                    out.append({"__special__": "-Infinity"})
                elif v != v:  # NaN
                    out.append({"__special__": "NaN"})
                else:
                    out.append(v)
            else:
                out.append(v)
        return out

    def _rebuild_engine(self, engine: str) -> None:
        if engine == "sqlite":
            try:
                self.sqlite.close()
            except Exception:  # noqa: BLE001
                pass
            self.sqlite = sqlite3.connect(":memory:", check_same_thread=False)
            con = self.sqlite
        else:
            try:
                self.duckdb.close()
            except Exception:  # noqa: BLE001
                pass
            self.duckdb = duckdb.connect(":memory:")
            con = self.duckdb
        for table, (cols, py_rows) in self._raw_data.items():
            ddl = ea.build_ddl(table, cols)
            self._create_and_insert(table, cols, py_rows, ddl)

    def close(self) -> None:
        with self.lock:
            for con in (self.sqlite, self.duckdb):
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass


class SessionManager:
    def __init__(self, ttl_sec: int = 3600):
        self._sessions: dict[str, Session] = {}
        self._guard = threading.Lock()
        self.ttl = ttl_sec

    def create(self) -> Session:
        sid = uuid.uuid4().hex
        sess = Session(sid)
        with self._guard:
            self._sessions[sid] = sess
        self._evict()
        return sess

    def get(self, sid: str) -> Session:
        if not sid or not re.match(r"^[0-9a-f]{32}$", sid):
            raise KeyError("非法会话 id")
        with self._guard:
            sess = self._sessions.get(sid)
        if sess is None:
            raise KeyError("会话不存在或已过期，请刷新页面新建会话")
        return sess

    def drop(self, sid: str) -> None:
        with self._guard:
            sess = self._sessions.pop(sid, None)
        if sess:
            sess.close()

    def _evict(self) -> None:
        now = time.time()
        dead = [sid for sid, s in self._sessions.items() if now - s.created_at > self.ttl]
        for sid in dead:
            sess = self._sessions.pop(sid, None)
            if sess:
                sess.close()
