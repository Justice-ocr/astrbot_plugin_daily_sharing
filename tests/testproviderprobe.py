import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_provider_probe_module():
    module_name = "daily_sharing_provider_probe_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "core" / "dashboard" / "provider_probe.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class DashboardProviderProbeTests(unittest.TestCase):
    def test_probe_tool_blacklist_blocks_message_and_own_tools(self):
        module = _load_provider_probe_module()
        probe = module.DashboardProviderProbeMixin()

        for name in (
            "send_message_to_user",
            "daily_share",
            "news_link",
            "custom_send_message_image",
        ):
            with self.subTest(name=name):
                self.assertFalse(probe._page_probe_tool_allowed_text(name, "image tool"))

    def test_probe_tool_blacklist_blocks_dangerous_descriptions(self):
        module = _load_provider_probe_module()
        probe = module.DashboardProviderProbeMixin()

        self.assertFalse(
            probe._page_probe_tool_allowed_text(
                "image_sender",
                "Can send message to the user after rendering an image.",
            )
        )

    def test_probe_tool_allows_normal_media_tool(self):
        module = _load_provider_probe_module()
        probe = module.DashboardProviderProbeMixin()
        tool = types.SimpleNamespace(
            name="aiimg_generate",
            description="Generate an image from a prompt.",
        )

        self.assertTrue(probe._page_probe_tool_allowed(tool))


if __name__ == "__main__":
    unittest.main()
