"""Flask backend: upload notice HTML, pick table, edit periods, export CSV.

Besides the ordinary CSV export there is a "handover" download: the CSV that
is actually delivered is archived in SQLite together with per-cell provenance,
and a lookup API lets a reviewer find the original notice cell behind a
batch/row/field from a returned (rejected) handover.
"""

import csv
import hashlib
import io
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request, url_for

from parser import parse_html_tables
from timeparse import parse_datetime

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["HANDOVER_DB"] = os.environ.get(
    "HANDOVER_DB", os.path.join(app.root_path, "data", "handovers.db")
)
app.json.ensure_ascii = False

SAMPLE_PATH = os.path.join(app.root_path, "sample", "sample_notice.html")

CSV_COLUMNS = ["路段", "方向", "开始", "结束"]
FIELD_LABELS = {"road": "路段", "direction": "方向", "start": "开始", "end": "结束"}
FIELD_KEYS = ("road", "direction", "start", "end")
FIELD_INDEX = {field: i for i, field in enumerate(FIELD_KEYS)}
TIME_FIELDS = frozenset(("start", "end"))

FIELD_ALIASES = {
    "road": "road",
    "路段": "road",
    "direction": "direction",
    "方向": "direction",
    "start": "start",
    "开始": "start",
    "开始时间": "start",
    "end": "end",
    "结束": "end",
    "结束时间": "end",
}

TIME_SOURCES = frozenset(("original", "shared_edit", "override"))
TEXT_SOURCES = frozenset(("original", "edited"))
SOURCE_LABELS = {
    "original": "原值",
    "shared_edit": "共用改值",
    "override": "单行覆盖",
    "edited": "采录时编辑",
}
SOURCE_DESCRIPTIONS = {
    "original": "公告原格未编辑的原文，直接取用",
    "shared_edit": "合并格原格被编辑后的共用值，本行未单独覆盖",
    "override": "本行单独设置的覆盖值，不随原格共用值变化",
    "edited": "采录时将公告原格原文改成了实际交付值",
}

_BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_no TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    notice_file_name TEXT NOT NULL,
    table_no INTEGER NOT NULL,
    table_index INTEGER,
    mapping_json TEXT NOT NULL DEFAULT '{}',
    skip_header INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL,
    csv_file_name TEXT NOT NULL,
    csv_bytes BLOB NOT NULL,
    csv_sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS handover_cells (
    batch_no TEXT NOT NULL REFERENCES batches(batch_no) ON DELETE CASCADE,
    row_no INTEGER NOT NULL,
    field TEXT NOT NULL,
    field_label TEXT NOT NULL,
    value TEXT NOT NULL,
    original_text TEXT NOT NULL,
    origin_id TEXT,
    origin_row INTEGER NOT NULL,
    origin_col INTEGER NOT NULL,
    covered INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,
    grid_row INTEGER,
    PRIMARY KEY (batch_no, row_no, field)
);

CREATE INDEX IF NOT EXISTS idx_handover_cells_batch
    ON handover_cells(batch_no);
"""


# ---------------------------------------------------------------------------
# Small payload/database helpers
# ---------------------------------------------------------------------------


def _bad_request(message):
    return jsonify({"error": message}), 400


def _as_text(value):
    return "" if value is None else str(value).strip()


def _as_raw_text(value):
    return "" if value is None else str(value)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return False


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decode_upload(file_storage):
    raw = file_storage.read()
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _db_path():
    path = app.config["HANDOVER_DB"]
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create the archive tables if needed. Safe to run in every worker."""
    last_error = None
    for attempt in range(5):
        try:
            conn = _connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_BATCH_SCHEMA)
                conn.commit()
                return
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            # Several workers may start at once; retry transient locks.
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    raise last_error


def _row_values(row):
    """Return the four delivered values from either nested or legacy rows."""
    if not isinstance(row, dict):
        return {field: "" for field in FIELD_KEYS}
    source = row.get("values") if isinstance(row.get("values"), dict) else row
    cells = row.get("cells") if isinstance(row.get("cells"), dict) else {}
    values = {}
    for field in FIELD_KEYS:
        value = source.get(field) if isinstance(source, dict) else None
        if value is None and isinstance(cells.get(field), dict):
            value = cells[field].get("value")
        values[field] = _as_text(value)
    return values


