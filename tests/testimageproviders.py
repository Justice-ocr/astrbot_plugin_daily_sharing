import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def debug(self, *args, **kwargs):
        return None

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


def _install_astrbot_stub():
    for name in list(sys.modules):
        if name.startswith("astrbot"):
            sys.modules.pop(name, None)

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.__path__ = []
    astrbot_api.logger = _Logger()

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = astrbot_api


def _load_providers_module():
    _install_astrbot_stub()
    module_name = "daily_sharing_image_providers_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "core" / "image" / "providers.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _Star:
    def __init__(self, name, star_cls):
        self.name = name
        self.id = name
        self.module_name = name
        self.star_cls = star_cls


class _Context:
    def __init__(self, stars):
        self._stars = stars

    def get_all_stars(self):
        return self._stars


class _ToolManager:
    def __init__(self, tools):
        self.func_list = tools

    def get_func(self, name):
        for tool in reversed(self.func_list):
            if tool.name == name:
                return tool
        return None


class _ToolContext(_Context):
    def __init__(self, stars, tools):
        super().__init__(stars)
        self._tool_manager = _ToolManager(tools)
        self.sent_messages = []

    def get_llm_tool_manager(self):
        return self._tool_manager

    async def send_message(self, target_umo, chain):
        self.sent_messages.append((target_umo, chain))


class _LlmTool:
    active = True

    def __init__(self, name, parameters, handler):
        self.name = name
        self.parameters = parameters
        self.description = name
        self.handler = handler


