"""SQLite / DuckDB 结果对照逻辑。

设计原则（对应需求）：
- 没有排序条件（或显式选择集合模式）时按“记录多重集合”比较，保留重复次数，
  不逐行硬比，避免行序不同造成假差异；
- 可指定业务主键，按主键配对记录；无主键时按整行规范化签名配对；
- 数值列支持绝对/相对误差，误差内视为一致；
- 布尔 0/1 与 true/false 视为等价但标注表示差异（representation_only）；
- 引擎报错不参与行比较，整体标记 error 并原样保留两边错误。
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from .session_manager import EngineResult


def _norm_scalar(v):
    """规范化为可哈希签名；特殊浮点和 blob 用元组标记。"""
    if isinstance(v, dict):
        if "__special__" in v:
            return ("__special__", v["__special__"])
        if "__blob__" in v:
            return ("__blob__", v["__blob__"].lower())
    if isinstance(v, float):
        if v == int(v) and math.isfinite(v):
            return ("num", float(v))
        return ("num", v)
    if isinstance(v, bool):  # 纯 Python bool（JSON 回来的 true/false）
        return ("bool", v)
    if isinstance(v, int):
        return ("num", float(v))
    if v is None:
        return ("__null__",)
    return ("val", v)


def _is_number_token(tok) -> bool:
    return isinstance(tok, tuple) and tok and tok[0] == "num"


def _num_value(tok) -> float:
    return tok[1]


def _values_equivalent(a, b, abs_tol: float, rel_tol: float) -> tuple[bool, bool]:
    """返回 (是否一致, 是否仅表示形式不同如 1 vs true / 3 vs 3.0)。"""
    ta, tb = _norm_scalar(a), _norm_scalar(b)
    # 布尔/整数表示差异：True == 1
    bool_like_a = isinstance(a, bool) or (
        isinstance(a, int) and not isinstance(a, bool) and a in (0, 1))
    bool_like_b = isinstance(b, bool) or (
        isinstance(b, int) and not isinstance(b, bool) and b in (0, 1))
    if _is_number_token(ta) and _is_number_token(tb):
        x, y = _num_value(ta), _num_value(tb)
        if not math.isfinite(x) or not math.isfinite(y):
            if math.isnan(x) and math.isnan(y):
                return True, False
            return (x == y), False
        if x == y:
            # 3 (int) vs 3.0 (float) 或 1 vs true：值一致，仅表示形式不同
            rep_only = (type(a) != type(b)) or bool_like_a != bool_like_b or isinstance(a, bool) != isinstance(b, bool)
            return True, rep_only
        diff = abs(x - y)
        allowed = abs_tol + rel_tol * max(abs(x), abs(y))
        if diff <= allowed:
            return True, False
        return False, False
    if ta == tb:
        rep_only = (type(a) != type(b))
        return True, rep_only
    # 0/1 与 true/false 跨类型等价
    if bool_like_a and bool_like_b and _truthy(a) == _truthy(b):
        return True, True
    return False, False


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return bool(v)


def _row_signature(row: list) -> tuple:
    return tuple(_norm_scalar(v) for v in row)


@dataclass
class CellDiff:
    column: str
    sqlite_value: object
    duckdb_value: object
    sqlite_type: str
    duckdb_type: str
    detail: str
    representation_only: bool = False


@dataclass
class RowDiff:
    kind: str                      # value_diff | only_in_sqlite | only_in_duckdb | duplicate_count
    key_values: dict = field(default_factory=dict)
    sqlite_row: list | None = None
    duckdb_row: list | None = None
    cell_diffs: list[CellDiff] = field(default_factory=list)
    multiplicity: tuple = (0, 0)   # (sqlite 出现次数, duckdb 出现次数)


@dataclass
class ComparisonReport:
    status: str                    # identical | different | error | structure_diff
    mode: str                      # set | ordered
    summary: str
    row_counts: tuple
    column_match: bool
    columns_sqlite: list
    columns_duckdb: list
    row_diffs: list[RowDiff] = field(default_factory=list)
    rep_only_count: int = 0
    tolerance: dict = field(default_factory=dict)


def compare(sqlite_res: EngineResult, duckdb_res: EngineResult,
            key_columns: list[str] | None = None,
            abs_tol: float = 0.0, rel_tol: float = 0.0,
            ordered: bool = False) -> ComparisonReport:
    key_columns = key_columns or []
    tol = {"abs": abs_tol, "rel": rel_tol}

    if not sqlite_res.ok or not duckdb_res.ok:
        problems = []
        if not sqlite_res.ok:
            problems.append(f"SQLite 报错: {sqlite_res.error}")
        if not duckdb_res.ok:
            problems.append(f"DuckDB 报错: {duckdb_res.error}")
        return ComparisonReport(
            status="error", mode="ordered" if ordered else "set",
            summary="；".join(problems),
            row_counts=(len(sqlite_res.rows), len(duckdb_res.rows)),
            column_match=False,
            columns_sqlite=sqlite_res.columns, columns_duckdb=duckdb_res.columns,
            tolerance=tol,
        )

    cols_s, cols_d = sqlite_res.columns, duckdb_res.columns
    column_match = cols_s == cols_d
    if not column_match:
        return ComparisonReport(
            status="structure_diff", mode="ordered" if ordered else "set",
            summary=(f"结果列结构不一致：SQLite {len(cols_s)} 列 {cols_s}；"
                     f"DuckDB {len(cols_d)} 列 {cols_d}"),
            row_counts=(len(sqlite_res.rows), len(duckdb_res.rows)),
            column_match=False, columns_sqlite=cols_s, columns_duckdb=cols_d,
            tolerance=tol,
        )

    if ordered:
        return _compare_ordered(sqlite_res, duckdb_res, key_columns, abs_tol, rel_tol, tol)
    return _compare_multiset(sqlite_res, duckdb_res, key_columns, abs_tol, rel_tol, tol)


def _key_map(columns: list[str], key_columns: list[str]) -> list[int]:
    idx = []
    for k in key_columns:
        if k not in columns:
            raise ValueError(f"主键列 {k} 不在结果列中，结果列: {columns}")
        idx.append(columns.index(k))
    return idx


def _extract_key(row: list, key_idx: list[int], columns: list[str], key_columns: list[str]) -> dict:
    return {key_columns[i]: row[key_idx[i]] for i in range(len(key_idx))}


def _compare_multiset(sres: EngineResult, dres: EngineResult, key_columns: list[str],
                      abs_tol: float, rel_tol: float, tol: dict) -> ComparisonReport:
    diffs: list[RowDiff] = []
    rep_only_total = 0
    key_idx = _key_map(sres.columns, key_columns) if key_columns else []

    if key_idx:
        # 按主键分组，保留每个主键下两边的多重集合（重复主键也要保留次数）
        s_groups: dict[tuple, list[list]] = {}
        d_groups: dict[tuple, list[list]] = {}
        for row in sres.rows:
            s_groups.setdefault(tuple(_norm_scalar(row[i]) for i in key_idx), []).append(row)
        for row in dres.rows:
            d_groups.setdefault(tuple(_norm_scalar(row[i]) for i in key_idx), []).append(row)
        all_keys = sorted(set(s_groups) | set(d_groups), key=lambda t: repr(t))
        for sig in all_keys:
            s_rows = s_groups.get(sig, [])
            d_rows = d_groups.get(sig, [])
            kv = _extract_key((s_rows or d_rows)[0], key_idx, sres.columns, key_columns)
            n = max(len(s_rows), len(d_rows))
            for i in range(n):
                sr = s_rows[i] if i < len(s_rows) else None
                dr = d_rows[i] if i < len(d_rows) else None
                if sr is None or dr is None:
                    diffs.append(RowDiff(
                        kind="only_in_duckdb" if sr is None else "only_in_sqlite",
                        key_values=kv, sqlite_row=sr, duckdb_row=dr,
                        multiplicity=(len(s_rows), len(d_rows))))
                else:
                    cd, rep = _cell_diffs(sr, dr, sres, dres, abs_tol, rel_tol)
                    rep_only_total += rep
                    if cd:
                        diffs.append(RowDiff(kind="value_diff", key_values=kv,
                                             sqlite_row=sr, duckdb_row=dr, cell_diffs=cd,
                                             multiplicity=(1, 1)))
    else:
        # 无主键：按整行签名做多重集合配对，Counter 保留重复次数
        s_counter = Counter(_row_signature(r) for r in sres.rows)
        d_counter = Counter(_row_signature(r) for r in dres.rows)
        s_pool = list(sres.rows)
        d_pool = list(dres.rows)

        def pop_row(pool, sig):
            for i, r in enumerate(pool):
                if _row_signature(r) == sig:
                    return pool.pop(i)
            return None

        # 第一轮：精确签名匹配（含重复次数）
        for sig in set(s_counter) & set(d_counter):
            m = min(s_counter[sig], d_counter[sig])
            for _ in range(m):
                pop_row(s_pool, sig)
                pop_row(d_pool, sig)
        # 第二轮：剩余行按最小差异贪心配对——容差/布尔等价则静默消费，
        # 否则记为 value_diff；最后仍有剩余的行作为单侧存在差异。
        rep_only_total += _greedy_value_diffs(
            s_pool, d_pool, sres, dres, abs_tol, rel_tol, diffs)

    # 状态汇总（有主键时重复数不等已在 only_* 行的 multiplicity 中体现）
    status = "identical" if not diffs else "different"
    if diffs and all(d.kind == "value_diff" and
                     all(c.representation_only for c in d.cell_diffs) for d in diffs):
        status = "identical"  # 仅 1 vs true 表示差异，值层面判定一致（摘要里提示）
    summary = _build_summary(status, sres, dres, diffs, rep_only_total, key_columns)
    return ComparisonReport(
        status=status, mode="set", summary=summary,
        row_counts=(len(sres.rows), len(dres.rows)), column_match=True,
        columns_sqlite=sres.columns, columns_duckdb=dres.columns,
        row_diffs=diffs[:100], rep_only_count=rep_only_total, tolerance=tol,
    )


def _greedy_value_diffs(s_remain, d_remain, sres, dres, abs_tol, rel_tol, diffs) -> int:
    """两侧剩余行按列距离贪心配对：等价则静默消费（仅返回表示差异计数），
    否则输出逐列差异；配不上对的剩余行作为单侧存在差异。"""
    rep_count = 0
    while s_remain and d_remain:
        best = None
        for si, sr in enumerate(s_remain):
            for di, dr in enumerate(d_remain):
                cd, rep = _cell_diffs(sr, dr, sres, dres, abs_tol, rel_tol)
                score = len([c for c in cd if not c.representation_only])
                if best is None or score < best[0]:
                    best = (score, si, di, cd)
        _, si, di, cd = best
        sr, dr = s_remain.pop(si), d_remain.pop(di)
        if cd:
            diffs.append(RowDiff(kind="value_diff", sqlite_row=sr, duckdb_row=dr,
                                 cell_diffs=cd))
        else:
            # 完全在容差内等价（可能含 1 vs true 之类表示差异）
            rep_count += 1
    for sr in s_remain:
        diffs.append(RowDiff(kind="only_in_sqlite", sqlite_row=sr, duckdb_row=None))
    for dr in d_remain:
        diffs.append(RowDiff(kind="only_in_duckdb", sqlite_row=None, duckdb_row=dr))
    return rep_count


def _compare_ordered(sres: EngineResult, dres: EngineResult, key_columns,
                     abs_tol, rel_tol, tol) -> ComparisonReport:
    diffs: list[RowDiff] = []
    rep_only_total = 0
    key_idx = _key_map(sres.columns, key_columns) if key_columns else []
    for i in range(max(len(sres.rows), len(dres.rows))):
        sr = sres.rows[i] if i < len(sres.rows) else None
        dr = dres.rows[i] if i < len(dres.rows) else None
        kv = {}
        if key_idx and sr is not None:
            kv = _extract_key(sr, key_idx, sres.columns, key_columns)
        if sr is None or dr is None:
            diffs.append(RowDiff(
                kind="only_in_duckdb" if sr is None else "only_in_sqlite",
                key_values=kv, sqlite_row=sr, duckdb_row=dr))
            continue
        cd, rep = _cell_diffs(sr, dr, sres, dres, abs_tol, rel_tol)
        rep_only_total += rep
        if cd:
            diffs.append(RowDiff(kind="value_diff", key_values=kv,
                                 sqlite_row=sr, duckdb_row=dr, cell_diffs=cd))
    status = "identical" if not diffs else "different"
    if diffs and all(d.kind == "value_diff" and
                     all(c.representation_only for c in d.cell_diffs) for d in diffs):
        status = "identical"
    summary = _build_summary(status, sres, dres, diffs, rep_only_total, key_columns, ordered=True)
    return ComparisonReport(
        status=status, mode="ordered", summary=summary,
        row_counts=(len(sres.rows), len(dres.rows)), column_match=True,
        columns_sqlite=sres.columns, columns_duckdb=dres.columns,
        row_diffs=diffs[:100], rep_only_count=rep_only_total, tolerance=tol,
    )


def _cell_diffs(sr: list, dr: list, sres: EngineResult, dres: EngineResult,
                abs_tol: float, rel_tol: float) -> tuple[list[CellDiff], int]:
    out = []
    rep_rows = 0
    for i, col in enumerate(sres.columns):
        a, b = sr[i], dr[i]
        equal, rep_only = _values_equivalent(a, b, abs_tol, rel_tol)
        if rep_only:
            rep_rows += 1
        if not equal or rep_only:
            st = sres.column_types[i] if i < len(sres.column_types) else "?"
            dt = dres.column_types[i] if i < len(dres.column_types) else "?"
            if equal and rep_only:
                detail = f"值相等但表示/类型不同：{a!r} ({st}) vs {b!r} ({dt})"
            elif _is_number_token(_norm_scalar(a)) and _is_number_token(_norm_scalar(b)):
                detail = (f"数值差 {abs(float(a) - float(b)):g} 超过容差"
                          f"(abs={abs_tol:g}, rel={rel_tol:g})")
            else:
                detail = f"值不同：{a!r} ({st}) vs {b!r} ({dt})"
            out.append(CellDiff(col, a, b, st, dt, detail, rep_only))
    return out, (1 if rep_rows and not any(not c.representation_only for c in out) else 0)


def _build_summary(status, sres: EngineResult, dres: EngineResult,
                   diffs: list[RowDiff], rep_only: int, key_columns, ordered=False) -> str:
    ns, nd = len(sres.rows), len(dres.rows)
    mode = "逐行有序" if ordered else ("按主键 " + ",".join(key_columns) if key_columns else "无序多重集合")
    if status == "identical":
        base = f"两边结果一致（{mode} 比较，各 {ns} 行）"
        if rep_only:
            base += f"；另有 {rep_only} 处仅表示形式差异（如 1 vs true、3 vs 3.0），值等价"
        if sres.truncated or dres.truncated:
            base += "；注意结果已截断到 500 行，仅比较了截断后的部分"
        return base
    only_s = sum(1 for d in diffs if d.kind == "only_in_sqlite")
    only_d = sum(1 for d in diffs if d.kind == "only_in_duckdb")
    val = sum(1 for d in diffs if d.kind == "value_diff")
    parts = [f"发现 {len(diffs)} 处差异（{mode} 比较；SQLite {ns} 行 / DuckDB {nd} 行）"]
    if val:
        parts.append(f"{val} 处同行值不同")
    if only_s:
        parts.append(f"{only_s} 行仅 SQLite 有")
    if only_d:
        parts.append(f"{only_d} 行仅 DuckDB 有")
    return "；".join(parts)
