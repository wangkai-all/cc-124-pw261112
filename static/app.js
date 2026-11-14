/* SQLite × DuckDB 对照台前端逻辑（无框架、无构建步骤） */
"use strict";

const state = {
  sessionId: null,
  schema: [],
  meta: null,
  activeExample: null,
  ordered: false,
  lastSql: { sqlite: "", duckdb: "" },
  lastResults: null,
  lastComparison: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtCell(v) {
  if (v === null || v === undefined) return { text: "NULL", cls: "null" };
  if (typeof v === "object") {
    if (v.__special__ !== undefined) return { text: v.__special__, cls: "special" };
    if (v.__blob__ !== undefined) return { text: `x'${v.__blob__.slice(0, 32)}…'`, cls: "special" };
  }
  if (typeof v === "boolean") return { text: v ? "true" : "false", cls: "boolv" };
  return { text: esc(v), cls: "" };
}

// ---------------- 初始化 ----------------
async function init() {
  bindEvents();
  try {
    state.meta = await api("/api/meta");
    renderDialectTable();
    $("duck-version").textContent = "2.0";
    $("cfg-timeout").value = state.meta.defaults.timeout_sec;
  } catch (e) {
    setRunStatus("元数据加载失败: " + e.message, true);
  }
  await newSession();
}

async function newSession() {
  try {
    const data = await api("/api/session", { method: "POST" });
    state.sessionId = data.session_id;
    state.schema = [];
    $("session-state").textContent = "会话 " + data.session_id.slice(0, 8) + "（隔离内存）";
    $("session-state").className = "badge badge-live";
    $("btn-load-sample").disabled = false;
    $("btn-run").disabled = false;
    $("schema-area").innerHTML = '<p class="hint">会话已创建。请载入内置示例或上传 CSV。</p>';
    $("import-notes").innerHTML = "";
  } catch (e) {
    $("session-state").textContent = "会话创建失败";
  }
}

function bindEvents() {
  $("btn-new-session").addEventListener("click", newSession);
  $("btn-load-sample").addEventListener("click", loadSample);
  $("file-upload").addEventListener("change", uploadCsv);
  $("btn-run").addEventListener("click", runQuery);
  $("btn-copy-left").addEventListener("click", () => {
    $("sql-duckdb").value = $("sql-sqlite").value;
  });
  $("mode-set").addEventListener("click", () => setMode(false));
  $("mode-ordered").addEventListener("click", () => setMode(true));
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-mask").addEventListener("click", closeDrawer);
}

function setMode(ordered) {
  state.ordered = ordered;
  $("mode-set").classList.toggle("active", !ordered);
  $("mode-ordered").classList.toggle("active", ordered);
}

// ---------------- 数据导入 ----------------
async function loadSample() {
  try {
    setRunStatus("正在两引擎建表并导入示例…");
    const data = await api(`/api/session/${state.sessionId}/sample`, { method: "POST" });
    state.schema = data.schema;
    renderSchema();
    renderImportNotes(data.loaded.import_notes);
    setRunStatus(`示例已导入：表 ${data.loaded.table}，${data.loaded.row_count} 行`);
    renderExamples();
  } catch (e) {
    setRunStatus("导入失败: " + e.message, true);
  }
}

async function uploadCsv(ev) {
  const file = ev.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    setRunStatus(`正在上传并推断 ${file.name} 的类型…`);
    const data = await api(`/api/session/${state.sessionId}/upload`, { method: "POST", body: fd });
    state.schema = data.schema;
    renderSchema();
    renderImportNotes(data.loaded.import_notes);
    setRunStatus(`已导入：表 ${data.loaded.table}，${data.loaded.row_count} 行`);
  } catch (e) {
    setRunStatus("上传失败: " + e.message, true);
  } finally {
    ev.target.value = "";
  }
}

function renderImportNotes(notes) {
  $("import-notes").innerHTML = notes.map((n) => `<div>ℹ ${esc(n)}</div>`).join("");
}