class ImageProviderManagerTests(unittest.TestCase):
    def test_manual_generic_provider_resolves_method_and_extra_args(self):
        providers = _load_providers_module()
        calls = []

        class DrawService:
            def generate(self, prompt, size):
                calls.append((prompt, size))
                return types.SimpleNamespace(output=["/tmp/manual.png"])

        class Plugin:
            draw = DrawService()

        manager = providers.ImageProviderManager(
            _Context([_Star("custom_image_plugin", Plugin())]),
            {
                "image_provider": "generic_plugin",
                "generic_image_plugin_name": "custom_image",
                "generic_image_method_path": "draw.generate",
                "generic_image_extra_args": json.dumps({"size": "1024x1024"}),
            },
        )

        result = asyncio.run(manager.generate_with_generic_plugin("manual prompt"))

        self.assertEqual(result, "/tmp/manual.png")
        self.assertEqual(calls, [("manual prompt", "1024x1024")])

    def test_generic_image_edit_uses_plugin_reference_images(self):
        providers = _load_providers_module()
        calls = []

        class EditService:
            async def edit(self, prompt, images):
                calls.append((prompt, images))
                return {"image_path": "/tmp/selfie.png"}

        class Plugin:
            edit = EditService()

            def _get_config_selfie_reference_paths(self):
                return ["/tmp/ref.png"]

            async def _read_paths_bytes(self, paths):
                return [f"bytes:{path}".encode() for path in paths]

        manager = providers.ImageProviderManager(
            _Context([_Star("custom_image_plugin", Plugin())]),
            {
                "image_provider": "generic_plugin",
                "generic_image_plugin_name": "custom_image",
                "generic_image_method_path": "draw.generate",
                "generic_image_edit_method_path": "edit.edit",
            },
        )

        result = asyncio.run(manager.generate_with_generic_plugin("selfie prompt", use_ref_selfie=True))

        self.assertEqual(result, "/tmp/selfie.png")
        self.assertEqual(calls, [("selfie prompt", [b"bytes:/tmp/ref.png"])])

    def test_image_edit_uses_active_persona_reference_images(self):
        providers = _load_providers_module()
        calls = []

        class EditService:
            async def edit(self, prompt, images):
                calls.append((prompt, images))
                return {"image_path": "/tmp/persona-selfie.png"}

        class PersonaManager:
            def get_active_ref_paths(self):
                return ["/tmp/persona-ref.png"]

        class Plugin:
            edit = EditService()
            persona_mgr = PersonaManager()

            def _get_config_selfie_reference_paths(self):
                return ["/tmp/webui-ref.png"]

            async def _read_paths_bytes(self, paths):
                return [f"bytes:{path}".encode() for path in paths]

        manager = providers.ImageProviderManager(
            _Context([_Star("astrbot_plugin_aiimg_enhanced", Plugin())]),
            {
                "image_provider": "generic_plugin",
                "generic_image_plugin_name": "astrbot_plugin_aiimg_enhanced",
                "generic_image_method_path": "draw.generate",
                "generic_image_edit_method_path": "edit.edit",
            },
        )

        result = asyncio.run(manager.generate_with_generic_plugin("selfie prompt", use_ref_selfie=True))

        self.assertEqual(result, "/tmp/persona-selfie.png")
        self.assertEqual(calls, [("selfie prompt", [b"bytes:/tmp/persona-ref.png"])])

    def test_calibrated_provider_self_delivers_recorded_llm_image_tool(self):
        providers = _load_providers_module()
        calls = []

        async def recorded_tool(event, prompt, mode):
            calls.append(("recorded", prompt, mode, event.unified_msg_origin))
            await event.send(types.SimpleNamespace(chain=[types.SimpleNamespace(path="/tmp/recorded.png")]))
            return None

        class Plugin:
            def draw_image(self, prompt):
                calls.append(("fallback", prompt))
                return "/tmp/fallback.png"

        context = _ToolContext(
                [_Star("plugin_draw_image", Plugin())],
                [
                    _LlmTool(
                        "draw_calibrated",
                        {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string"},
                                "mode": {"type": "string"},
                            },
                            "required": ["prompt"],
                        },
                        recorded_tool,
                    )
                ],
            )
        manager = providers.ImageProviderManager(
            context,
            {
                "image_provider": "calibrated_tool",
                "llm_image_tool_name": "draw_calibrated",
                "llm_image_tool_args": {"prompt": "probe prompt", "mode": "probe"},
            },
        )

        result = asyncio.run(manager.generate_with_calibrated_tool("real prompt", target_umo="aiocqhttp:FriendMessage:123"))

        self.assertIsNone(result)
        self.assertEqual(calls, [("recorded", "real prompt", "text", "aiocqhttp:FriendMessage:123")])
        self.assertEqual(len(context.sent_messages), 1)
        self.assertEqual(context.sent_messages[0][0], "aiocqhttp:FriendMessage:123")
        self.assertTrue(manager.get_last_external_delivery("image")["sent"])

    def test_calibrated_tts_self_delivers_recorded_llm_tool(self):
        providers = _load_providers_module()
        calls = []

        async def recorded_tool(event, text, emotion):
            calls.append((text, emotion, event.unified_msg_origin))
            await event.send(types.SimpleNamespace(chain=[types.SimpleNamespace(file="/tmp/recorded.mp3")]))
            return None

        context = _ToolContext(
                [],
                [
                    _LlmTool(
                        "voice_calibrated",
                        {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "emotion": {"type": "string"},
                            },
                        },
                        recorded_tool,
                    )
                ],
            )
        manager = providers.ImageProviderManager(
            context,
            {
                "tts_provider": "calibrated_tool",
                "llm_tts_tool_name": "voice_calibrated",
                "llm_tts_tool_args": {"text": "probe text", "emotion": "neutral"},
            },
        )

        result = asyncio.run(
            manager.generate_tts_with_calibrated_tool(
                "正式语音",
                emotion="happy",
                target_umo="aiocqhttp:FriendMessage:456",
            )
        )

        self.assertIsNone(result)
        self.assertEqual(calls, [("正式语音", "happy", "aiocqhttp:FriendMessage:456")])
        self.assertEqual(len(context.sent_messages), 1)
        self.assertTrue(manager.get_last_external_delivery("audio")["sent"])

    def test_calibrated_tts_rejects_plain_text_result(self):
        providers = _load_providers_module()

        def recorded_tool(event, text):
            return text

        manager = providers.ImageProviderManager(
            _ToolContext(
                [],
                [
                    _LlmTool(
                        "voice_text_only",
                        {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                            },
                        },
                        recorded_tool,
                    )
                ],
            ),
            {
                "tts_provider": "calibrated_tool",
                "llm_tts_tool_name": "voice_text_only",
                "llm_tts_tool_args": {"text": "probe text"},
            },
        )

        result = asyncio.run(manager.generate_tts_with_calibrated_tool("正式语音"))

        self.assertIsNone(result)
        self.assertFalse(manager.get_last_external_delivery("audio")["sent"])

    def test_calibrated_image_does_not_deliver_event_messages_without_send(self):
        providers = _load_providers_module()

        class ImageComponent:
            path = "/tmp/event-image.png"
            file = ""
            url = ""

        def recorded_tool(event, prompt):
            event.sent_messages.append(types.SimpleNamespace(chain=[ImageComponent()]))
            return None

        manager = providers.ImageProviderManager(
            _ToolContext(
                [],
                [
                    _LlmTool(
                        "draw_event_image",
                        {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string"},
                            },
                        },
                        recorded_tool,
                    )
                ],
            ),
            {
                "image_provider": "calibrated_tool",
                "llm_image_tool_name": "draw_event_image",
                "llm_image_tool_args": {"prompt": "probe prompt"},
            },
        )

        result = asyncio.run(manager.generate_with_calibrated_tool("real prompt"))

        self.assertIsNone(result)
        self.assertFalse(manager.get_last_external_delivery("image")["sent"])

    def test_generic_selfie_mode_without_edit_method_does_not_fallback_to_draw(self):
        providers = _load_providers_module()
        calls = []

        class DrawService:
            def generate(self, prompt):
                calls.append(("draw.generate", prompt))
                return "/tmp/draw.png"

        class Plugin:
            draw = DrawService()

        manager = providers.ImageProviderManager(
            _Context([_Star("custom_image_plugin", Plugin())]),
            {
                "image_provider": "generic_plugin",
                "generic_image_plugin_name": "custom_image",
                "generic_image_method_path": "draw.generate",
            },
        )

        result = asyncio.run(manager.generate_with_generic_plugin("selfie prompt", use_ref_selfie=True))

        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_generic_tts_passes_text_and_emotion(self):
        providers = _load_providers_module()
        calls = []

        class Plugin:
            def text_to_speech(self, text, emotion):
                calls.append((text, emotion))
                return types.SimpleNamespace(audio_path="/tmp/voice.mp3")

        manager = providers.ImageProviderManager(
            _Context([_Star("plugin_voice_tools", Plugin())]),
            {
                "tts_provider": "generic_plugin",
                "generic_tts_plugin_name": "voice_tools",
                "generic_tts_method_path": "text_to_speech",
            },
        )

        result = asyncio.run(
            manager.generate_tts_with_generic_plugin("hello", emotion="happy", target_umo="session-1")
        )

        self.assertEqual(result, "/tmp/voice.mp3")
        self.assertEqual(calls, [("hello", "happy")])

    def test_removed_auto_provider_falls_back_to_generic_plugin(self):
        providers = _load_providers_module()

        manager = providers.ImageProviderManager(
            _Context([]),
            {"image_provider": "auto"},
        )

        self.assertEqual(manager.select_provider(), "generic_plugin")

    def test_removed_fixed_providers_fall_back_to_generic_plugin(self):
        providers = _load_providers_module()

        image_manager = providers.ImageProviderManager(
            _Context([]),
            {"image_provider": "gitee_aiimg", "video_provider": "gitee_aiimg"},
        )
        tts_manager = providers.ImageProviderManager(
            _Context([]),
            {"tts_provider": "emotion_router"},
        )

        self.assertEqual(image_manager.select_provider(), "generic_plugin")
        self.assertEqual(image_manager.select_video_provider(), "generic_plugin")
        self.assertEqual(tts_manager.select_tts_provider(), "generic_plugin")

    def test_unknown_providers_fall_back_to_generic_plugin(self):
        providers = _load_providers_module()
        manager = providers.ImageProviderManager(
            _Context([]),
            {
                "image_provider": "unknown_image",
                "video_provider": "unknown_video",
                "tts_provider": "unknown_tts",
            },
        )

        self.assertEqual(manager.select_provider(), "generic_plugin")
        self.assertEqual(manager.select_video_provider(), "generic_plugin")
        self.assertEqual(manager.select_tts_provider(), "generic_plugin")

    def test_calibrated_tool_blacklist_blocks_message_and_own_tools(self):
        providers = _load_providers_module()

        async def blocked_tool(event, prompt):
            await event.send(types.SimpleNamespace(chain=[types.SimpleNamespace(path="/tmp/blocked.png")]))

        for tool_name in ("send_message_to_user", "daily_share", "news_link"):
            with self.subTest(tool_name=tool_name):
                context = _ToolContext(
                    [],
                    [
                        _LlmTool(
                            tool_name,
                            {
                                "type": "object",
                                "properties": {"prompt": {"type": "string"}},
                            },
                            blocked_tool,
                        )
                    ],
                )
                manager = providers.ImageProviderManager(
                    context,
                    {
                        "image_provider": "calibrated_tool",
                        "llm_image_tool_name": tool_name,
                        "llm_image_tool_args": {"prompt": "probe prompt"},
                    },
                )

                result = asyncio.run(manager.generate_with_calibrated_tool("real prompt"))

                self.assertIsNone(result)
                self.assertEqual(context.sent_messages, [])
                self.assertFalse(manager.get_last_external_delivery("image")["sent"])

    def test_schema_exposes_generic_image_provider_options(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        image_items = schema["image_conf"]["items"]

        self.assertEqual(
            image_items["image_provider"]["options"],
            ["generic_plugin", "calibrated_tool"],
        )
        self.assertIn("generic_image_method_path", image_items)
        self.assertIn("generic_image_result_field", image_items)
        self.assertIn("generic_image_edit_method_path", image_items)
        self.assertIn("video_provider", image_items)

        tts_items = schema["tts_conf"]["items"]
        self.assertEqual(
            tts_items["tts_provider"]["options"],
            ["generic_plugin", "calibrated_tool"],
        )
        self.assertIn("generic_tts_method_path", tts_items)


if __name__ == "__main__":
    unittest.main()
