"""Lenient date-time parsing for control-period cells.

Accepts common Chinese and numeric forms containing date + hour/minute,
e.g. 2026-11-15 22:00, 2026/11/15 22:00, 2026年11月15日22时00分.
"""

import re
from datetime import datetime

_STRICT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日 %H时%M分",
    "%Y年%m月%d日%H时%M分",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y年%m月%d日",
)

# Numeric groups separated by any non-digit run: 年 月 日 空格 : 等.
_LOOSE_RE = re.compile(
    r"\s*(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})"
    r"(?:\D+(\d{1,2}))?\D*$"
)


def parse_datetime(text):
    """Return a datetime, or None when *text* cannot be parsed."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None

    for fmt in _STRICT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass

    match = _LOOSE_RE.fullmatch(s)
    if match:
        year, month, day, hour, minute, second = (
            int(g) if g else 0 for g in match.groups()
        )
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
    return None
