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

# The marker is a no-op in ordinary Manim rendering as well. It remains a
# Python method, and the independent section renderer recognizes its call.
MARKER_DECK = (
    DECK.replace('    def construct(self):\n',
                 '    def section(self):\n        pass\n    def construct(self):\n')
        .replace('self.heading("first")', 'self.section()')
        .replace('self.heading("second")', 'self.section()')
        .replace('self.heading("third")', 'self.section()')
)


HELPER_DECK = '''
class LessonSlide:
    def __init__(self):
        self.events = []
    def heading(self, title):
        # Existing objects are cleared on every heading, like the real base.
        self.events.append("heading " + title)
    def bullet_column(self, values):
        return list(values)
    def next_slide(self):
        self.events.append("pause")
    def play(self, obj):
        self.events.append("show " + obj)
    def reveal(self, *objects):
        for obj in objects:
            self.play(obj)
            self.next_slide()
    def finish_slide(self):
        self.next_slide()

SHARED_TITLE = "Infos"
class Deck(LessonSlide):
    def construct(self):
        self.show_info()
        self.show_criteria()
        self.show_submissions()

    def show_info(self):
        self.heading(SHARED_TITLE)
        info = self.bullet_column(["Leon", "E-mail"])
        self.next_slide()
        self.reveal(*info)
        # finish_slide() deliberately not called

    def show_criteria(self):
        self.heading("Scheinkriterien")
        criteria = self.bullet_column(["attendance", "points"])
        self.reveal(*criteria)

    def show_submissions(self):
        self.heading("Abgaben")
        self.reveal("PDF")
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

    def test_section_only_deck_without_headings(self):
        self.source.write_text(MARKER_DECK, encoding="utf-8")
        p = self.plan()
        self.assertEqual(len(p["scenes"]), 3)
        namespace = {"__name__": "msp_marker_wrapper"}
        exec(compile(Path(p["source"]).read_text(), p["source"], "exec"), namespace)
        first = namespace[p["scenes"][0]]()
        second = namespace[p["scenes"][1]]()
        third = namespace[p["scenes"][2]]()
        for scene in (first, second, third):
            scene.construct()
        self.assertEqual(first.events, ["play alpha", "pause"])
        self.assertEqual(second.events, ["play beta", "pause"])
        self.assertEqual(third.events, ["play constant", "pause"])
        self.commit(p)
        self.assertEqual(self.plan()["dirty"], [])
        self.source.write_text(MARKER_DECK.replace('"alpha"', '"ALPHA"'), encoding="utf-8")
        changed = self.plan()
        self.assertEqual(changed["dirty"], [p["scenes"][0]])
        self.assertEqual(changed["reused"], p["scenes"][1:])

    def test_mixed_headings_and_section_markers(self):
        mixed = DECK.replace("    def construct(self):\n",
                             "    def section(self):\n        pass\n    def construct(self):\n")
        mixed = mixed.replace('self.heading("second")', 'self.section()')
        self.source.write_text(mixed, encoding="utf-8")
        p = self.plan()
        self.assertEqual(len(p["scenes"]), 3)
        namespace = {"__name__": "msp_mixed_wrapper"}
        exec(compile(Path(p["source"]).read_text(), p["source"], "exec"), namespace)
        middle = namespace[p["scenes"][1]]()
        middle.construct()
        self.assertEqual(middle.events, ["play beta", "pause"])
        self.commit(p)
        self.source.write_text(mixed.replace('"beta"', '"BETA"'), encoding="utf-8")
        self.assertEqual(self.plan()["dirty"], [p["scenes"][1]])

    def test_adjacent_section_marker_and_heading_are_one_section(self):
        mixed = DECK.replace("    def construct(self):\n",
                             "    def section(self):\n        pass\n    def construct(self):\n")
        mixed = mixed.replace('self.heading("second")',
                              'self.section()\n        self.heading("second")')
        self.source.write_text(mixed, encoding="utf-8")
        p = self.plan()
        self.assertEqual(len(p["scenes"]), 3)
        namespace = {"__name__": "msp_adjacent_wrapper"}
        exec(compile(Path(p["source"]).read_text(), p["source"], "exec"), namespace)
        second = namespace[p["scenes"][1]]()
        second.construct()
        self.assertEqual(second.events, ["heading second", "play beta", "pause"])

    def test_invalid_marker_arguments_rejected(self):
        code = MARKER_DECK.replace('self.section()', 'self.section("name")', 1)
        with self.assertRaises(ValueError):
            sec.partition(code, "Deck")

    def test_nested_marker_rejected(self):
        code = MARKER_DECK.replace('        self.section()\n',
                                   '        if True:\n            self.section()\n', 1)
        with self.assertRaises(ValueError):
            sec.partition(code, "Deck")

    def test_trailing_empty_section_rejected(self):
        code = MARKER_DECK.rstrip() + "\n        self.section()\n"
        with self.assertRaisesRegex(ValueError, "trailing"):
            sec.partition(code, "Deck")

    def _helper_source(self, content=HELPER_DECK):
        self.source.write_text(content, encoding="utf-8")
        return self.plan()

    def test_helper_method_sections_preserve_full_content_and_reveal_pauses(self):
        initial = self._helper_source()
        self.assertEqual(initial["mode"], "helper-methods")
        self.assertEqual(len(initial["scenes"]), 3)
        self.assertEqual(len(initial["dirty"]), 3)
        ns = {"__name__": "independent_helpers"}
        exec(compile(Path(initial["source"]).read_text(encoding="utf-8"),
                     initial["source"], "exec"), ns)
        scenes = [ns[name]() for name in initial["scenes"]]
        for scene in scenes:
            scene.construct()
        self.assertEqual(scenes[0].events,
                         ["heading Infos", "pause", "show Leon", "pause",
                          "show E-mail", "pause"])
        self.assertEqual(scenes[1].events,
                         ["heading Scheinkriterien", "show attendance", "pause",
                          "show points", "pause"])
        self.assertEqual(scenes[2].events,
                         ["heading Abgaben", "show PDF", "pause"])
        self.commit(initial)
        repeat = self.plan()
        self.assertEqual(repeat["dirty"], [])
        self.assertEqual(repeat["reused"], initial["scenes"])

    def test_edit_one_helper_method_rerenders_only_that_section(self):
        initial = self._helper_source()
        self.commit(initial)
        changed = HELPER_DECK.replace('"attendance", "points"',
                                      '"attendance", "more points"')
        updated = self._helper_source(changed)
        self.assertEqual(updated["dirty"], [initial["scenes"][1]])
        self.assertEqual(updated["reused"],
                         [initial["scenes"][0], initial["scenes"][2]])
        self.commit(updated)
        self.assertEqual(self.plan()["dirty"], [])

    def test_helper_method_docstring_change_is_local_to_section(self):
        initial = self._helper_source()
        self.commit(initial)
        changed = HELPER_DECK.replace(
            '    def show_criteria(self):\n',
            '    def show_criteria(self):\n        """New description."""\n')
        result = self._helper_source(changed)
        self.assertEqual(result["dirty"], [initial["scenes"][1]])

    def test_unchanged_helper_order_preserved_and_reorder_invalidates_positions(self):
        initial = self._helper_source()
        self.commit(initial)
        changed = HELPER_DECK.replace(
            '        self.show_info()\n        self.show_criteria()\n',
            '        self.show_criteria()\n        self.show_info()\n')
        updated = self._helper_source(changed)
        self.assertEqual(updated["dirty"], initial["scenes"][:2])
        self.assertEqual(updated["reused"], initial["scenes"][2:])
        ns = {"__name__": "helpers_reordered"}
        exec(compile(Path(updated["source"]).read_text(encoding="utf-8"),
                     updated["source"], "exec"), ns)
        first = ns[updated["scenes"][0]]()
        first.construct()
        self.assertEqual(first.events[0], "heading Scheinkriterien")

    def test_shared_helper_changes_invalidate_all_helper_sections(self):
        initial = self._helper_source()
        self.commit(initial)
        changed = HELPER_DECK.replace('self.events.append("show " + obj)',
                                      'self.events.append("display " + obj)')
        self.assertEqual(self._helper_source(changed)["dirty"],
                         initial["scenes"])

    def test_unrelated_scene_class_helper_changes_invalidate_all(self):
        initial = self._helper_source()
        self.commit(initial)
        changed = HELPER_DECK.replace(
            '    def show_submissions(self):',
            '    def supporting_helper(self):\n        return 42\n\n'
            '    def show_submissions(self):')
        self.assertEqual(self._helper_source(changed)["dirty"],
                         initial["scenes"])

    def _private_helper_deck(self):
        return HELPER_DECK.replace(
            '        criteria = self.bullet_column(["attendance", "points"])',
            '        criteria = self._criteria()').replace(
            '    def show_submissions(self):',
            '    def _criteria(self):\n'
            '        return self._rows(["attendance", "points"])\n\n'
            '    def _rows(self, values):\n'
            '        return self.bullet_column(values)\n\n'
            '    def show_submissions(self):')

    def test_private_helper_body_change_is_local_to_users(self):
        deck = self._private_helper_deck()
        initial = self._helper_source(deck)
        self.commit(initial)
        # Transitive helper (_rows via _criteria) only affects show_criteria.
        changed = deck.replace("return self.bullet_column(values)",
                               "return self.bullet_column(values[::-1])")
        updated = self._helper_source(changed)
        self.assertEqual(updated["dirty"], [initial["scenes"][1]])
        ns = {"__name__": "private_helpers"}
        exec(compile(Path(updated["source"]).read_text(encoding="utf-8"),
                     updated["source"], "exec"), ns)
        second = ns[updated["scenes"][1]]()
        second.construct()
        self.assertIn("show points", second.events)

    def test_private_helper_signature_change_invalidates_all(self):
        deck = self._private_helper_deck()
        initial = self._helper_source(deck)
        self.commit(initial)
        changed = deck.replace("def _rows(self, values):",
                               "def _rows(self, values, extra=None):")
        self.assertEqual(self._helper_source(changed)["dirty"], initial["scenes"])

    def test_private_helper_referenced_indirectly_stays_shared(self):
        deck = self._private_helper_deck().replace(
            '        self.reveal("PDF")', '        self.reveal(str("_rows"))')
        initial = self._helper_source(deck)
        self.commit(initial)
        changed = deck.replace('str("_rows")', 'str("_rows")').replace(
            "return self.bullet_column(values)",
            "return self.bullet_column(list(values))")
        self.assertEqual(self._helper_source(changed)["dirty"], initial["scenes"])

    def test_escaping_self_keeps_helpers_shared(self):
        deck = self._private_helper_deck().replace(
            '        self.reveal("PDF")', '        self.reveal(repr(self))')
        initial = self._helper_source(deck)
        self.commit(initial)
        changed = deck.replace("return self.bullet_column(values)",
                               "return self.bullet_column(list(values))")
        self.assertEqual(self._helper_source(changed)["dirty"], initial["scenes"])

    def test_module_level_value_changes_invalidate_all(self):
        initial = self._helper_source()
        self.commit(initial)
        changed = HELPER_DECK.replace('SHARED_TITLE = "Infos"',
                                      'SHARED_TITLE = "Information"')
        self.assertEqual(self._helper_source(changed)["dirty"],
                         initial["scenes"])

    def test_helper_dependency_file_changes_invalidate_all(self):
        helper = self.root / "slide_utils" / "slides.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("VALUE = 1\n", encoding="utf-8")
        initial = self._helper_source()
        self.commit(initial)
        helper.write_text("VALUE = 2\n", encoding="utf-8")
        self.assertEqual(self.plan()["dirty"], initial["scenes"])

    def test_render_option_change_invalidates_all_helper_sections(self):
        initial = self._helper_source()
        self.commit(initial)
        self.settings[0] = "-qh"
        self.assertEqual(self.plan()["dirty"], initial["scenes"])

    def test_missing_helper_method_fallback_reason(self):
        changed = HELPER_DECK.replace("    def show_submissions(self):",
                                      "    def unused_submissions(self):")
        with self.assertRaisesRegex(ValueError, "section method show_submissions"):
            sec.partition(changed, "Deck")

    def test_helper_without_initial_heading_fallback_reason(self):
        changed = HELPER_DECK.replace(
            '        self.heading("Scheinkriterien")\n',
            '        self.next_slide()\n')
        with self.assertRaisesRegex(ValueError, "must start with self.heading"):
            sec.partition(changed, "Deck")

    def test_helper_decorated_or_argumented_fallback(self):
        changed = HELPER_DECK.replace(
            "    def show_criteria(self):", "    def show_criteria(self, x):")
        with self.assertRaisesRegex(ValueError, "undecorated"):
            sec.partition(changed, "Deck")
        changed = HELPER_DECK.replace(
            "        self.show_criteria()", '        self.show_criteria("oops")')
        with self.assertRaisesRegex(ValueError, "only argument-free"):
            sec.partition(changed, "Deck")

    def test_helper_construct_extra_statements_fall_back(self):
        changed = HELPER_DECK.replace(
            "        self.show_criteria()",
            "        saved = 5\n        self.show_criteria()")
        with self.assertRaisesRegex(ValueError, "without other statements"):
            sec.partition(changed, "Deck")

    def test_helper_cross_section_state_dependency_falls_back(self):
        changed = HELPER_DECK.replace(
            '        info = self.bullet_column(["Leon", "E-mail"])',
            '        self.saved = "bad"\n'
            '        info = self.bullet_column(["Leon", "E-mail"])')
        with self.assertRaisesRegex(ValueError, "attribute mutations"):
            sec.partition(changed, "Deck")

    def test_helper_calls_another_section_method_falls_back(self):
        changed = HELPER_DECK.replace(
            '        self.reveal("PDF")',
            '        self.show_info()\n        self.reveal("PDF")')
        with self.assertRaisesRegex(ValueError, "nested show"):
            sec.partition(changed, "Deck")

    def test_helper_finish_slide_boundary_kept_when_present(self):
        changed = HELPER_DECK.replace(
            '        self.reveal("PDF")',
            '        self.reveal("PDF")\n        self.finish_slide()')
        p = self._helper_source(changed)
        ns = {"__name__": "helper_with_finish"}
        exec(compile(Path(p["source"]).read_text(encoding="utf-8"),
                     p["source"], "exec"), ns)
        last = ns[p["scenes"][-1]]()
        last.construct()
        self.assertEqual(last.events, ["heading Abgaben", "show PDF", "pause",
                                       "pause"])

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
