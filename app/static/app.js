/* 道路施工管制时段采录 —— 前端逻辑 */
"use strict";

const state = {
  fileName: "",
  tables: [],
  tableIndex: 0,
  mapping: { road: 1, direction: 2, start: 3, end: 4 },
  skipHeader: true,
  rows: [],          // 每行: { road, direction, start: TimeField, end: TimeField }
  origins: { start: {}, end: {} }, // field -> originId -> { text, rowIndexes: number[] }
};

const FIELD_LABELS = { road: "路段", direction: "方向", start: "开始", end: "结束" };
const HEADER_KEYWORDS = {
  road: ["路段", "道路", "地点", "位置"],
  direction: ["方向", "朝向"],
  start: ["开始", "起始", "起"],
  end: ["结束", "截止", "止"],
};

const $ = (sel) => document.querySelector(sel);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

/* ---------- 时间解析（与后端 timeparse.py 规则一致） ---------- */

function parseDateTime(text) {
  const s = String(text ?? "").trim();
  if (!s) return null;

  const build = (y, mo, d, h, mi, se) => {
    const dt = new Date(Number(y), Number(mo) - 1, Number(d), Number(h) || 0,
                        Number(mi) || 0, Number(se) || 0);
    const valid = dt.getFullYear() === Number(y) && dt.getMonth() === Number(mo) - 1 &&
      dt.getDate() === Number(d) && dt.getHours() === (Number(h) || 0) &&
      dt.getMinutes() === (Number(mi) || 0) && dt.getSeconds() === (Number(se) || 0);
    return valid ? dt : null;
  };

  let m = s.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
  if (m) return build(...m.slice(1));

  m = s.match(/^(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})时(\d{1,2})分(?:(\d{1,2})秒)?$/);
  if (m) return build(...m.slice(1));

  m = s.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/);
  if (m) return build(m[1], m[2], m[3], 0, 0, 0);

  m = s.match(/^(\d{4})年(\d{1,2})月(\d{1,2})日$/);
  if (m) return build(m[1], m[2], m[3], 0, 0, 0);

  // 宽松兜底：日期时间数字分组
  m = s.match(/^\s*(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})(?:\D+(\d{1,2}))?\D*$/);
  if (m) return build(...m.slice(1));

  return null;
}

function effectiveValue(row, field) {
  const f = row[field];
  return f.override !== null ? f.override : state.origins[field][f.originId].text;
}

/* ---------- 数据载入 ---------- */

async function loadFromUrl(url) {
  $("#source-error").hidden = true;
  const resp = await fetch(url);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || "载入失败");
  state.fileName = data.fileName;
  state.tables = data.tables;
  state.tableIndex = 0;
  $("#source-name").textContent = `已载入：${data.fileName}（${data.tables.length} 张表）`;
  $("#mapping-card").hidden = false;
  $("#editor-card").hidden = false;
  initTableSelect();
  selectTable(0);
}

function currentTable() {
  return state.tables[state.tableIndex];
}

function initTableSelect() {
  const sel = $("#table-select");
  sel.innerHTML = state.tables.map((t, i) =>
    `<option value="${i}">表 ${i + 1}（${t.rowCount} 行 × ${t.colCount} 列）</option>`).join("");
}

function selectTable(index) {
  state.tableIndex = index;
  const table = currentTable();
  const headerCells = table.rows[0] || [];

  // 列下拉
  for (const key of ["road", "direction", "start", "end"]) {
    const sel = $(`#map-${key}`);
    sel.innerHTML = Array.from({ length: table.colCount }, (_, c) => {
      const h = headerCells[c]?.text || "";
      return `<option value="${c}">第 ${c + 1} 列${h ? `（${escapeHtml(h)}）` : ""}</option>`;
    }).join("");
  }

  // 自动识别列
  for (const [key, words] of Object.entries(HEADER_KEYWORDS)) {
    let found = -1;
    headerCells.forEach((cell, c) => {
      if (found === -1 && words.some((w) => cell.text.includes(w))) found = c;
    });
    state.mapping[key] = found !== -1 ? found
      : Math.min(key === "road" ? 1 : key === "direction" ? 2 : key === "start" ? 3 : 4,
                 table.colCount - 1);
    $(`#map-${key}`).value = state.mapping[key];
  }

  renderPreview();
  buildEditor();
}

/* ---------- 原表预览 ---------- */

function renderPreview() {
  const table = currentTable();
  const mappedCols = new Set(Object.values(state.mapping));
  const html = ["<thead></thead><tbody>"];
  table.rows.forEach((row) => {
    html.push("<tr>");
    row.forEach((cell, c) => {
      const tag = cell.isHeader ? "th" : "td";
      const classes = [
        cell.covered ? "covered" : "",
        mappedCols.has(c) ? "mapped" : "",
      ].filter(Boolean).join(" ");
      html.push(`<${tag} class="${classes}">${escapeHtml(cell.text) || "&nbsp;"}</${tag}>`);
    });
    html.push("</tr>");
  });
  html.push("</tbody>");
  $("#preview-table").innerHTML = html.join("");
}

