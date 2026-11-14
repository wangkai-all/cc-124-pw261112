"""逻辑类型 -> SQLite / DuckDB 建表类型映射，以及 CSV 类型推断与导入转换。

所有规则都来自 scripts/probe*.py 的实测结果，不假设两个引擎行为一致。
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

LOGICAL_TYPES = ("TEXT", "INTEGER", "REAL", "BOOLEAN", "DATE", "TIMESTAMP")

# 逻辑类型 -> 各引擎建表 SQL 里的声明类型
SQLITE_DECL = {
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
    "REAL": "REAL",
    "BOOLEAN": "BOOLEAN",  # SQLite 无原生布尔，BOOLEAN 只是亲和性名，实际存 0/1
    "DATE": "TEXT",       # SQLite 无原生日期，按 ISO-8601 文本存储
    "TIMESTAMP": "TEXT",
}
DUCKDB_DECL = {
    "TEXT": "VARCHAR",
    "INTEGER": "BIGINT",
    "REAL": "DOUBLE",
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
}

_INT_RE = re.compile(r"^[+-]?\d+$")
_REAL_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?$")
_BOOL_SET = {"true", "false"}


def _is_int(s: str) -> bool:
    return bool(_INT_RE.match(s.strip()))


def _is_real(s: str) -> bool:
    # 注意：整数字符串也匹配 REAL_RE。纯整数列会先被 INTEGER 检查命中；
    # 整数与小数混合的列走到这里时，应整体推断为 REAL。
    return bool(_REAL_RE.match(s.strip()))


def _is_bool(s: str) -> bool:
    return s.strip().lower() in _BOOL_SET


def _is_date(s: str) -> bool:
    if not _DATE_RE.match(s.strip()):
        return False
    try:
        _dt.date.fromisoformat(s.strip())
        return True
    except ValueError:
        return False


def _is_timestamp(s: str) -> bool:
    v = s.strip().replace(" ", "T")
    if not (_TS_RE.match(s.strip()) or _DATE_RE.match(s.strip())):
        return False
    try:
        _dt.datetime.fromisoformat(v)
        return True
    except ValueError:
        return False


_CHECK_ORDER = [
    ("INTEGER", _is_int),
    ("BOOLEAN", _is_bool),
    ("DATE", _is_date),
    ("TIMESTAMP", _is_timestamp),
    ("REAL", _is_real),
]


@dataclass
class ColumnSpec:
    name: str
    logical_type: str

    @property
    def sqlite_decl(self) -> str:
        return SQLITE_DECL[self.logical_type]

    @property
    def duckdb_decl(self) -> str:
        return DUCKDB_DECL[self.logical_type]


def infer_column_type(values: list[str]) -> str:
    """根据非空样本推断列的逻辑类型。混入无法解析的值则退化为 TEXT。"""
    seen = [v for v in values if v is not None and v.strip() != ""]
    if not seen:
        return "TEXT"
    for ltype, check in _CHECK_ORDER:
        if all(check(v) for v in seen):
            return ltype
    return "TEXT"


def parse_value(raw: str, logical_type: str):
    """把 CSV 文本单元格转换成 Python 值（未命中的类型保持 None/字符串）。"""
    if raw is None or raw == "":
        return None
    v = raw.strip() if logical_type != "TEXT" else raw
    if logical_type == "INTEGER":
        return int(v)
    if logical_type == "REAL":
        return float(v)
    if logical_type == "BOOLEAN":
        return 1 if v.lower() == "true" else 0
    if logical_type == "DATE":
        return _dt.date.fromisoformat(v)
    if logical_type == "TIMESTAMP":
        return _dt.datetime.fromisoformat(v.replace(" ", "T"))
    return raw


def to_sqlite_value(py_value, logical_type: str):
    """SQLite 侧：布尔转 0/1，日期/时间转 ISO 文本，其余原样（驱动自行适配）。"""
    if py_value is None:
        return None
    if logical_type == "BOOLEAN":
        return 1 if py_value else 0
    if isinstance(py_value, _dt.datetime):
        return py_value.isoformat(sep=" ")
    if isinstance(py_value, _dt.date):
        return py_value.isoformat()
    return py_value


def to_duckdb_value(py_value, logical_type: str):
    """DuckDB 侧：布尔转 bool，日期/时间保持 datetime 对象，驱动会强转进原生列。"""
    if py_value is None:
        return None
    if logical_type == "BOOLEAN":
        return bool(py_value)
    return py_value


def build_ddl(table: str, columns: list[ColumnSpec]) -> dict[str, str]:
    cols_sqlite = ", ".join(f'"{c.name}" {c.sqlite_decl}' for c in columns)
    cols_duck = ", ".join(f'"{c.name}" {c.duckdb_decl}' for c in columns)
    return {
        "sqlite": f'CREATE TABLE "{table}" ({cols_sqlite})',
        "duckdb": f'CREATE TABLE "{table}" ({cols_duck})',
    }


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