function renderSchema() {
  $("schema-area").innerHTML = state.schema.map((t) => {
    const cols = t.columns.map((c) => `
      <tr>
        <td><code>${esc(c.name)}</code></td>
        <td><span class="pill">${esc(c.logical_type)}</span></td>
        <td><code>${esc(c.sqlite_declared)}</code></td>
        <td><code>${esc(c.duckdb_declared)}</code></td>
      </tr>`).join("");
    return `
    <div class="table-block">
      <div class="table-block-head">
        表 <code>${esc(t.table)}</code>
        <span class="pill">${t.row_count} 行 × ${t.columns.length} 列</span>
        <button class="ddl-toggle" data-ddl="${esc(t.table)}">查看两引擎建表 DDL ▾</button>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>列名</th><th>推断逻辑类型</th><th>SQLite 声明类型</th><th>DuckDB 声明类型</th></tr></thead>
          <tbody>${cols}</tbody>
        </table>
      </div>
      <div class="ddl-box hidden" id="ddl-${esc(t.table)}">
<span class="eng-tag">-- SQLite（注意：日期是 TEXT、布尔是 INTEGER 亲和）</span>
${esc(t.ddl.sqlite)};

<span class="eng-tag">-- DuckDB（原生 DATE / BOOLEAN / BIGINT / DOUBLE）</span>
${esc(t.ddl.duckdb)};
      </div>
    </div>`;
  }).join("");
  document.querySelectorAll(".ddl-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const box = $("ddl-" + btn.dataset.ddl);
      box.classList.toggle("hidden");
      btn.textContent = box.classList.contains("hidden") ? "查看两引擎建表 DDL ▾" : "收起 DDL ▴";
    });
  });
}

// ---------------- 示例 ----------------
function renderExamples() {
  const bar = $("examples-bar");
  bar.innerHTML = state.meta.examples.map((ex) =>
    `<button class="ex-chip" data-ex="${esc(ex.id)}">${esc(ex.title)}</button>`).join("");
  bar.querySelectorAll(".ex-chip").forEach((chip) => {
    chip.addEventListener("click", () => selectExample(chip.dataset.ex));
  });
}

function selectExample(id) {
  const ex = state.meta.examples.find((e) => e.id === id);
  state.activeExample = ex;
  document.querySelectorAll(".ex-chip").forEach((c) =>
    c.classList.toggle("active", c.dataset.ex === id));
  const desc = $("example-desc");
  desc.classList.remove("hidden");
  desc.innerHTML = `
    <div><strong>${esc(ex.focus)}</strong> — ${esc(ex.description)}</div>
    ${ex.dialect_note ? `<div class="dialect-note">⚠ 方言说明：${esc(ex.dialect_note)}</div>` : ""}
    <div class="ex-actions">
      <button class="btn btn-sm btn-primary" data-ver="divergence">载入会产生真实差异的版本</button>
      <button class="btn btn-sm" data-ver="fixed">载入改写后一致的版本</button>
    </div>`;
  desc.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => applyExampleVersion(ex, b.dataset.ver));
  });
  applyExampleVersion(ex, "divergence");
}

function applyExampleVersion(ex, version) {
  const v = ex[version];
  $("sql-sqlite").value = v.sqlite_sql;
  $("sql-duckdb").value = v.duckdb_sql;
  $("cfg-keys").value = (ex.keys || []).join(", ");
  $("cfg-abs-tol").value = ex.abs_tol ?? 0;
  $("cfg-rel-tol").value = ex.rel_tol ?? 0;
  setMode(!!ex.ordered);
}

// ---------------- 执行与对照 ----------------
function readConfig() {
  return {
    key_columns: $("cfg-keys").value.split(",").map((s) => s.trim()).filter(Boolean),
    abs_tol: parseFloat($("cfg-abs-tol").value) || 0,
    rel_tol: parseFloat($("cfg-rel-tol").value) || 0,
    timeout_sec: Math.min(15, Math.max(1, parseInt($("cfg-timeout").value, 10) || 5)),
    ordered: state.ordered,
  };
}

