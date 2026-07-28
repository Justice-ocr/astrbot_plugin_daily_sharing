from __future__ import annotations

import asyncio
from datetime import datetime
import json
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .models import validate_weather_data


class WeatherService:
    SEARCH_ARG_NAMES = ("query", "q", "keyword", "keywords", "search_query")
    TOOL_ALIASES = {
        "grok": "grok_web_search",
        "anysearch": "anysearch_search",
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

    def _provider_names(self) -> list[str]:
        provider = str(self.config.get("provider", "auto") or "auto").strip().lower()
        if provider != "auto":
            raw = [provider]
        else:
            raw = self.config.get(
                "provider_order",
                ["grok", "astrbot", "anysearch"],
            )
            if isinstance(raw, str):
                raw = [item.strip() for item in re.split(r"[,，\n]+", raw) if item.strip()]
        result = []
        for item in raw or []:
            name = str(item or "").strip()
            if not name:
                continue
            if name.lower() == "astrbot":
                name = str(self.config.get("astrbot_tool_name", "web_search_tavily") or "").strip()
            else:
                name = self.TOOL_ALIASES.get(name.lower(), name)
            if name and name not in result:
                result.append(name)
        return result

    def _build_query(self, location: str, timezone: str, now: datetime) -> str:
        today = now.astimezone(ZoneInfo(timezone)).date().isoformat()
        return (
            f"查询 {location} 在 {today} 的实时天气，以及从 {today} 起连续四天（今天和未来三天）的"
            "天气、最高最低温、降水概率；同时提供体感温度、湿度、风向风力、天气预警和来源链接。"
            "优先使用当地气象部门或可靠天气服务的最新资料。"
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
        result, event = await asyncio.wait_for(
            self.tool_adapter._execute_recorded_llm_tool(
                tool,
                kwargs,
                target_umo,
                query,
            ),
            timeout=timeout,
        )
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
issued_at 填资料自身的更新时间；若资料仅说明为实时结果但没有具体更新时间，可填当前检索时间。
daily 必须恰好是当地今天起连续四天。所有温度为摄氏度，概率为 0 到 100 的数字。
缺少任何必填信息时输出 {{"error":"说明缺失项"}}，禁止猜测或补造天气数据。
sources 必须保留至少一个来源名称或 URL。

JSON 结构：
{{
  "location": "{location}",
  "timezone": "{timezone}",
  "issued_at": "ISO 8601",
  "current": {{"condition":"", "temperature_c":0, "feels_like_c":0, "humidity_pct":0, "wind":""}},
  "daily": [{{"date":"YYYY-MM-DD", "condition":"", "low_c":0, "high_c":0, "precipitation_probability_pct":0}}],
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

        errors = []
        query = self._build_query(location, timezone, local_now)
        for tool_name in self._provider_names():
            try:
                raw = await self._invoke_search_tool(tool_name, query, target_umo)
                data = await self._normalize(
                    raw,
                    location=location,
                    timezone=timezone,
                    target_umo=target_umo,
                    now=local_now,
                )
                data["provider"] = tool_name
                async with self._cache_lock:
                    self._cache[key] = (time.monotonic(), data)
                return data
            except Exception as exc:
                errors.append(f"{tool_name}: {exc}")
                logger.warning(f"[每日分享/天气] 搜索源 {tool_name} 不可用或数据无效：{exc}")
        if not self._provider_names():
            errors.append("未配置搜索工具")
        raise RuntimeError("所有天气搜索源均失败：" + "；".join(errors))
