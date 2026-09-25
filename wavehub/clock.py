"""时间约定：所有业务时间均为带时区的 ISO 8601（见领域合同 time_policy）。"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"业务时间必须包含时区：{value}")
    return dt


def iso(value: str | datetime) -> str:
    return parse_dt(value).isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