/* ---------- 采录模型 ---------- */

function buildEditor() {
  const table = currentTable();
  const { road, direction, start, end } = state.mapping;
  state.rows = [];
  state.origins = { start: {}, end: {} };

  const dup = new Set(Object.values(state.mapping)
    .filter((c, _, arr) => arr.indexOf(c) !== arr.lastIndexOf(c)));
  $("#source-error").hidden = dup.size === 0;
  if (dup.size) {
    $("#source-error").textContent = "提示：路段/方向/开始/结束存在重复选择的列，请确认列映射是否正确。";
  }

  table.rows.forEach((gridRow, r) => {
    const roadCell = gridRow[road];
    const dirCell = gridRow[direction];
    const startCell = gridRow[start];
    const endCell = gridRow[end];

    if (state.skipHeader && [roadCell, dirCell, startCell, endCell].every((c) => c?.isHeader)) {
      return;
    }

    const row = {
      road: roadCell?.text || "",
      direction: dirCell?.text || "",
      start: makeTimeField(startCell, "start", r),
      end: makeTimeField(endCell, "end", r),
    };
    state.rows.push(row);
  });

  renderEditor();
}

function makeTimeField(cell, field, _gridRow) {
  const id = cell.id;
  if (!state.origins[field][id]) {
    state.origins[field][id] = { text: cell.text, rowIndexes: [] };
  }
  state.origins[field][id].rowIndexes.push(state.rows.length);
  return { originId: id, override: null };
}

/* ---------- 编辑区渲染 ---------- */

function renderEditor(focus) {
  const head = `
    <thead><tr>
      <th style="width:46px">#</th><th>路段</th><th>方向</th>
      <th>开始时间（含日期与时分）</th><th>结束时间（含日期与时分）</th>
    </tr></thead>`;

  const body = state.rows.map((row, i) => {
    const startErr = fieldError(row, "start");
    const endErr = fieldError(row, "end");
    const orderErr = orderError(row);
    const rowClass = startErr || endErr || orderErr ? ' class="row-error"' : "";
    return `<tr${rowClass}>
      <td>${i + 1}</td>
      <td><input class="road-input" data-field="road" data-row="${i}"
                 value="${escapeHtml(row.road)}" placeholder="路段"></td>
      <td><input class="dir-input" data-field="direction" data-row="${i}"
                 value="${escapeHtml(row.direction)}" placeholder="方向"></td>
      <td>${timeCellHtml(row, i, "start", startErr)}</td>
      <td>${timeCellHtml(row, i, "end", endErr || orderErr)}</td>
    </tr>`;
  }).join("");

  $("#editor-table").innerHTML = head + `<tbody>${body}</tbody>`;
  renderValidationSummary();

  if (focus) {
    const input = document.querySelector(
      `input.time-input[data-row="${focus.row}"][data-timefield="${focus.field}"]`);
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }
}

function timeCellHtml(row, rowIndex, field, errorMsg) {
  const f = row[field];
  const origin = state.origins[field][f.originId];
  const isOverride = f.override !== null;
  const value = isOverride ? f.override : origin.text;
  const cls = ["time-input", isOverride ? "override" : "origin",
               parseDateTime(value) === null ? "invalid" : ""].filter(Boolean).join(" ");
  const actionBtn = isOverride
    ? `<button type="button" class="btn btn-mini" data-action="revert"
         data-row="${rowIndex}" data-timefield="${field}">撤销覆盖（取原格最新值）</button>`
    : `<button type="button" class="btn btn-mini" data-action="override"
         data-row="${rowIndex}" data-timefield="${field}">单独覆盖此行</button>`;
  const badge = isOverride ? ""
    : `<span class="shared-badge">原格共用 ${origin.rowIndexes.length} 行</span>`;
  const note = isOverride
    ? `<div class="origin-note">覆盖中；原格当前值：${escapeHtml(origin.text) || "（空）"}</div>`
    : `<div class="origin-note">编辑此框将更新原格共用时间，仅作用于未覆盖的 ${origin.rowIndexes.length} 行</div>`;
  const errHtml = errorMsg ? `<div class="field-err">${escapeHtml(errorMsg)}</div>` : "";

  return `
    <input class="${cls}" data-row="${rowIndex}" data-timefield="${field}"
           value="${escapeHtml(value)}" placeholder="例如 2026-11-15 22:00">
    <div class="cell-actions">${actionBtn}${badge}</div>
    ${note}${errHtml}`;
}

function fieldError(row, field) {
  const value = effectiveValue(row, field);
  if (!String(value).trim()) return "时间不能为空";
  return parseDateTime(value) === null ? "时间格式无法解析（需含日期与时分）" : "";
}

function orderError(row) {
  const s = parseDateTime(effectiveValue(row, "start"));
  const e = parseDateTime(effectiveValue(row, "end"));
  if (s && e && e < s) return "结束时间早于开始时间";
  return "";
}

