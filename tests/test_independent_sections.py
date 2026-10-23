"""Unit/integration tests for independent heading scenes (no Manim dependency)."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sections", ROOT / "independent_sections.py")
sec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sec)

DECK = '''
class Slide:
    def __init__(self):
        self.events = []
    def heading(self, value):
        self.events.append("heading " + value)
    def play(self, value):
        self.events.append("play " + value)
    def next_slide(self):
        self.events.append("pause")
    def reveal(self, value):
        self.play(value)
        self.next_slide()

CONSTANT = "constant"
class Deck(Slide):
    def construct(self):
        self.heading("first")
        first_local = "alpha"
        self.reveal(first_local)
        self.heading("second")
        second_local = "beta"
        self.reveal(second_local)
        self.heading("third")
        self.reveal(CONSTANT)
'''


class IndependentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "deck.py"
        self.source.write_text(DECK, encoding="utf-8")
        self.preview = self.root / ".manim-slides-preview"
        self.settings = ["-ql", False, "", [], True, True]

    def plan(self):
        return sec.plan(self.source, self.root, self.preview, self.settings, "Deck")

    def commit(self, plan):
        for name in plan["dirty"]:
            folder = self.root / "slides" / "files" / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "part.mp4").write_bytes(b"test")
            (self.root / "slides" / (name + ".json")).write_text("{}", encoding="utf-8")
        os.replace(plan["candidate"], plan["manifest"])

    def test_independent_execution_and_unchanged_reuse(self):
        p = self.plan()
        self.assertEqual(len(p["scenes"]), 3)
        self.assertEqual(len(p["dirty"]), 3)
        namespace = {"__name__": "msp_test_wrapper"}
        exec(compile(Path(p["source"]).read_text(), p["source"], "exec"), namespace)
        first = namespace[p["scenes"][0]]()
        second = namespace[p["scenes"][1]]()
        third = namespace[p["scenes"][2]]()
        first.construct()
        second.construct()
        third.construct()
        self.assertEqual(first.events, ["heading first", "play alpha", "pause"])
        self.assertEqual(second.events, ["heading second", "play beta", "pause"])
        self.assertEqual(third.events, ["heading third", "play constant", "pause"])
        self.commit(p)
        again = self.plan()
        self.assertEqual(again["dirty"], [])
        self.assertEqual(len(again["reused"]), 3)

    def test_edit_first_section_renders_only_first(self):
        first = self.plan()
        self.commit(first)
        self.source.write_text(DECK.replace('"first"', '"FIRST"'), encoding="utf-8")
        next_plan = self.plan()
        self.assertEqual(next_plan["dirty"], [first["scenes"][0]])
        self.assertEqual(next_plan["reused"], first["scenes"][1:])

    def test_edit_middle_section_renders_only_middle(self):
        first = self.plan()
        self.commit(first)
        self.source.write_text(DECK.replace('"beta"', '"BETA"'), encoding="utf-8")
        next_plan = self.plan()
        self.assertEqual(next_plan["dirty"], [first["scenes"][1]])

    def test_global_change_invalidates_all(self):
        first = self.plan()
        self.commit(first)
        self.source.write_text(DECK.replace('"constant"', '"updated"'), encoding="utf-8")
        self.assertEqual(len(self.plan()["dirty"]), 3)

    def test_python_dependency_change_invalidates_all(self):
        helper = self.root / "helper.py"
        helper.write_text("v=1", encoding="utf-8")
        first = self.plan()
        self.commit(first)
        helper.write_text("v=2", encoding="utf-8")
        self.assertEqual(len(self.plan()["dirty"]), 3)

    def test_missing_output_invalidates_only_missing_section(self):
        first = self.plan()
        self.commit(first)
        (self.root / "slides" / (first["scenes"][1] + ".json")).unlink()
        self.assertEqual(self.plan()["dirty"], [first["scenes"][1]])

    def test_quality_change_invalidates_all(self):
        first = self.plan()
        self.commit(first)
        self.settings[0] = "-qh"
        self.assertEqual(len(self.plan()["dirty"]), 3)

    def test_cross_section_local_rejected(self):
        code = DECK.replace('second_local = "beta"', 'second_local = first_local')
        with self.assertRaisesRegex(ValueError, "cross-section locals"):
            sec.partition(code, "Deck")

    def test_nested_heading_rejected(self):
        code = DECK.replace('self.heading("second")',
                            'if True:\n            self.heading("second")')
        with self.assertRaises(ValueError):
            sec.partition(code, "Deck")

    def test_bom_source(self):
        self.source.write_text("\ufeff" + DECK, encoding="utf-8")
        self.assertEqual(len(self.plan()["scenes"]), 3)

    def test_failed_render_does_not_commit(self):
        self.plan()
        self.assertFalse(any(p.name.endswith(".json") and not p.name.endswith(".pending.json") for p in self.preview.rglob("*.json")))
        self.assertEqual(len(self.plan()["dirty"]), 3)

if __name__ == "__main__":
    unittest.main(verbosity=2)
