from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import math
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class WeatherRule:
    target: str
    location: str
    cron: str
    timezone: str


_CRON_RANGES = {
    "second": (0, 59),
    "minute": (0, 59),
    "hour": (0, 23),
    "day": (1, 31),
    "month": (1, 12),
    "day_of_week": (0, 7),
    "year": (1970, 9999),
}


def _validate_basic_cron_field(value: str, field: str) -> None:
    """Validate common numeric Cron syntax when APScheduler is unavailable."""
    low, high = _CRON_RANGES[field]
    for item in value.split(","):
        item = item.strip()
        if not item:
            raise ValueError(f"{field} contains an empty list item")
        base, separator, step = item.partition("/")
        if separator and (not step.isdigit() or int(step) < 1):
            raise ValueError(f"{field} step must be a positive integer")
        if base in {"*", "?"}:
            continue
        bounds = base.split("-", 1)
        if not all(part.isdigit() for part in bounds):
            # Month/day names and APScheduler-specific expressions are checked
            # by CronTrigger in production. The fallback remains permissive.
            continue
        numbers = [int(part) for part in bounds]
        if any(number < low or number > high for number in numbers):
            raise ValueError(f"{field} must be between {low} and {high}")
        if len(numbers) == 2 and numbers[0] > numbers[1]:
            raise ValueError(f"{field} range start cannot exceed its end")


def _validate_cron(cron_parts: list[str], timezone: ZoneInfo) -> None:
    cron_keys = (
        ("minute", "hour", "day", "month", "day_of_week")
        if len(cron_parts) == 5
        else ("second", "minute", "hour", "day", "month", "day_of_week")
        if len(cron_parts) == 6
        else ("second", "minute", "hour", "day", "month", "day_of_week", "year")
    )
    values = dict(zip(cron_keys, cron_parts))
    try:
        from apscheduler.triggers.cron import CronTrigger
    except (ImportError, ModuleNotFoundError):
        for field, value in values.items():
            _validate_basic_cron_field(value, field)
        return
    CronTrigger(timezone=timezone, **values)


def parse_weather_rules(raw: Any, *, strict: bool = False) -> list[WeatherRule]:
    if isinstance(raw, (list, tuple)):
        lines = [str(item or "") for item in raw]
    else:
        lines = str(raw or "").splitlines()

    rules: list[WeatherRule] = []
    errors: list[str] = []
    for line_number, line in enumerate(lines, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = [part.strip() for part in text.split("|")]
        if len(parts) != 4 or not all(parts):
            errors.append(f"第 {line_number} 行应为：用户ID | 地点 | Cron | 时区")
            continue
        target, location, cron, timezone = parts
        cron_parts = cron.split()
        if len(cron_parts) not in (5, 6, 7):
            errors.append(f"第 {line_number} 行 Cron 必须是 5、6 或 7 段")
            continue
        try:
            tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"第 {line_number} 行时区无效：{timezone}")
            continue
        try:
            _validate_cron(cron_parts, tz)
        except (TypeError, ValueError) as exc:
            errors.append(f"第 {line_number} 行 Cron 无效：{exc}")
            continue
        rules.append(WeatherRule(target, location, cron, timezone))

    if strict and errors:
        raise ValueError("；".join(errors))
    return rules


