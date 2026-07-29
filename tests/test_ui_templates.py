import importlib.util
from pathlib import Path
import unittest


def load_templates_module():
    template_path = Path(__file__).resolve().parents[1] / "llmpivot" / "ui" / "templates.py"
    spec = importlib.util.spec_from_file_location("ui_templates", template_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UITemplateTests(unittest.TestCase):
    def test_prompt_list_uses_prompt_manager_branding(self):
        templates = load_templates_module()

        html = templates.prompt_list([], protected=False, base="")

        self.assertIn("Prompt Manager", html)
        self.assertIn("<title>Home - Prompt Manager</title>", html)
        self.assertNotIn("llmpivot", html.lower())
        self.assertNotIn("LLM Pivot", html)
        self.assertNotIn(">v1<", html)


if __name__ == "__main__":
    unittest.main()
