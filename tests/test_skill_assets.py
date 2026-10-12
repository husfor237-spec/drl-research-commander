"""Integrity checks for the skill's own files: JSON, frontmatter and links.

The skill is mostly documents, so a broken relative link or an unparsable
template silently degrades it. These checks need no external dependency and no
installed reader, so they always run.
"""

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)\s]+)\)")
SKIP_PREFIX = ("http://", "https://", "mailto:", "#", "<")
# Scripts owned by another skill and referenced only as a dependency. They must
# not exist in this repository; README and context-handoff.md document the
# sibling `context-gauge` skill that provides them.
EXTERNAL_SCRIPTS = {"context_gauge.py"}


def markdown_files():
    files = [ROOT / "SKILL.md", ROOT / "README.md"]
    files += sorted((ROOT / "references").glob("*.md"))
    files += sorted((ROOT / "assets").glob("*.md"))
    return [path for path in files if path.is_file()]


class SkillIntegrityTests(unittest.TestCase):
    def test_no_markdown_file_is_empty(self):
        for path in markdown_files():
            self.assertTrue(path.read_text(encoding="utf-8").strip(),
                            str(path) + " is empty")

    def test_relative_markdown_links_resolve(self):
        for path in markdown_files():
            text = path.read_text(encoding="utf-8")
            for target in LINK.findall(text):
                if target.startswith(SKIP_PREFIX):
                    continue
                cleaned = target.split("#", 1)[0]
                if not cleaned:
                    continue
                resolved = (path.parent / cleaned).resolve()
                self.assertTrue(resolved.exists(),
                                "%s links to a missing file: %s" % (path.name, target))

    def test_json_assets_parse(self):
        assets = sorted((ROOT / "assets").glob("*.json"))
        self.assertTrue(assets, "no JSON asset templates found")
        for path in assets:
            with self.subTest(asset=path.name):
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(value, dict)

    def test_skill_frontmatter_declares_name_and_version(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"), "SKILL.md must open with frontmatter")
        frontmatter = text.split("---\n", 2)[1]
        self.assertIn("name: drl-research-commander", frontmatter)
        self.assertIn("description:", frontmatter)
        version = re.search(r'version:\s*"(\d+\.\d+\.\d+)"', frontmatter)
        self.assertIsNotNone(version, "frontmatter needs a quoted semantic version")
        self.assertEqual(frontmatter.count("---"), 0, "frontmatter must not nest a delimiter")

    def test_interface_yaml_declares_required_keys(self):
        text = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        for key in ("display_name:", "short_description:", "default_prompt:",
                    "allow_implicit_invocation:"):
            self.assertIn(key, text)

    def test_scripts_referenced_by_the_documents_exist(self):
        for name in ("context_policy.py", "project_control.py", "record_decision.py"):
            self.assertTrue((ROOT / "scripts" / name).is_file(), name + " is missing")
        for document in markdown_files():
            for script in re.findall(r"([A-Za-z_]+\.py)", document.read_text(encoding="utf-8")):
                if script in EXTERNAL_SCRIPTS:
                    continue
                matches = list(ROOT.rglob(script))
                self.assertTrue(matches, "%s references a missing script: %s"
                                % (document.name, script))

    def test_external_script_is_not_vendored(self):
        for name in EXTERNAL_SCRIPTS:
            self.assertFalse(list(ROOT.rglob(name)),
                             name + " belongs to another skill and must not be vendored")

    def test_role_documents_named_in_the_skill_exist(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for role in ("commander", "literature", "analyst", "executor", "reviewer",
                     "experiment-protocol", "templates", "context-handoff"):
            self.assertIn("references/%s.md" % role, text, role + " is not routed in SKILL.md")
            self.assertTrue((ROOT / "references" / (role + ".md")).is_file())

    def test_pitfall_checklist_is_routed(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        protocol = (ROOT / "references" / "experiment-protocol.md").read_text(encoding="utf-8")
        self.assertIn("pitfalls.md", skill)
        self.assertIn("pitfalls.md", protocol)
        self.assertTrue((ROOT / "references" / "pitfalls.md").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