async function runQuery() {
  const cfg = readConfig();
  const body = {
    sqlite_sql: $("sql-sqlite").value,
    duckdb_sql: $("sql-duckdb").value,
    ...cfg,
  };
  state.lastSql = { sqlite: body.sqlite_sql, duckdb: body.duckdb_sql };
  setRunStatus("两个引擎执行中…");
  try {
    const data = await api(`/api/session/${state.sessionId}/query`, {
      method: "POST", body: JSON.stringify(body),
    });
    state.lastResults = data.results;
    state.lastComparison = data.comparison;
    renderResults(data.results);
    renderComparison(data.comparison, data.compare_error, cfg);
    setRunStatus(`执行完成（SQLite ${data.results[0].duration_ms}ms / DuckDB ${data.results[1].duration_ms}ms）`);
  } catch (e) {
    setRunStatus("请求失败: " + e.message, true);
  }
}

function setRunStatus(text, isError) {
  const el = $("run-status");
  el.textContent = text;
  el.style.color = isError ? "var(--bad)" : "var(--muted)";
}

// ---------------- 结果渲染 ----------------
function renderResults(results) {
  results.forEach((r) => {
    $("meta-" + r.engine).textContent = r.ok
      ? `${r.rows.length} 行 · ${r.duration_ms}ms${r.truncated ? " · 已截断" : ""}`
      : `执行失败 · ${r.duration_ms}ms`;
    const body = $("result-" + r.engine);
    if (!r.ok) {
      body.innerHTML = `<div class="err-box">${esc(r.error || "未知错误")}</div>`;
      return;
    }
    const head = r.columns.map((c, i) =>
      `<th>${esc(c)}<span class="th-type">${esc(r.column_types[i] || "?")}</span></th>`).join("");
    const rows = r.rows.map((row) =>
      "<tr>" + row.map((v) => {
        const f = fmtCell(v);
        return `<td class="${f.cls}">${f.text}</td>`;
      }).join("") + "</tr>").join("");
    body.innerHTML = `
      ${r.truncated ? '<div class="trunc-note">结果超过 500 行，仅保留前 500 行用于展示与对照</div>' : ""}
      <div class="table-wrap">
        <table>
          <thead><tr>${head}</tr></thead>
          <tbody>${rows || '<tr><td class="null">空结果集（0 行）</td></tr>'}</tbody>
        </table>
      </div>`;
  });
}

function renderComparison(rep, compareError, cfg) {
  const banner = $("comparison-banner");
  const list = $("diff-list");
  banner.className = "banner";
  if (compareError) {
    banner.classList.add("warn");
    banner.innerHTML = `<div class="b-title">对照无法完成</div><div>${esc(compareError)}</div>`;
    list.innerHTML = "";
    return;
  }
  if (!rep) {
    banner.classList.add("warn");
    banner.textContent = "无对照结果";
    list.innerHTML = "";
    return;
  }
  const map = {
    identical: ["ok", "✓ 结果一致"],
    different: ["bad", "✗ 发现差异"],
    error: ["error", "⛔ 引擎报错（报错作为真实结果保留，未做行比较）"],
    structure_diff: ["warn", "⚠ 结果列结构不同"],
  };
  const [cls, title] = map[rep.status] || ["warn", rep.status];
  banner.classList.add(cls);
  banner.innerHTML = `
    <div class="b-title">${title}</div>
    <div>${esc(rep.summary)}</div>
    <div class="b-meta">比较模式：${rep.mode === "ordered" ? "逐行有序" : "集合（无序，重复次数计入）"} ·
      行数 SQLite ${rep.row_counts[0]} / DuckDB ${rep.row_counts[1]} ·
      容差 abs=${cfg.abs_tol} rel=${cfg.rel_tol}${rep.rep_only_count ? ` · ${rep.rep_only_count} 处仅表示形式差异` : ""}
    </div>`;

  if (!rep.row_diffs || !rep.row_diffs.length) {
    list.innerHTML = "";
    return;
  }
  list.innerHTML = rep.row_diffs.map((d, idx) => renderDiffItem(d, idx, cfg)).join("");
  list.querySelectorAll(".diff-head").forEach((head) => {
    head.addEventListener("click", () => {
      const body = head.nextElementSibling;
      body.classList.toggle("hidden");
    });
  });
  list.querySelectorAll(".drill-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openDrawer(parseInt(btn.dataset.idx, 10));
    });
  });
}

