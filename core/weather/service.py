from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .models import validate_weather_data


class WeatherService:
    SEARCH_ARG_NAMES = ("query", "q", "keyword", "keywords", "search_query")
    SEARCH_NETWORK_RETRY_ATTEMPTS = 2
    DEFAULT_PROVIDER_TO_TOOL = {
        "tavily": "web_search_tavily",
        "bocha": "web_search_bocha",
        "brave": "web_search_brave",
        "firecrawl": "web_search_firecrawl",
        "baidu": "web_search_baidu",
        "baidu_ai_search": "web_search_baidu",
        "exa": "web_search_exa",
    }

    def __init__(self, context, config: dict, llm_func, tool_adapter):
        self.context = context
        self.config = config
        self.call_llm = llm_func
        self.tool_adapter = tool_adapter
        self._cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
        self._cache_lock = asyncio.Lock()

    def update_config(self, config: dict) -> None:
        self.config = config
        self._cache.clear()

    def _default_search_tool_names(self) -> list[str]:
        """Use AstrBot's active web-search provider, without plugin-specific settings."""
        provider_settings = {}
        try:
            get_config = getattr(self.context, "get_config", None)
            runtime_config = get_config() if callable(get_config) else None
            if hasattr(runtime_config, "get"):
                provider_settings = runtime_config.get("provider_settings", {}) or {}
        except Exception as exc:
            logger.debug(f"[每日分享/天气] 读取 AstrBot 搜索配置失败：{exc}")

        provider = str(provider_settings.get("websearch_provider", "") or "").lower()
        names = [self.DEFAULT_PROVIDER_TO_TOOL[provider]] if provider in self.DEFAULT_PROVIDER_TO_TOOL else []

        return names

    def _forecast_dates(self, timezone: str, now: datetime) -> str:
        local_date = now.astimezone(ZoneInfo(timezone)).date()
        dates = [local_date + timedelta(days=offset) for offset in range(4)]
        return "、".join(item.isoformat() for item in dates)

    def _build_current_query(self, location: str) -> str:
        return (
            f"查询 {location} 当前实时天气。只需要返回当前天气现象和当前气温，"
            "优先使用当地气象部门或可靠天气服务的最新资料，并保留来源链接。"
        )

    def _build_forecast_query(self, location: str, timezone: str, now: datetime) -> str:
        return (
            f"查询 {location} 在 {self._forecast_dates(timezone, now)} 这四个明确日期（今天和未来三天）的"
            "逐日天气预报。每个日期都需要天气现象、最高温和最低温。"
            "不要只给出笼统的“未来三天”摘要；优先使用当地气象部门或可靠天气服务的最新资料，并保留来源链接。"
        )

    def _build_repair_query(
        self,
        location: str,
        timezone: str,
        now: datetime,
        missing: str,
    ) -> str:
        return (
            f"补充查询 {location} 当前实时天气，以及 {self._forecast_dates(timezone, now)} 的逐日天气预报。"
            f"当前缺少：{missing[:500]}。"
            "只提供可核实的数据：当前天气现象和气温，以及每个日期的天气现象、最高温、最低温，并保留来源链接。"
        )

    async def _invoke_search_tool(self, tool_name: str, query: str, target_umo: str) -> str:
        tool = self.tool_adapter._find_llm_tool(tool_name)
        if not tool:
            raise RuntimeError(f"未找到搜索工具 {tool_name}")
        kwargs = self.tool_adapter._build_recorded_tool_kwargs(
            tool,
            {},
            [(self.SEARCH_ARG_NAMES, query)],
        )
        if kwargs is None:
            raise RuntimeError(f"搜索工具 {tool_name} 缺少必需参数")
        timeout = max(5, min(int(self.config.get("search_timeout_seconds", 60) or 60), 300))
        for attempt in range(1, self.SEARCH_NETWORK_RETRY_ATTEMPTS + 1):
            try:
                result, event = await asyncio.wait_for(
                    self.tool_adapter._execute_recorded_llm_tool(
                        tool,
                        kwargs,
                        target_umo,
                        query,
                    ),
                    timeout=timeout,
                )
                break
            except Exception as exc:
                retryable = isinstance(exc, asyncio.TimeoutError) or any(
                    marker in str(exc).lower()
                    for marker in ("timeout", "timed out", "connection", "temporarily unavailable")
                )
                if not retryable or attempt >= self.SEARCH_NETWORK_RETRY_ATTEMPTS:
                    raise
                logger.warning(
                    f"[每日分享/天气] AstrBot 搜索工具 {tool_name} 网络异常，"
                    f"{attempt}/{self.SEARCH_NETWORK_RETRY_ATTEMPTS} 次尝试失败，正在重试：{exc}"
                )
                await asyncio.sleep(attempt)
        text = self._stringify_result(result)
        if not text:
            text = self._stringify_result(getattr(event, "sent_messages", None))
        if not text:
            event_result = getattr(event, "get_result", lambda: None)()
            text = self._stringify_result(event_result)
        if not text:
            raise RuntimeError(f"搜索工具 {tool_name} 返回空结果")
        if re.match(r"^\s*(?:错误|失败|error|failed)\s*[：:]", text, flags=re.I):
            raise RuntimeError(f"搜索工具 {tool_name} 返回错误：{text[:300]}")
        return text

    async def _search_with_astrbot_default(self, query: str, target_umo: str) -> tuple[str, str]:
        tool_names = self._default_search_tool_names()
        if not tool_names:
            raise RuntimeError("AstrBot 未启用网页搜索工具")
        errors = []
        for tool_name in tool_names:
            try:
                return await self._invoke_search_tool(tool_name, query, target_umo), tool_name
            except Exception as exc:
                errors.append(f"{tool_name}: {exc}")
                logger.warning(f"[每日分享/天气] AstrBot 搜索工具 {tool_name} 不可用：{exc}")
        raise RuntimeError(
            "AstrBot 网页搜索失败：" + "；".join(errors)
            + "。请检查 AstrBot 当前默认网页搜索提供商的网络连接、API Key 和配额。"
        )

    def _stringify_result(self, value: Any, _seen: set[int] | None = None) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", errors="replace").strip()
        if isinstance(value, (int, float, bool)):
            return str(value)
        seen = _seen if _seen is not None else set()
        marker = id(value)
        if marker in seen:
            return ""
        seen.add(marker)
        if isinstance(value, dict):
            try:
                return json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                return "\n".join(filter(None, (self._stringify_result(v, seen) for v in value.values())))
        if isinstance(value, (list, tuple, set)):
            return "\n".join(filter(None, (self._stringify_result(item, seen) for item in value)))
        for attr in ("completion_text", "text", "message_str", "content", "chain", "result"):
            nested = getattr(value, attr, None)
            if nested is not None and nested is not value:
                text = self._stringify_result(nested, seen)
                if text:
                    return text
        return str(value).strip()

    def _normalization_prompt(
        self,
        *,
        raw: str,
        location: str,
        timezone: str,
        now: datetime,
    ) -> str:
        local_now = now.astimezone(ZoneInfo(timezone)).isoformat()
        return f"""把下面的联网搜索结果整理为严格 JSON。只输出 JSON，不要 Markdown，不要解释。
只有搜索结果明确对应用户请求的地点“{location}”时，location 才填写该地点；否则输出 error。时区使用“{timezone}”；当前检索时间为 {local_now}。
issued_at 填资料自身的更新时间；若资料没有具体更新时间，可省略该字段，系统会使用本次检索时间。
daily 必须恰好是当地今天起连续四天。所有温度为摄氏度。
只有地点、当前天气现象、当前气温、四个日期的天气现象/最高温/最低温或来源缺失时才输出 {{"error":"说明缺失项"}}，禁止猜测或补造这些基础天气数据。体感温度、湿度和风力为可选展示字段，缺失时可省略。
sources 必须保留至少一个来源名称或 URL。

JSON 结构：
{{
  "location": "{location}",
  "timezone": "{timezone}",
  "issued_at": "ISO 8601（可省略）",
  "current": {{"condition":"", "temperature_c":0, "feels_like_c":0, "humidity_pct":0, "wind":""}},
  "daily": [{{"date":"YYYY-MM-DD", "condition":"", "low_c":0, "high_c":0}}],
  "alerts": [],
  "sources": []
}}

搜索结果：
{raw[:24000]}"""

    @staticmethod
    def extract_json(text: str) -> dict:
        raw = str(text or "").strip()
        if not raw:
            raise ValueError("模型未返回天气 JSON")
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.I | re.S)
        candidates = [fenced.group(1)] if fenced else []
        candidates.append(raw)
        decoder = json.JSONDecoder()
        for candidate in candidates:
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
            for match in re.finditer(r"\{", candidate):
                try:
                    value, _ = decoder.raw_decode(candidate[match.start():])
                except Exception:
                    continue
                if isinstance(value, dict):
                    return value
        raise ValueError("无法从模型输出中解析天气 JSON")

    async def _normalize(
        self,
        raw: str,
        *,
        location: str,
        timezone: str,
        target_umo: str,
        now: datetime,
    ) -> dict:
        response = await self.call_llm(
            self._normalization_prompt(
                raw=raw,
                location=location,
                timezone=timezone,
                now=now,
            ),
            system_prompt="你是严谨的天气数据整理器，只能依据搜索结果输出 JSON。",
            timeout=int(self.config.get("normalize_timeout_seconds", 90) or 90),
            max_retries=1,
            umo=target_umo or None,
        )
        parsed = self.extract_json(response)
        if parsed.get("error"):
            raise ValueError(str(parsed.get("error")))
        return validate_weather_data(
            parsed,
            location=location,
            timezone=timezone,
            now=now,
        )

    @staticmethod
    def _result_excerpt(value: str, limit: int = 700) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    async def get_weather(
        self,
        location: str,
        timezone: str,
        *,
        target_umo: str = "",
        now: datetime | None = None,
    ) -> dict:
        tz = ZoneInfo(timezone)
        local_now = (now or datetime.now(tz)).astimezone(tz)
        key = (location.strip().lower(), timezone, local_now.date().isoformat())
        ttl = max(1, int(self.config.get("cache_minutes", 30) or 30)) * 60
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < ttl:
                return cached[1]

        current_raw, current_tool = await self._search_with_astrbot_default(
            self._build_current_query(location),
            target_umo,
        )
        forecast_raw, forecast_tool = await self._search_with_astrbot_default(
            self._build_forecast_query(location, timezone, local_now),
            target_umo,
        )
        raw = f"实时天气搜索结果：\n{current_raw}\n\n逐日预报搜索结果：\n{forecast_raw}"
        try:
            data = await self._normalize(
                raw,
                location=location,
                timezone=timezone,
                target_umo=target_umo,
                now=local_now,
            )
            provider_tools = [current_tool, forecast_tool]
        except (ValueError, KeyError) as first_error:
            logger.info(
                f"[每日分享/天气] {location} 首次搜索信息不完整，使用 AstrBot 默认搜索补查：{first_error}"
            )
            supplement_query = self._build_repair_query(
                location,
                timezone,
                local_now,
                str(first_error),
            )
            supplement, supplement_tool = await self._search_with_astrbot_default(
                supplement_query,
                target_umo,
            )
            try:
                data = await self._normalize(
                    f"首次搜索结果：\n{raw}\n\n补充搜索结果：\n{supplement}",
                    location=location,
                    timezone=timezone,
                    target_umo=target_umo,
                    now=local_now,
                )
                provider_tools = [current_tool, forecast_tool, supplement_tool]
            except Exception as final_error:
                logger.warning(
                    f"[每日分享/天气] {location} 补查后仍缺少核心天气数据：{final_error}；"
                    f"实时摘要={self._result_excerpt(current_raw)!r}；"
                    f"预报摘要={self._result_excerpt(forecast_raw)!r}；"
                    f"补查摘要={self._result_excerpt(supplement)!r}"
                )
                raise
        data["provider"] = "AstrBot · " + " / ".join(dict.fromkeys(provider_tools))
        async with self._cache_lock:
            self._cache[key] = (time.monotonic(), data)
        return data
