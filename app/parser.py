"""Parse construction-notice HTML into column-aligned grids.

Merged cells (rowspan / colspan) are expanded: covered positions carry the
origin cell's id and text so the frontend can group and edit shared values.
"""

from bs4 import BeautifulSoup


def parse_html_tables(html):
    """Return a list of table dicts found in *html*.

    Each table: {"index", "rowCount", "colCount", "rows": [[cell, ...], ...]}
    Each cell: {"id", "text", "rowSpan", "colSpan", "originRow",
                "originCol", "covered", "isHeader"}
    """
    soup = BeautifulSoup(html or "", "html.parser")
    return [_parse_table(table, i) for i, table in enumerate(soup.find_all("table"))]


def _span(value):
    try:
        return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        return 1


def _cell_text(cell):
    return " ".join(cell.stripped_strings)


def _blank_cell(table_idx, row_idx, col_idx):
    return {
        "id": f"t{table_idx}-r{row_idx}-c{col_idx}",
        "text": "",
        "rowSpan": 1,
        "colSpan": 1,
        "originRow": row_idx,
        "originCol": col_idx,
        "covered": False,
        "isHeader": False,
    }


def _covered_from(origin):
    return {
        "id": origin["id"],
        "text": origin["text"],
        "rowSpan": origin["rowSpan"],
        "colSpan": origin["colSpan"],
        "originRow": origin["originRow"],
        "originCol": origin["originCol"],
        "covered": True,
        "isHeader": origin["isHeader"],
    }


def _parse_table(table, table_idx):
    grid = []
    # col -> [remaining rows to fill, origin cell dict]
    pending = {}

    for tr in table.find_all("tr"):
        row = []
        col = 0
        cells = tr.find_all(["td", "th"], recursive=False)
        ci = 0

        while ci < len(cells) or any(c < col or c in pending for c in pending):
            if col in pending:
                remaining, origin = pending[col]
                row.append(_covered_from(origin))
                remaining -= 1
                if remaining <= 0:
                    del pending[col]
                else:
                    pending[col][0] = remaining
                col += 1
                continue

            if ci >= len(cells):
                break

            cell = cells[ci]
            ci += 1
            row_span = _span(cell.get("rowspan"))
            col_span = _span(cell.get("colspan"))
            origin = {
                "id": f"t{table_idx}-r{len(grid)}-c{col}",
                "text": _cell_text(cell),
                "rowSpan": row_span,
                "colSpan": col_span,
                "originRow": len(grid),
                "originCol": col,
                "covered": False,
                "isHeader": cell.name == "th",
            }

            for k in range(col_span):
                target_col = col + k
                if k == 0:
                    row.append(dict(origin))
                else:
                    row.append(_covered_from(origin))
                if row_span > 1:
                    pending[target_col] = [row_span - 1, origin]
            col += col_span

        grid.append(row)

    # Flush rowspans left over by truncated/malformed tables.
    while pending:
        row = []
        for col in range(max(pending) + 1):
            if col in pending:
                remaining, origin = pending[col]
                row.append(_covered_from(origin))
                remaining -= 1
                if remaining <= 0:
                    del pending[col]
                else:
                    pending[col][0] = remaining
            else:
                row.append(_blank_cell(table_idx, len(grid), col))
        grid.append(row)

    width = max((len(r) for r in grid), default=0)
    for r_idx, row in enumerate(grid):
        while len(row) < width:
            row.append(_blank_cell(table_idx, r_idx, len(row)))

    return {
        "index": table_idx,
        "rowCount": len(grid),
        "colCount": width,
        "rows": grid,
    }
