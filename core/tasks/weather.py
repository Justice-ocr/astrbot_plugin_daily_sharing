import asyncio

from astrbot.api import logger

from ..weather import WeatherRule, parse_weather_rules


class TaskWeatherMixin:
    """Weather retrieval, rendering, and image-only delivery."""

    def get_weather_rules(self, *, strict: bool = False) -> list[WeatherRule]:
        return parse_weather_rules(self.weather_conf.get("rules", ""), strict=strict)

    def _weather_target_umo(self, target: str) -> str:
        target = str(target or "").strip()
        if self._is_full_umo(target):
            return target
        return self._build_target_umo(
            target,
            False,
            self._get_default_adapter_id(warn_on_fallback=False),
        )

    async def execute_weather_rule(
        self,
        rule: WeatherRule,
        *,
        source_type: str = "scheduled",
        event=None,
    ) -> bool:
        if self.plugin._is_terminated:
            return False
        target_umo = (
            str(getattr(event, "unified_msg_origin", "") or "").strip()
            if event
            else self._weather_target_umo(rule.target)
        )
        if not target_umo:
            logger.error("[每日分享/天气] 无法确定发送目标")
            return False

        locks = getattr(self.plugin, "_weather_locks", None)
        if locks is None:
            locks = self.plugin._weather_locks = {}
        lock = locks.setdefault(target_umo, asyncio.Lock())
        if lock.locked():
            logger.warning(f"[每日分享/天气] 目标 {target_umo} 的上一项天气任务尚未完成，等待执行")
        async with lock:
            try:
                weather = await self.weather_service.get_weather(
                    rule.location,
                    rule.timezone,
                    target_umo=target_umo,
                )
                image_path = await asyncio.to_thread(self.weather_renderer.render, weather)
                prepared_path = await self._prepare_image_for_target(target_umo, image_path)
                media_result = {}
                await self._send_image_chain_with_retry(
                    target_umo,
                    prepared_path,
                    event=event,
                    media_result=media_result,
                )
                current = weather["current"]
                summary = (
                    f"{weather['location']}：{current['condition']}，"
                    f"{current['temperature_c']}°C；未来四天天气卡片"
                )
                await self.db.add_sent_history(
                    target_umo,
                    "weather",
                    summary,
                    True,
                    media_type="image",
                    media_path=str(image_path),
                    source_type=str(source_type or "scheduled"),
                )
                logger.info(f"[每日分享/天气] 已向 {target_umo} 发送 {rule.location} 天气卡片")
                return True
            except Exception as exc:
                logger.error(f"[每日分享/天气] {rule.location} 天气任务失败：{exc}")
                await self.db.add_sent_history(
                    target_umo,
                    "weather",
                    f"{rule.location} 天气播报",
                    False,
                    error_reason=str(exc),
                    source_type=str(source_type or "scheduled"),
                )
                if event:
                    try:
                        await event.send(event.plain_result(f"天气播报生成失败：{exc}"))
                    except Exception:
                        pass
                return False
    async def execute_weather_test(self, event, *, location: str = "", timezone: str = "") -> bool:
        rules = self.get_weather_rules()
        event_target = str(getattr(event, "unified_msg_origin", "") or "").strip()
        matching = next(
            (rule for rule in rules if self._weather_target_umo(rule.target) == event_target),
            None,
        )
        if matching and not location:
            rule = matching
        else:
            location = str(location or (matching.location if matching else "")).strip()
            timezone = str(timezone or (matching.timezone if matching else "Asia/Shanghai")).strip()
            if not location:
                raise ValueError("请先配置天气规则，或指定测试地点")
            rule = WeatherRule(event_target, location, "0 8 * * *", timezone)
        return await self.execute_weather_rule(rule, source_type="command", event=event)