def _validate_rows(raw_rows):
    """Same validation and row order as the ordinary CSV export."""
    rows_out = []
    errors = []
    for idx, row in enumerate(raw_rows):
        try:
            values = _row_values(row)
        except (TypeError, ValueError):
            values = {field: "" for field in FIELD_KEYS}
        start_dt = parse_datetime(values["start"])
        end_dt = parse_datetime(values["end"])
        row_errors = []
        if not values["start"] or start_dt is None:
            row_errors.append("开始时间无法解析")
        if not values["end"] or end_dt is None:
            row_errors.append("结束时间无法解析")
        if start_dt is not None and end_dt is not None and end_dt < start_dt:
            row_errors.append("结束早于开始")
        if row_errors:
            errors.append({"row": idx, "messages": row_errors})

        rows_out.append([
            values["road"],
            values["direction"],
            values["start"],
            values["end"],
        ])
    return rows_out, errors


def _csv_payload(rows_out):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(rows_out)
    # UTF-8 BOM 作为字节前缀写入，Excel 可直接正确识别编码
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def _cell_error(row_idx, field, detail):
    return f"第 {row_idx + 1} 行{FIELD_LABELS.get(field, field)}：{detail}"


def _normalize_provenance(raw_rows, rows_out, table_no):
    """Validate and normalize per-cell provenance sent by the transcription page."""
    normalized = []
    for idx, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            return None, _cell_error(idx, "road", "缺少逐格来源数据")
        cells = raw.get("cells")
        if not isinstance(cells, dict):
            return None, f"第 {idx + 1} 行缺少逐格来源数据"

        grid_row = _as_int(raw.get("gridRow"))
        normalized_cells = []
        for field in FIELD_KEYS:
            raw_cell = cells.get(field)
            label = FIELD_LABELS[field]
            if not isinstance(raw_cell, dict):
                return None, f"第 {idx + 1} 行{label}缺少来源信息"

            original_text = raw_cell.get("originalText")
            if original_text is None:
                original_text = raw_cell.get("original_text")
            if original_text is None:
                original_text = raw_cell.get("text")
            if original_text is None:
                return None, f"第 {idx + 1} 行{label}缺少未编辑原文"
            original_text = _as_raw_text(original_text)

            origin_row = _as_int(raw_cell.get("originRow"))
            origin_col = _as_int(raw_cell.get("originCol"))
            if origin_row is None or origin_col is None or origin_row < 0 or origin_col < 0:
                return None, f"第 {idx + 1} 行{label}缺少有效的原格行列"

            origin_id = raw_cell.get("originId")
            if origin_id is None:
                origin_id = raw_cell.get("cellId")
            origin_id = _as_text(origin_id)
            if not origin_id:
                origin_id = f"t{table_no - 1}-r{origin_row}-c{origin_col}"

            covered = _as_bool(raw_cell.get("covered"))
            source = _as_text(raw_cell.get("source"))
            if field in TIME_FIELDS:
                if not source:
                    if _as_bool(raw_cell.get("override")):
                        source = "override"
                    elif (_as_bool(raw_cell.get("sharedEdited"))
                          or _as_bool(raw_cell.get("originEdited"))):
                        source = "shared_edit"
                    else:
                        source = "original"
                if source not in TIME_SOURCES:
                    return None, f"第 {idx + 1} 行{label}的取值来源无效：{source}"
            else:
                if not source:
                    source = ("edited" if rows_out[idx][FIELD_INDEX[field]] != original_text
                              else "original")
                if source not in TEXT_SOURCES:
                    return None, f"第 {idx + 1} 行{label}的取值来源无效：{source}"

            delivered = rows_out[idx][FIELD_INDEX[field]]
            if source == "original" and delivered != original_text:
                return None, (
                    f"第 {idx + 1} 行{label}的取值来源标为原值，"
                    "但实际交付值与原格原文不一致"
                )

            normalized_cells.append({
                "field": field,
                "field_label": label,
                "value": rows_out[idx][FIELD_INDEX[field]],
                "original_text": original_text,
                "origin_id": origin_id,
                "origin_row": origin_row,
                "origin_col": origin_col,
                "covered": 1 if covered else 0,
                "source": source,
                "edited": 0 if source == "original" else 1,
            })
        normalized.append({"row_no": idx + 1, "grid_row": grid_row,
                           "cells": normalized_cells})
    return normalized, None


def _new_batch_no():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"TC-{stamp}-{secrets.token_hex(4).upper()}"


