import asyncio
import base64
from datetime import datetime
from io import BytesIO
import json
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from PIL import Image, ImageStat


if "astrbot.api" not in sys.modules:
    class _Logger:
        def debug(self, *args, **kwargs): pass
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.__path__ = []
    astrbot_api.logger = _Logger()
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = astrbot_api

from core.weather import (
    WeatherRenderer,
    WeatherService,
    parse_weather_rules,
    validate_weather_data,
)


NOW = datetime(2026, 7, 28, 7, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))


def sample_weather(location="香港沙田"):
    return {
        "location": location,
        "timezone": "Asia/Hong_Kong",
        "issued_at": "2026-07-28T06:55:00+08:00",
        "current": {
            "condition": "多云有阵雨",
            "temperature_c": 29,
            "feels_like_c": 34,
            "humidity_pct": 78,
            "wind": "东南风 3级",
        },
        "daily": [
            {"date": "2026-07-28", "condition": "多云有阵雨", "low_c": 27, "high_c": 32, "precipitation_probability_pct": 60},
            {"date": "2026-07-29", "condition": "雷阵雨", "low_c": 26, "high_c": 31, "precipitation_probability_pct": 75},
            {"date": "2026-07-30", "condition": "多云", "low_c": 27, "high_c": 33, "precipitation_probability_pct": 35},
            {"date": "2026-07-31", "condition": "晴间多云", "low_c": 28, "high_c": 34, "precipitation_probability_pct": 20},
        ],
        "alerts": ["雷暴警告"],
        "sources": ["https://www.hko.gov.hk/"],
    }


class WeatherModelTests(unittest.TestCase):
    def test_parse_rules(self):
        rules = parse_weather_rules(
            "123456 | 香港沙田 | 0 7 * * * | Asia/Hong_Kong\n"
            "# disabled example"
        )
        self.assertEqual(1, len(rules))
        self.assertEqual("香港沙田", rules[0].location)
        self.assertEqual("Asia/Hong_Kong", rules[0].timezone)

    def test_parse_rules_strict_rejects_invalid_timezone(self):
        with self.assertRaises(ValueError):
            parse_weather_rules("1 | 北京 | 0 8 * * * | Mars/Base", strict=True)

    def test_parse_rules_strict_rejects_invalid_cron_field(self):
        with self.assertRaises(ValueError):
            parse_weather_rules("1 | 北京 | 99 8 * * * | Asia/Shanghai", strict=True)

    def test_validate_weather(self):
        value = validate_weather_data(
            sample_weather(),
            location="香港沙田区",
            timezone="Asia/Hong_Kong",
            now=NOW,
        )
        self.assertEqual(4, len(value["daily"]))

    def test_validate_rejects_date_gap_and_bad_temperature(self):
        value = sample_weather()
        value["daily"][2]["date"] = "2026-08-01"
        with self.assertRaisesRegex(ValueError, "连续四天"):
            validate_weather_data(value, location="香港沙田", timezone="Asia/Hong_Kong", now=NOW)
        value = sample_weather()
        value["daily"][0]["low_c"] = 40
        with self.assertRaisesRegex(ValueError, "最高温"):
            validate_weather_data(value, location="香港沙田", timezone="Asia/Hong_Kong", now=NOW)

        value = sample_weather()
        value["current"]["temperature_c"] = math.nan
        with self.assertRaisesRegex(ValueError, "合理范围"):
            validate_weather_data(value, location="香港沙田", timezone="Asia/Hong_Kong", now=NOW)

    def test_validate_allows_missing_display_details(self):
        value = sample_weather()
        value.pop("issued_at")
        value["current"].pop("feels_like_c")
        value["current"].pop("humidity_pct")
        value["current"].pop("wind")
        for item in value["daily"]:
            item.pop("precipitation_probability_pct")

        normalized = validate_weather_data(
            value,
            location="香港沙田",
            timezone="Asia/Hong_Kong",
            now=NOW,
        )
        self.assertEqual(NOW.isoformat(), normalized["issued_at"])
        self.assertEqual(29, normalized["current"]["feels_like_c"])
        self.assertIsNone(normalized["current"]["humidity_pct"])
        self.assertEqual("风力未提供", normalized["current"]["wind"])

    def test_extract_json_from_fenced_output(self):
        parsed = WeatherService.extract_json("说明\n```json\n{\"location\": \"香港\"}\n```")
        self.assertEqual("香港", parsed["location"])


