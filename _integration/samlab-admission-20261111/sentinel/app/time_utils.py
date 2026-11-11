"""统一时间存储与展示工具。

存储和接口使用带时区的 UTC ISO-8601 字符串；面向中国用户的页面显示
转换为 Asia/Shanghai。没有时区的历史值按 UTC 解析，并保留该假设。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta, tzinfo
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]


UTC = timezone.utc
if ZoneInfo is not None:
    try:
        DISPLAY_ZONE: tzinfo = ZoneInfo("Asia/Shanghai")
    except Exception:
        DISPLAY_ZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
else:
    DISPLAY_ZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
DISPLAY_ZONE_LABEL = "北京时间（Asia/Shanghai）"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def next_timestamp(value: str | None) -> str:
    """Compatibility helper returning a real clock value without offsets.

    Historical callers may still import this name. It intentionally does not
    force a value to be greater than ``value``; equal timestamps are valid
    evidence when two phases complete within the same clock precision.
    """
    del value
    return utc_now_iso()


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_timestamp(value: str | None, unknown: str = "未记录") -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        return unknown
    local = parsed.astimezone(DISPLAY_ZONE)
    return f"{local:%Y-%m-%d %H:%M:%S}（北京时间）"


def normalize_timestamp(value: str | None) -> str | None:
    parsed = parse_timestamp(value)
    return parsed.replace(microsecond=0).isoformat() if parsed else None


def timestamp_precision(value: str | None) -> str:
    """Report precision present in an ISO timestamp without inventing it."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return "unknown"
    return "subsecond" if parsed.microsecond else "second"
