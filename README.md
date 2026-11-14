# SQLite × DuckDB 迁移对照台

同一份数据在 SQLite 与 DuckDB 两个引擎中**实际建表、实际执行**，并排展示 SQL、字段类型与结果，
用于定位数据迁移后“数字对不上”的原因。不是用预设结果替代——两边都跑真实查询，报错也作为真实结果保留。

## 启动

```bash
python3 -m venv .venv
.venv/bin/pip install duckdb flask
./run.sh          # 默认 http://127.0.0.1:8137
```

## 功能对照需求清单

| 需求 | 实现位置 |
| --- | --- |
| 上传小型数据集，两引擎实际建表执行 | `app/session_manager.py` CSV 推断 + 双引擎 DDL/INSERT |
| 并排显示 SQL、字段类型、结果 | 双栏编辑器 + 结果表（含每列声明/存储类型） |
| 建表类型与导入处理可查看 | Schema 区“查看两引擎建表 DDL”，含导入说明 |
| 查询报错作为真实结果保留 | 引擎异常原样展示，比较结论标记 error，不吞错 |
| 空值比较 / 整数除法 / 字符串数值转换 / 聚合空集 / 时间字段 | 内置 8 个案例（`app/examples_data.py`），差异均经 `scripts/probe*.py` 实测 |
| 业务主键 + 允许数值误差（绝对/相对） | 比较配置区 + `app/result_comparator.py` |
| 无排序时按记录集合比较、保留重复次数 | 多重集合 Counter 比较，不逐行硬比；可切“逐行有序” |
| 展开差异查看源行、表达式、两边类型 | 差异下钻抽屉（按主键回查两引擎源行 + 双方 SQL + 逐列类型） |
| 修改各自 SQL 后重新对照 | 左右独立编辑器，分别按方言改写 |
| 真实差异示例 + 改写后一致版本 | 每个案例都有 divergence / fixed 两版 |
| 隔离数据空间 | 每会话独立 `:memory:` 连接 + UUID 隔离 + 锁串行 |
| 执行超时 | 工作线程执行，超时 `interrupt()`，兜底强制重建连接（1–15 秒可配） |
| 不支持的 SQL 明确说明、不伪装通用标准 | SQL Guard 拦截 + 方言速查表（12 条实测差异） |

## 安全边界

- 仅允许单条 `SELECT` / `WITH ... SELECT`；写操作、`PRAGMA`、`ATTACH`、`read_csv` 等文件函数、路径字面量一律拒绝（`sql_guard`，词法级，先剥离字符串与注释再扫描）。
- 引擎全部使用内存库，工作目录不向引擎开放；上传限 2MB / 2000 行，结果保留前 500 行。
- 会话默认 1 小时过期，服务仅监听 127.0.0.1。

## 目录

```
app/                  后端（Flask + sqlite3 + duckdb）
  engine_abstraction.py   类型映射、CSV 推断与导入转换
  session_manager.py      隔离会话、SQL Guard、超时执行、类型自省
  result_comparator.py    多重集合/主键/容差对照
  examples_data.py        示例数据与 8 组对照案例
  app.py                  路由
static/               前端（原生 HTML/CSS/JS，无构建步骤）
scripts/probe*.py     引擎行为探针（示例差异的实测依据）
```