def _number(value: Any, field: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字")
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        raise ValueError(f"{field} 超出合理范围")
    return number


def _normalized_location(value: Any) -> str:
    text = re.sub(r"[\s,，.。·/\\_-]+", "", str(value or "").lower())
    return re.sub(r"(?:特别行政区|自治区|自治州|省|市|区|县)$", "", text)


def _location_matches(expected: str, actual: str) -> bool:
    left = _normalized_location(expected)
    right = _normalized_location(actual)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.72


def _parse_issued_at(value: Any, timezone: ZoneInfo) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("issued_at 不能为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("issued_at 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def validate_weather_data(
    value: Any,
    *,
    location: str,
    timezone: str,
    now: datetime | None = None,
) -> dict:
    if not isinstance(value, dict):
        raise ValueError("天气结果必须是 JSON 对象")

    tz = ZoneInfo(timezone)
    local_now = (now or datetime.now(tz)).astimezone(tz)
    actual_location = str(value.get("location") or "").strip()
    if not _location_matches(location, actual_location):
        raise ValueError(f"天气地点不匹配：期望 {location}，得到 {actual_location or '空值'}")

    raw_issued_at = value.get("issued_at")
    issued_at = (
        local_now
        if raw_issued_at is None or not str(raw_issued_at).strip()
        else _parse_issued_at(raw_issued_at, tz)
    )
    if issued_at < local_now - timedelta(hours=36) or issued_at > local_now + timedelta(hours=2):
        raise ValueError("天气数据发布时间过旧或位于未来")

    daily = value.get("daily")
    if not isinstance(daily, list) or len(daily) != 4:
        raise ValueError("daily 必须恰好包含今天及未来三天")

    current = value.get("current")
    current = current if isinstance(current, dict) else {}
    condition = str(current.get("condition") or "").strip()
    raw_temperature = current.get("temperature_c")
    has_current_temperature = not (
        raw_temperature is None or str(raw_temperature).strip() == ""
    )
    derived_from_daily = not condition or not has_current_temperature
    fallback_item = daily[0] if isinstance(daily[0], dict) else {}
    if derived_from_daily:
        fallback_low = _number(fallback_item.get("low_c"), "daily[0].low_c", -80, 60)
        fallback_high = _number(fallback_item.get("high_c"), "daily[0].high_c", -80, 60)
        condition = condition or str(fallback_item.get("condition") or "").strip()
        if not has_current_temperature:
            temperature_c = (fallback_low + fallback_high) / 2
        else:
            temperature_c = _number(raw_temperature, "current.temperature_c", -80, 60)
    else:
        temperature_c = _number(raw_temperature, "current.temperature_c", -80, 60)
    if not condition:
        raise ValueError("缺少当前天气现象，且无法从今日预报推导")
    feels_like_c = (
        temperature_c
        if current.get("feels_like_c") is None or str(current.get("feels_like_c")).strip() == ""
        else _number(current.get("feels_like_c"), "current.feels_like_c", -80, 70)
    )
    humidity_pct = (
        None
        if current.get("humidity_pct") is None or str(current.get("humidity_pct")).strip() == ""
        else _number(current.get("humidity_pct"), "current.humidity_pct", 0, 100)
    )
    normalized_current = dict(current)
    normalized_current["condition"] = condition
    normalized_current["temperature_c"] = temperature_c
    normalized_current["feels_like_c"] = feels_like_c
    normalized_current["humidity_pct"] = humidity_pct
    normalized_current["wind"] = str(current.get("wind") or "风力未提供").strip() or "风力未提供"
    normalized_current["derived_from_daily"] = derived_from_daily
    if derived_from_daily:
        normalized_current["forecast_low_c"] = fallback_low
        normalized_current["forecast_high_c"] = fallback_high

    expected_date = local_now.date()
    normalized_daily = []
    for index, item in enumerate(daily):
        if not isinstance(item, dict):
            raise ValueError(f"daily[{index}] 必须是对象")
        try:
            item_date = datetime.strptime(str(item.get("date") or ""), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"daily[{index}].date 格式无效") from exc
        if item_date != expected_date + timedelta(days=index):
            raise ValueError("daily 日期必须从当地今天起连续四天")
        daily_condition = str(item.get("condition") or "").strip()
        if not daily_condition:
            raise ValueError(f"daily[{index}].condition 不能为空")
        low_c = _number(item.get("low_c"), f"daily[{index}].low_c", -80, 60)
        high_c = _number(item.get("high_c"), f"daily[{index}].high_c", -80, 60)
        if high_c < low_c:
            raise ValueError(f"daily[{index}] 最高温不能低于最低温")
        normalized_item = dict(item)
        normalized_item["condition"] = daily_condition
        normalized_item["low_c"] = low_c
        normalized_item["high_c"] = high_c
        normalized_daily.append(normalized_item)

    sources = value.get("sources")
    if not isinstance(sources, list) or not any(str(item).strip() for item in sources):
        raise ValueError("天气结果至少需要一个可追溯来源")
    alerts = value.get("alerts", [])
    if not isinstance(alerts, list):
        raise ValueError("alerts 必须是数组")

    normalized = dict(value)
    normalized["location"] = actual_location
    normalized["timezone"] = timezone
    normalized["issued_at"] = issued_at.isoformat()
    normalized["current"] = normalized_current
    normalized["daily"] = normalized_daily
    normalized["alerts"] = alerts
    normalized["sources"] = sources
    return normalized
