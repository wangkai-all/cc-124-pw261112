"""Flask 入口：页面路由 + 会话/导入/查询/对照/下钻 API。"""
from __future__ import annotations

import os
import traceback

from flask import Flask, jsonify, request, send_from_directory

from .examples_data import DIALECT_NOTES, EXAMPLES, SAMPLE_CSV, SAMPLE_TABLE
from .result_comparator import compare
from .session_manager import (
    DEFAULT_TIMEOUT_SEC,
    MAX_TIMEOUT_SEC,
    SessionManager,
    SqlGuardError,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    manager = SessionManager()
    app.config["MANAGER"] = manager

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/api/meta")
    def meta():
        return jsonify({
            "examples": EXAMPLES,
            "dialect_notes": DIALECT_NOTES,
            "defaults": {"timeout_sec": DEFAULT_TIMEOUT_SEC, "max_timeout_sec": MAX_TIMEOUT_SEC},
        })

    @app.post("/api/session")
    def create_session():
        sess = manager.create()
        return jsonify({"session_id": sess.sid})

    @app.post("/api/session/<sid>/sample")
    def load_sample(sid):
        try:
            sess = manager.get(sid)
            loaded = sess.load_csv("orders.csv", SAMPLE_CSV.encode("utf-8"),
                                   table_name=SAMPLE_TABLE)
            return jsonify({"loaded": _table_payload(loaded), "schema": sess.schema_info()})
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        except Exception as e:  # noqa: BLE001
            return _error_response(e)

    @app.post("/api/session/<sid>/upload")
    def upload(sid):
        try:
            sess = manager.get(sid)
            f = request.files.get("file")
            if f is None:
                return jsonify({"error": "未收到上传文件"}), 400
            content = f.read()
            overrides = {}
            raw_override = request.form.get("type_overrides")
            if raw_override:
                import json
                overrides = json.loads(raw_override)
            loaded = sess.load_csv(f.filename, content,
                                   table_name=request.form.get("table_name") or None,
                                   type_overrides=overrides)
            return jsonify({"loaded": _table_payload(loaded), "schema": sess.schema_info()})
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        except Exception as e:  # noqa: BLE001
            return _error_response(e)

    @app.get("/api/session/<sid>/schema")
    def schema(sid):
        try:
            sess = manager.get(sid)
            return jsonify({"schema": sess.schema_info()})
        except KeyError as e:
            return jsonify({"error": str(e)}), 404

    @app.post("/api/session/<sid>/query")
    def query(sid):
        body = request.get_json(force=True, silent=True) or {}
        try:
            sess = manager.get(sid)
            sql_sqlite = body.get("sqlite_sql", "")
            sql_duckdb = body.get("duckdb_sql", "")
            timeout_sec = int(body.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
            key_columns = body.get("key_columns") or []
            abs_tol = float(body.get("abs_tol") or 0)
            rel_tol = float(body.get("rel_tol") or 0)
            ordered = bool(body.get("ordered", False))

            results = sess.execute_both(sql_sqlite, sql_duckdb, timeout_sec)
            report = None
            compare_error = None
            try:
                report = compare(results[0], results[1], key_columns,
                                 abs_tol=abs_tol, rel_tol=rel_tol, ordered=ordered)
            except ValueError as e:
                compare_error = str(e)
            return jsonify({
                "results": [_result_payload(r) for r in results],
                "comparison": _report_payload(report) if report else None,
                "compare_error": compare_error,
            })
        except SqlGuardError as e:
            return jsonify({"error": f"SQL 安全检查未通过: {e}"}), 400
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        except Exception as e:  # noqa: BLE001
            return _error_response(e)

    @app.post("/api/session/<sid>/source-row")
    def source_row(sid):
        body = request.get_json(force=True, silent=True) or {}
        try:
            sess = manager.get(sid)
            data = sess.source_rows(
                body.get("table", ""),
                body.get("key_columns") or [],
                body.get("key_values") or [],
            )
            return jsonify({"source": data})
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        except Exception as e:  # noqa: BLE001
            return _error_response(e)

    @app.delete("/api/session/<sid>")
    def drop_session(sid):
        manager.drop(sid)
        return jsonify({"ok": True})

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "接口不存在"}), 404

    return app


def _table_payload(loaded) -> dict:
    return {
        "table": loaded.name,
        "row_count": loaded.row_count,
        "ddl": loaded.ddl,
        "columns": [
            {"name": c.name, "logical_type": c.logical_type,
             "sqlite_declared": c.sqlite_decl, "duckdb_declared": c.duckdb_decl}
            for c in loaded.columns
        ],
        "import_notes": loaded.import_notes,
    }


def _result_payload(r) -> dict:
    return {
        "engine": r.engine,
        "ok": r.ok,
        "columns": r.columns,
        "column_types": r.column_types,
        "rows": r.rows,
        "truncated": r.truncated,
        "error": r.error,
        "error_kind": r.error_kind,
        "duration_ms": r.duration_ms,
    }


def _report_payload(rep) -> dict:
    return {
        "status": rep.status,
        "mode": rep.mode,
        "summary": rep.summary,
        "row_counts": rep.row_counts,
        "column_match": rep.column_match,
        "columns_sqlite": rep.columns_sqlite,
        "columns_duckdb": rep.columns_duckdb,
        "rep_only_count": rep.rep_only_count,
        "tolerance": rep.tolerance,
        "row_diffs": [
            {
                "kind": d.kind,
                "key_values": d.key_values,
                "sqlite_row": d.sqlite_row,
                "duckdb_row": d.duckdb_row,
                "multiplicity": d.multiplicity,
                "cell_diffs": [
                    {
                        "column": c.column,
                        "sqlite_value": c.sqlite_value,
                        "duckdb_value": c.duckdb_value,
                        "sqlite_type": c.sqlite_type,
                        "duckdb_type": c.duckdb_type,
                        "detail": c.detail,
                        "representation_only": c.representation_only,
                    }
                    for c in d.cell_diffs
                ],
            }
            for d in rep.row_diffs
        ],
    }


def _error_response(e: Exception):
    traceback.print_exc()
    return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8137, debug=False)