function renderDiffItem(d, idx, cfg) {
  const repOnly = d.cell_diffs.length && d.cell_diffs.every((c) => c.representation_only);
  const kindLabel = {
    value_diff: repOnly ? "表示差异(值等价)" : "值不同",
    only_in_sqlite: "仅 SQLite 有",
    only_in_duckdb: "仅 DuckDB 有",
  }[d.kind] || d.kind;
  const keyTxt = Object.keys(d.key_values || {}).length
    ? "键 " + Object.entries(d.key_values).map(([k, v]) => `${k}=${fmtCell(v).text}`).join(", ")
    : "";
  let body = "";
  if (d.cell_diffs && d.cell_diffs.length) {
    body = d.cell_diffs.map((c) => `
      <div class="cell-diff-row">
        <div class="col-name">${esc(c.column)}</div>
        <div class="cell-val changed ${c.representation_only ? "rep" : ""}">
          ${fmtCell(c.sqlite_value).text}
          <span class="type-tag">SQLite: ${esc(c.sqlite_type)}</span>
        </div>
        <div class="cell-val changed ${c.representation_only ? "rep" : ""}">
          ${fmtCell(c.duckdb_value).text}
          <span class="type-tag">DuckDB: ${esc(c.duckdb_type)}</span>
        </div>
        <div class="cell-detail">${esc(c.detail)}</div>
      </div>`).join("");
  } else {
    const sRow = d.sqlite_row ? JSON.stringify(d.sqlite_row) : "—";
    const dRow = d.duckdb_row ? JSON.stringify(d.duckdb_row) : "—";
    body = `
      <div class="cell-diff-row">
        <div class="col-name">SQLite 行</div>
        <div class="cell-val" style="grid-column: span 2;">${esc(sRow)}</div>
        <div class="col-name">DuckDB 行</div>
        <div class="cell-val" style="grid-column: span 2;">${esc(dRow)}</div>
        ${d.multiplicity.some((m) => m) ? `<div class="cell-detail">重复次数 SQLite=${d.multiplicity[0]} / DuckDB=${d.multiplicity[1]}</div>` : ""}
      </div>`;
  }
  return `
  <div class="diff-item ${repOnly ? "rep-only" : ""}">
    <div class="diff-head">
      <span class="diff-kind kind-${repOnly ? "rep" : d.kind}">${esc(kindLabel)}</span>
      <span class="diff-key">${esc(keyTxt)}</span>
      <span class="diff-drill"><button class="btn btn-sm drill-btn" data-idx="${idx}">展开：源行 / 表达式 / 两边类型 →</button></span>
    </div>
    <div class="diff-body hidden">${body}</div>
  </div>`;
}

