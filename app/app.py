"""Flask backend: upload notice HTML, pick table, edit periods, export CSV."""

import csv
import io
import os

from flask import Flask, Response, jsonify, render_template, request

from parser import parse_html_tables
from timeparse import parse_datetime

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

SAMPLE_PATH = os.path.join(app.root_path, "sample", "sample_notice.html")

CSV_COLUMNS = ["路段", "方向", "开始", "结束"]


def _bad_request(message):
    return jsonify({"error": message}), 400


def _decode_upload(file_storage):
    raw = file_storage.read()
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


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

    rows_out = []
    errors = []
    for idx, row in enumerate(payload["rows"]):
        road = (row.get("road") or "").strip()
        direction = (row.get("direction") or "").strip()
        start_text = (row.get("start") or "").strip()
        end_text = (row.get("end") or "").strip()

        start_dt = parse_datetime(start_text)
        end_dt = parse_datetime(end_text)
        row_errors = []
        if not start_text or start_dt is None:
            row_errors.append("开始时间无法解析")
        if not end_text or end_dt is None:
            row_errors.append("结束时间无法解析")
        if start_dt is not None and end_dt is not None and end_dt < start_dt:
            row_errors.append("结束早于开始")
        if row_errors:
            errors.append({"row": idx, "messages": row_errors})

        rows_out.append([road, direction, start_text, end_text])

    if errors:
        return jsonify({"error": "存在错误行，已阻止导出", "errors": errors}), 422

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(rows_out)
    # UTF-8 BOM 作为字节前缀写入，Excel 可直接正确识别编码
    payload = b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
    return Response(
        payload,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=traffic_control.csv"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