function collectErrors() {
  return state.rows.map((row, i) => {
    const messages = [fieldError(row, "start"), fieldError(row, "end"), orderError(row)]
      .filter(Boolean);
    return { row: i, messages };
  }).filter((e) => e.messages.length);
}

function renderValidationSummary() {
  const errors = collectErrors();
  const list = $("#error-list");
  if (errors.length) {
    $("#validate-status").textContent =
      `共 ${state.rows.length} 行，其中 ${errors.length} 行存在错误，导出已被阻止。`;
    $("#validate-status").className = "error-text";
    list.hidden = false;
    list.innerHTML = errors.map((e) =>
      `<li>第 ${e.row + 1} 行：${e.messages.map(escapeHtml).join("；")}</li>`).join("");
  } else {
    $("#validate-status").textContent = `共 ${state.rows.length} 行，全部校验通过，可以导出。`;
    $("#validate-status").className = "muted";
    list.hidden = true;
    list.innerHTML = "";
  }
}

/* ---------- 编辑事件 ---------- */

$("#editor-table").addEventListener("input", (ev) => {
  const target = ev.target;
  const rowIndex = Number(target.dataset.row);
  if (!Number.isInteger(rowIndex) || !state.rows[rowIndex]) return;

  if (target.dataset.field === "road" || target.dataset.field === "direction") {
    state.rows[rowIndex][target.dataset.field] = target.value;
    return;
  }

  const field = target.dataset.timefield;
  if (field !== "start" && field !== "end") return;
  const row = state.rows[rowIndex];

  if (row[field].override !== null) {
    // 单行覆盖：只改本行
    row[field].override = target.value;
    renderValidationSummary();
    target.classList.toggle("invalid", parseDateTime(target.value) === null);
    const tr = target.closest("tr");
    tr.classList.toggle("row-error",
      !!(fieldError(row, "start") || fieldError(row, "end") || orderError(row)));
  } else {
    // 原格共用时间：更新原格，仅影响未覆盖的共用行
    state.origins[field][row[field].originId].text = target.value;
    renderEditor({ row: rowIndex, field });
  }
});

$("#editor-table").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-action]");
  if (!btn) return;
  const rowIndex = Number(btn.dataset.row);
  const field = btn.dataset.timefield;
  const row = state.rows[rowIndex];

  if (btn.dataset.action === "override") {
    row[field].override = effectiveValue(row, field);
    renderEditor({ row: rowIndex, field });
  } else if (btn.dataset.action === "revert") {
    // 撤销覆盖：重新取对应原格的最新值
    row[field].override = null;
    renderEditor({ row: rowIndex, field });
  }
});

/* ---------- 导出 ---------- */

$("#export-btn").addEventListener("click", async () => {
  if (collectErrors().length) {
    renderValidationSummary();
    return;
  }
  const payload = {
    rows: state.rows.map((row) => ({
      road: row.road,
      direction: row.direction,
      start: effectiveValue(row, "start"),
      end: effectiveValue(row, "end"),
    })),
  };

  const resp = await fetch("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (resp.ok) {
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "traffic_control.csv";
    a.click();
    URL.revokeObjectURL(url);
    return;
  }

  const data = await resp.json().catch(() => ({}));
  if (resp.status === 422 && data.errors) {
    renderEditor();
    $("#validate-status").textContent =
      `后端校验拦截：${data.errors.length} 行有错误（${data.errors
        .map((e) => `第${e.row + 1}行`).join("、")}），已阻止导出。`;
    $("#validate-status").className = "error-text";
  } else {
    $("#validate-status").textContent = `导出失败：${data.error || resp.status}`;
    $("#validate-status").className = "error-text";
  }
});

/* ---------- 来源与映射事件 ---------- */

$("#file-input").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const resp = await fetch("/api/upload", { method: "POST", body: form });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "上传失败");
    state.fileName = data.fileName;
    state.tables = data.tables;
    state.tableIndex = 0;
    $("#source-error").hidden = true;
    $("#source-name").textContent = `已载入：${data.fileName}（${data.tables.length} 张表）`;
    $("#mapping-card").hidden = false;
    $("#editor-card").hidden = false;
    initTableSelect();
    selectTable(0);
  } catch (err) {
    $("#source-error").textContent = err.message;
    $("#source-error").hidden = false;
  } finally {
    ev.target.value = "";
  }
});

$("#sample-btn").addEventListener("click", () => {
  loadFromUrl("/api/sample").catch((err) => {
    $("#source-error").textContent = err.message;
    $("#source-error").hidden = false;
  });
});

$("#table-select").addEventListener("change", (ev) => selectTable(Number(ev.target.value)));

for (const key of ["road", "direction", "start", "end"]) {
  $(`#map-${key}`).addEventListener("change", (ev) => {
    state.mapping[key] = Number(ev.target.value);
    renderPreview();
    buildEditor();
  });
}

$("#skip-header").addEventListener("change", (ev) => {
  state.skipHeader = ev.target.checked;
  buildEditor();
});