def _create_batch(notice_file_name, table_no, table_index, mapping_json,
                  skip_header, rows_out, normalized_rows):
    """Atomically store the delivered CSV and every cell's provenance.

    Nothing is returned until the transaction has committed, so the caller
    can never offer a download for an unarchived handover.
    """
    csv_payload = _csv_payload(rows_out)
    digest = hashlib.sha256(csv_payload).hexdigest()
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    conn = _connect()
    try:
        for _ in range(8):
            batch_no = _new_batch_no()
            csv_file_name = f"traffic_control_{batch_no}.csv"
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO batches (
                        batch_no, created_at, notice_file_name, table_no, table_index,
                        mapping_json, skip_header, row_count, csv_file_name,
                        csv_bytes, csv_sha256, byte_size
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (batch_no, created_at, notice_file_name, table_no, table_index,
                     mapping_json, 1 if skip_header else 0, len(rows_out),
                     csv_file_name, csv_payload, digest, len(csv_payload)),
                )
                cell_rows = []
                for row in normalized_rows:
                    for cell in row["cells"]:
                        cell_rows.append((
                            batch_no, row["row_no"], cell["field"],
                            cell["field_label"], cell["value"], cell["original_text"],
                            cell["origin_id"], cell["origin_row"], cell["origin_col"],
                            cell["covered"], cell["source"], cell["edited"],
                            row["grid_row"],
                        ))
                conn.executemany(
                    """
                    INSERT INTO handover_cells (
                        batch_no, row_no, field, field_label, value, original_text,
                        origin_id, origin_row, origin_col, covered, source, edited,
                        grid_row
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    cell_rows,
                )
                conn.commit()
                return {
                    "batch_no": batch_no,
                    "created_at": created_at,
                    "csv_file_name": csv_file_name,
                    "byte_size": len(csv_payload),
                    "sha256": digest,
                }
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                # Only a batch-number collision should be retried; anything
                # else means the archive would be incomplete.
                if "batch_no" not in str(exc):
                    raise
                continue
        raise sqlite3.IntegrityError("无法分配唯一批号")
    finally:
        conn.close()


def _find_batch(conn, batch_no):
    return conn.execute(
        "SELECT * FROM batches WHERE batch_no = ? COLLATE NOCASE", (batch_no,)
    ).fetchone()


def _batch_json(row):
    mapping = {}
    try:
        mapping = json.loads(row["mapping_json"] or "{}")
    except (TypeError, ValueError):
        mapping = {}
    return {
        "batchNo": row["batch_no"],
        "createdAt": row["created_at"],
        "noticeFileName": row["notice_file_name"],
        "tableNo": row["table_no"],
        "tableIndex": row["table_index"],
        "mapping": mapping,
        "skipHeader": bool(row["skip_header"]),
        "rowCount": row["row_count"],
        "csvFileName": row["csv_file_name"],
        "byteSize": row["byte_size"],
        "sha256": row["csv_sha256"],
        "downloadUrl": url_for("handover_download", batch_no=row["batch_no"]),
    }


def _cell_json(row, table_no):
    field = row["field"]
    source = row["source"]
    origin_row = row["origin_row"]
    origin_col = row["origin_col"]
    return {
        "rowNo": row["row_no"],
        "field": field,
        "fieldLabel": row["field_label"],
        "value": row["value"],
        "originalText": row["original_text"],
        "tableNo": table_no,
        "originId": row["origin_id"] or "",
        "originRow": origin_row,
        "originCol": origin_col,
        "originRowLabel": origin_row + 1,
        "originColLabel": origin_col + 1,
        "covered": bool(row["covered"]),
        "source": source,
        "sourceLabel": SOURCE_LABELS.get(source, source),
        "sourceDescription": SOURCE_DESCRIPTIONS.get(source, source),
        "edited": bool(row["edited"]),
    }


def _normalize_field(value):
    return FIELD_ALIASES.get(_as_text(value).lower())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/upload")
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        return _bad_request("请选择公告 HTML 文件")
    html = _decode_upload(file)
    tables = parse_html_tables(html)
    if not tables:
        return _bad_request("公告里没有解析到任何 <table>")
    return jsonify({"fileName": file.filename, "tables": tables})


@app.get("/api/sample")
def sample():
    with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
        html = fh.read()
    return jsonify({"fileName": "sample_notice.html", "tables": parse_html_tables(html)})


@app.post("/api/export")
def export():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        return _bad_request("缺少 rows 数据")

    rows_out, errors = _validate_rows(payload["rows"])
    if errors:
        return jsonify({"error": "存在错误行，已阻止导出", "errors": errors}), 422

    return Response(
        _csv_payload(rows_out),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=traffic_control.csv"},
    )


@app.post("/api/handover")
def handover():
    """Archive the actual delivered CSV plus per-cell provenance, then allow download."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        return _bad_request("缺少 rows 数据")

    notice_file_name = _as_text(payload.get("fileName"))
    if not notice_file_name:
        return _bad_request("缺少公告文件名")

    rows_out, errors = _validate_rows(payload["rows"])
    if errors:
        return jsonify({"error": "存在错误行，已阻止交接", "errors": errors}), 422

    table_no = _as_int(payload.get("tableNo"))
    table_index = _as_int(payload.get("tableIndex"))
    if table_no is None:
        table_no = table_index + 1 if table_index is not None and table_index >= 0 else 1
    if table_no < 1:
        return _bad_request("表号无效")

    normalized_rows, provenance_error = _normalize_provenance(
        payload["rows"], rows_out, table_no
    )
    if provenance_error:
        return _bad_request(provenance_error)

    mapping = payload.get("mapping")
    mapping_json = json.dumps(mapping, ensure_ascii=False, sort_keys=True) \
        if isinstance(mapping, dict) else "{}"

    try:
        stored = _create_batch(
            notice_file_name=notice_file_name,
            table_no=table_no,
            table_index=table_index,
            mapping_json=mapping_json,
            skip_header=_as_bool(payload.get("skipHeader")),
            rows_out=rows_out,
            normalized_rows=normalized_rows,
        )
    except (sqlite3.Error, OSError):
        app.logger.exception("交接存档失败")
        return jsonify({"error": "交接存档失败，未提供下载"}), 500

    return jsonify({
        "batchNo": stored["batch_no"],
        "createdAt": stored["created_at"],
        "noticeFileName": notice_file_name,
        "tableNo": table_no,
        "rowCount": len(rows_out),
        "csvFileName": stored["csv_file_name"],
        "byteSize": stored["byte_size"],
        "sha256": stored["sha256"],
        "downloadUrl": url_for("handover_download", batch_no=stored["batch_no"]),
        "message": "交接文件已存入存档，可开始下载",
    })


@app.get("/api/handover/<batch_no>")
def handover_lookup(batch_no):
    """Look up an archived batch, optionally one data row and/or field."""
    conn = _connect()
    try:
        batch = _find_batch(conn, batch_no)
        if batch is None:
            return jsonify({"error": "未找到该批号"}), 404

        row_param = request.args.get("row")
        field_param = request.args.get("field")
        row_no = None
        field = None

        if row_param not in (None, ""):
            row_no = _as_int(row_param)
            if row_no is None or row_no < 1:
                return _bad_request("数据行号必须是大于等于 1 的整数")
            if row_no > batch["row_count"]:
                return jsonify({"error": "该批号没有这一数据行"}), 404

        if field_param not in (None, ""):
            field = _normalize_field(field_param)
            if field is None:
                return _bad_request("字段名无效，可用：路段、方向、开始、结束")

        conditions = ["batch_no = ?"]
        params = [batch["batch_no"]]
        if row_no is not None:
            conditions.append("row_no = ?")
            params.append(row_no)
        if field is not None:
            conditions.append("field = ?")
            params.append(field)

        cells = []
        if row_no is not None or field is not None:
            query = (
                "SELECT * FROM handover_cells WHERE " + " AND ".join(conditions)
                + " ORDER BY row_no, CASE field"
                " WHEN 'road' THEN 1 WHEN 'direction' THEN 2"
                " WHEN 'start' THEN 3 ELSE 4 END"
            )
            rows = conn.execute(query, params).fetchall()
            if not rows:
                return jsonify({"error": "该批号没有匹配的单元格"}), 404
            cells = [_cell_json(cell, batch["table_no"]) for cell in rows]

        return jsonify({
            "batch": _batch_json(batch),
            "query": {"row": row_no, "field": field},
            "cells": cells,
            "availableFields": [
                {"field": field_key, "label": FIELD_LABELS[field_key]}
                for field_key in FIELD_KEYS
            ],
        })
    finally:
        conn.close()


@app.get("/api/handover/<batch_no>/download")
def handover_download(batch_no):
    """Return the exact archived CSV bytes; survives restarts and re-imports."""
    conn = _connect()
    try:
        batch = _find_batch(conn, batch_no)
        if batch is None:
            return jsonify({"error": "未找到该批号"}), 404
        csv_bytes = batch["csv_bytes"]
        csv_file_name = batch["csv_file_name"]
    finally:
        conn.close()

    response = Response(csv_bytes, content_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{csv_file_name}"'
    )
    response.headers["X-Batch-No"] = batch_no
    response.headers["Cache-Control"] = "no-store"
    return response


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