// ---------------- 下钻抽屉 ----------------
async function openDrawer(idx) {
  const rep = state.lastComparison;
  const d = rep.row_diffs[idx];
  const cfg = readConfig();
  const drawerBody = $("drawer-body");
  $("drawer").classList.remove("hidden");
  $("drawer-mask").classList.remove("hidden");

  const cellSection = d.cell_diffs.length ? `
    <h4>两边字段类型与值</h4>
    <table class="data-table">
      <thead><tr><th>列</th><th>SQLite（类型）</th><th>DuckDB（类型）</th></tr></thead>
      <tbody>${d.cell_diffs.map((c) => `
        <tr>
          <td><code>${esc(c.column)}</code></td>
          <td>${fmtCell(c.sqlite_value).text} <code style="color:var(--muted)">${esc(c.sqlite_type)}</code></td>
          <td>${fmtCell(c.duckdb_value).text} <code style="color:var(--muted)">${esc(c.duckdb_type)}</code></td>
        </tr>`).join("")}</tbody>
    </table>
    <p class="hint">${esc(d.cell_diffs.map((c) => c.detail).join("；"))}</p>` : "";

  drawerBody.innerHTML = `
    <h4>两边执行的表达式（SQL）</h4>
    <div class="src-block">
      <div class="src-tag sqlite">SQLite</div>
      <div class="expr-box">${esc(state.lastSql.sqlite)}</div>
    </div>
    <div class="src-block">
      <div class="src-tag duckdb">DuckDB</div>
      <div class="expr-box">${esc(state.lastSql.duckdb)}</div>
    </div>
    ${cellSection}
    <h4>源表记录回查（按业务主键）</h4>
    <div id="source-area"><p class="hint">正在回查…</p></div>`;

  await loadSourceRows(d, cfg);
}

async function loadSourceRows(d, cfg) {
  const area = $("source-area");
  const table = state.schema.find((t) =>
    keyColumns.every((k) => t.columns.some((c) => c.name === k)))?.table
    || state.schema[0]?.table;
  if (!table) {
    area.innerHTML = '<p class="hint">当前会话没有已导入的表，无法回溯源行。</p>';
    return;
  }
  const kv = d.key_values || {};
  const keyColumnsEff = keyColumns.length ? keyColumns : Object.keys(kv);
  const keyValues = keyColumnsEff.map((k) => kv[k] ?? null);
  if (!keyColumnsEff.length || keyColumnsEff.some((k) => !(k in kv))) {
    area.innerHTML = '<p class="hint">该差异未携带业务主键（空结果/无键集合比较），无法精确定位源行；可在上文结果表中查看原始输出。</p>';
    return;
  }
  try {
    const data = await api(`/api/session/${state.sessionId}/source-row`, {
      method: "POST",
      body: JSON.stringify({ table, key_columns: keyColumnsEff, key_values: keyValues }),
    });
    area.innerHTML = ["sqlite", "duckdb"].map((eng) => {
      const src = data.source[eng];
      const head = src.columns.map((c, i) =>
        `<th>${esc(c)}<span class="th-type">${esc(src.types[i] || "?")}</span></th>`).join("");
      const rows = src.rows.map((r) =>
        "<tr>" + r.map((v) => `<td class="${fmtCell(v).cls}">${fmtCell(v).text}</td>`).join("") + "</tr>").join("");
      return `
      <div class="src-block">
        <div class="src-tag ${eng}">${eng.toUpperCase()} 源行（${src.rows.length} 条匹配）</div>
        <div class="table-wrap"><table class="data-table">
          <thead><tr>${head}</tr></thead>
          <tbody>${rows || '<tr><td class="null">无匹配</td></tr>'}</tbody>
        </table></div>
      </div>`;
    }).join("");
  } catch (e) {
    area.innerHTML = `<p class="hint">源行回查失败：${esc(e.message)}</p>`;
  }
}

function closeDrawer() {
  $("drawer").classList.add("hidden");
  $("drawer-mask").classList.add("hidden");
}

// ---------------- 方言表 ----------------
function renderDialectTable() {
  $("dialect-table").querySelector("tbody").innerHTML =
    state.meta.dialect_notes.map((n) => `
      <tr>
        <td><strong>${esc(n.topic)}</strong></td>
        <td>${esc(n.sqlite)}</td>
        <td>${esc(n.duckdb)}</td>
        <td><code>${esc(n.portable)}</code></td>
      </tr>`).join("");
}

init();
