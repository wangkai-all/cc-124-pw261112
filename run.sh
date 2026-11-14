#!/usr/bin/env bash
# 启动 SQLite × DuckDB 迁移对照台（仅监听本机回环地址）
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m app.app