class WeatherRendererTests(unittest.TestCase):
    def test_render_default_card(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = WeatherRenderer(Path(directory), {})
            path = renderer.render(validate_weather_data(
                sample_weather("香港特别行政区沙田新市镇"),
                location="香港特别行政区沙田新市镇",
                timezone="Asia/Hong_Kong",
                now=NOW,
            ))
            with Image.open(path) as image:
                self.assertEqual((1080, 1440), image.size)
                self.assertGreater(ImageStat.Stat(image.convert("L")).var[0], 100)


class WeatherTemplateUploadTests(unittest.TestCase):
    def test_upload_normalizes_template_and_updates_config(self):
        from core.dashboard.routes import DashboardRoutesMixin

        class Renderer:
            def __init__(self):
                self.config = None

            def update_config(self, config):
                self.config = config

        class FakeRoutes(DashboardRoutesMixin):
            def __init__(self, directory, body):
                self.data_dir = Path(directory)
                self.config = {}
                self.weather_conf = {}
                self.weather_renderer = Renderer()
                self.body = body
                self.saved = False

            async def _page_json_body(self):
                return self.body

            async def _page_json(self, callback, headers=None):
                return await callback()

            async def _save_config_file(self):
                self.saved = True

        source = Image.new("RGB", (1200, 800), (98, 172, 204))
        buffer = BytesIO()
        source.save(buffer, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            routes = FakeRoutes(directory, {"data_url": data_url})
            response = asyncio.run(routes.page_weather_template_upload())
            template_path = Path(response["data"]["template_path"])
            self.assertTrue(template_path.is_file())
            self.assertTrue(routes.saved)
            self.assertEqual(str(template_path), routes.config["weather_conf"]["template_path"])
            self.assertIs(routes.config["weather_conf"], routes.weather_renderer.config)
            with Image.open(template_path) as image:
                self.assertEqual((1080, 1440), image.size)


class _FakeAdapter:
    def __init__(self, tools):
        self.tools = tools

    def _find_llm_tool(self, name):
        return self.tools.get(name)

    def _build_recorded_tool_kwargs(self, tool, saved, values):
        return {"query": values[0][1]}

    async def _execute_recorded_llm_tool(self, tool, kwargs, target, message):
        return await tool.run(**kwargs), SimpleNamespace(sent_messages=[])


class _FakeTool:
    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0
        self.queries = []

    async def run(self, **kwargs):
        self.calls += 1
        self.queries.append(kwargs.get("query", ""))
        if self.error:
            raise self.error
        return self.result


class WeatherServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_fallback_and_cache(self):
        first = _FakeTool("web_search_tavily", error=RuntimeError("offline"))
        second = _FakeTool("web_search_brave", result="fresh weather with sources")
        adapter = _FakeAdapter({first.name: first, second.name: second})
        context = SimpleNamespace(
            get_config=lambda: {"provider_settings": {"websearch_provider": "tavily"}},
            get_llm_tool_manager=lambda: SimpleNamespace(func_list=[first, second]),
        )

        async def llm(prompt, **kwargs):
            return json.dumps(sample_weather(), ensure_ascii=False)

        service = WeatherService(
            context,
            {},
            llm,
            adapter,
        )
        result = await service.get_weather("香港沙田", "Asia/Hong_Kong", now=NOW)
        cached = await service.get_weather("香港沙田", "Asia/Hong_Kong", now=NOW)
        self.assertEqual(f"AstrBot · {second.name}", result["provider"])
        self.assertIs(result, cached)
        self.assertEqual(1, first.calls)
        self.assertEqual(1, second.calls)

    async def test_requeries_when_first_result_is_incomplete(self):
        tool = _FakeTool("web_search_tavily", result="weather search result")
        adapter = _FakeAdapter({tool.name: tool})
        context = SimpleNamespace(
            get_config=lambda: {"provider_settings": {"websearch_provider": "tavily"}},
            get_llm_tool_manager=lambda: SimpleNamespace(func_list=[tool]),
        )
        responses = iter([
            json.dumps({"error": "缺少未来预报和体感温度"}, ensure_ascii=False),
            json.dumps(sample_weather(), ensure_ascii=False),
        ])

        async def llm(prompt, **kwargs):
            return next(responses)

        service = WeatherService(context, {}, llm, adapter)
        result = await service.get_weather("香港沙田", "Asia/Hong_Kong", now=NOW)

        self.assertEqual("AstrBot · web_search_tavily", result["provider"])
        self.assertEqual(2, tool.calls)
        self.assertIn("2026-07-28、2026-07-29、2026-07-30、2026-07-31", tool.queries[0])
        self.assertIn("缺少未来预报和体感温度", tool.queries[1])

    async def test_accepts_core_forecast_without_optional_details(self):
        tool = _FakeTool("web_search_tavily", result="weather search result")
        adapter = _FakeAdapter({tool.name: tool})
        context = SimpleNamespace(
            get_config=lambda: {"provider_settings": {"websearch_provider": "tavily"}},
            get_llm_tool_manager=lambda: SimpleNamespace(func_list=[tool]),
        )
        value = sample_weather()
        value.pop("issued_at")
        for key in ("feels_like_c", "humidity_pct", "wind"):
            value["current"].pop(key)
        for item in value["daily"]:
            item.pop("precipitation_probability_pct")

        async def llm(prompt, **kwargs):
            return json.dumps(value, ensure_ascii=False)

        service = WeatherService(context, {}, llm, adapter)
        result = await service.get_weather("香港沙田", "Asia/Hong_Kong", now=NOW)

        self.assertEqual(1, tool.calls)
        self.assertEqual(29, result["current"]["feels_like_c"])
        self.assertNotIn("降水概率", tool.queries[0])


class WeatherSchedulerTests(unittest.TestCase):
    def test_weather_jobs_are_independent_stable_and_timezone_aware(self):
        from tests.testfailure import _Plugin, _load_tasks_module

        class Scheduler:
            def __init__(self):
                self.jobs = []

            def add_job(self, func, trigger, **kwargs):
                self.jobs.append(SimpleNamespace(func=func, trigger=trigger, **kwargs))

            def get_jobs(self):
                return list(self.jobs)

            def remove_job(self, job_id):
                self.jobs = [job for job in self.jobs if job.id != job_id]

        mod = _load_tasks_module()
        plugin = _Plugin()
        plugin.scheduler = Scheduler()
        plugin.config["enable_auto_sharing"] = False
        plugin.weather_conf = {
            "enabled": True,
            "rules": (
                "1001 | 香港沙田 | 0 7 * * * | Asia/Hong_Kong\n"
                "1002 | 北京朝阳 | 30 8 * * * | Asia/Shanghai"
            ),
        }
        manager = mod.TaskManager(plugin)
        manager.setup_cleanup_tasks = lambda: None
        manager.setup_cron = lambda cron: self.fail("normal sharing must remain disabled")

        manager.setup_tasks()
        first_ids = [job.id for job in plugin.scheduler.jobs]
        self.assertEqual(2, len(first_ids))
        self.assertEqual(2, len(set(first_ids)))
        self.assertTrue(all(job.id.startswith("weather_") for job in plugin.scheduler.jobs))
        self.assertEqual(
            {"Asia/Hong_Kong", "Asia/Shanghai"},
            {str(job.timezone) for job in plugin.scheduler.jobs},
        )
        self.assertEqual({"天气 · 香港沙田", "天气 · 北京朝阳"}, {job.name for job in plugin.scheduler.jobs})

        manager.setup_weather_crons()
        self.assertEqual(first_ids, [job.id for job in plugin.scheduler.jobs])


if __name__ == "__main__":
    unittest.main()
