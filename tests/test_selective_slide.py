"""Pure-Python checks for selective slide-prefix rendering.

No Manim installation is required: the tests exercise the production parser,
manifest lifecycle, boundary hooks, invalidation and fallback behavior.
"""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The production helper patches Slide.next_slide on import. Emulate the concrete
# class, and make sure BaseSlide.next_slide.__wrapped__ remains accessible.
original_modules = {n: sys.modules.get(n) for n in
                    ("manim_slides", "manim_slides.slide", "manim_slides.slide.manim")}
fake_root = types.ModuleType("manim_slides")
fake_slide_package = types.ModuleType("manim_slides.slide")
fake_slide_module = types.ModuleType("manim_slides.slide.manim")


class DemoSlide:
    def next_slide(self):
        self.boundary_calls += 1


fake_slide_module.Slide = DemoSlide
sys.modules["manim_slides"] = fake_root
sys.modules["manim_slides.slide"] = fake_slide_package
sys.modules["manim_slides.slide.manim"] = fake_slide_module
try:
    spec = importlib.util.spec_from_file_location("selective_module", ROOT / "selective_slide.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
finally:
    for name, previous in original_modules.items():
        if previous is None:
            del sys.modules[name]
        else:
            sys.modules[name] = previous

SOURCE = """
from manim import *
X = 1
class Deck(Slide):
    def construct(self):
        self.play(Write(Text("one")))
        self.next_slide()
        self.play(Write(Text("two")))
        self.next_slide()
        self.play(Write(Text("three")))
"""

def changed_middle():
    return SOURCE.replace('Text("two")', 'Text("TWO")')

FakeMobject = type("Text", (), {
    "__module__": "manim.mobject.text.text_mobject",
    "__str__": lambda self: self.value,
    "__init__": lambda self, value: setattr(self, "value", value)
})
def _init_animation(self, mob):
    self.mobject = mob
    self.run_time = 1.0


FakeAnimation = type("Write", (), {
    "__module__": "manim.animation.creation",
    "__init__": _init_animation,
})


class FakeRenderer:
    def __init__(self):
        self.num_plays = 0
        self.animations_hashes = []


class Deck(DemoSlide):
    def __init__(self):
        self.boundary_calls = 0
        self.renderer = FakeRenderer()


class SelectiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "deck.py"
        self.store = self.root / "cache" / "manifests"
        self.source.write_text(SOURCE, encoding="utf-8")
        self.cache = mod.SlidePrefixCache()

    def request(self):
        return {
            "args": ["render", str(self.source), "Deck", "-ql", "--media_dir",
                     str(self.root / "media")],
            "in_process": True, "selective_slides": True,
            "selective_root": str(self.root), "selective_store": str(self.store)
        }

    def scene(self):
        # __name__ used as manifest lookup key, same as actual class
        return type("Deck", (Deck,), {} )()

    def frame(self, name="construct"):
        return types.SimpleNamespace(f_code=types.SimpleNamespace(
            co_name=name, co_filename=str(self.source)))

    def play(self, scene, value, digest):
        anim = FakeAnimation(FakeMobject(value))
        reused = self.cache.hash_or_none(scene, [anim])
        scene.renderer.animations_hashes.append(reused or digest)
        i = scene.renderer.num_plays
        scene.renderer.num_plays += 1
        self.cache.play_finished(scene, scene.renderer, i)
        return reused

    def seed(self):
        self.cache.begin(self.request())
        scene = self.scene()
        self.play(scene, "one", "hash_one")
        self.cache.slide_boundary(scene, self.frame())
        self.play(scene, "two", "hash_two")
        self.cache.slide_boundary(scene, self.frame())
        self.play(scene, "three", "hash_three")
        self.cache.complete(True)

    def test_prefix_is_reused_only_before_modified_slide(self):
        self.seed()
        self.source.write_text(changed_middle(), encoding="utf-8")
        self.cache.begin(self.request())
        self.assertEqual(self.cache.scenes["Deck"]["allow"], 1)
        scene = self.scene()
        self.assertEqual(self.play(scene, "one", "new_one"), "hash_one")
        self.cache.slide_boundary(scene, self.frame())
        self.assertIsNone(self.play(scene, "TWO", "new_two"))
        self.cache.slide_boundary(scene, self.frame())
        self.assertIsNone(self.play(scene, "three", "new_three"))
        self.cache.complete(True)
        self.assertEqual(self.cache.scenes["Deck"]["reused"], 1)

    def test_changes_to_helper_or_global_invalidate_prefix(self):
        self.seed()
        self.source.write_text(SOURCE.replace("X = 1", "X = 2"), encoding="utf-8")
        self.cache.begin(self.request())
        self.assertEqual(self.cache.scenes["Deck"]["allow"], 0)

    def test_external_python_change_invalidates_prefix(self):
        (self.root / "helpers.py").write_text("VALUE = 1", encoding="utf-8")
        self.seed()
        (self.root / "helpers.py").write_text("VALUE = 2", encoding="utf-8")
        self.cache.begin(self.request())
        self.assertEqual(self.cache.scenes["Deck"]["allow"], 0)

    def test_drift_in_animation_signature_disables_reuse(self):
        self.seed()
        self.source.write_text(changed_middle(), encoding="utf-8")
        self.cache.begin(self.request())
        scene = self.scene()
        self.assertIsNone(self.play(scene, "unexpected one", "fresh_one"))
        self.assertEqual(self.cache.scenes["Deck"]["allow"], 0)

    def test_nested_slide_boundary_is_unsupported(self):
        text = SOURCE.replace("        self.next_slide()", "        if True:\n            self.next_slide()", 1)
        self.assertIsNone(mod._source_layout(text, "Deck"))

    def test_bad_runtime_boundary_count_cannot_write_manifest(self):
        self.cache.begin(self.request())
        sc = self.scene()
        self.play(sc, "one", "hash_one")
        self.cache.complete(True)
        self.assertFalse(list(self.store.glob("*.json")))

    def test_failure_cannot_write_manifest(self):
        self.cache.begin(self.request())
        sc = self.scene()
        self.play(sc, "one", "hash_one")
        self.cache.slide_boundary(sc, self.frame())
        self.play(sc, "two", "hash_two")
        self.cache.slide_boundary(sc, self.frame())
        self.play(sc, "three", "hash_three")
        self.cache.complete(False)
        self.assertFalse(list(self.store.glob("*.json")))

    def test_unchanged_source_reuses_every_animation(self):
        self.seed()
        self.cache.begin(self.request())
        self.assertEqual(self.cache.scenes["Deck"]["allow"], 3)

    def test_utf8_bom_source_is_supported(self):
        self.source.write_text("\ufeff" + SOURCE, encoding="utf-8")
        self.cache.begin(self.request())
        self.assertTrue(self.cache.active)
        self.assertIn("Deck", self.cache.scenes)

    def test_helper_created_boundaries_do_not_change_block_count(self):
        src = SOURCE.replace('self.play(Write(Text("one")))',
                             'self.reveal(Write(Text("one")))')
        self.assertEqual(len(mod._source_layout(src, "Deck")["blocks"]), 3)

    def test_disabled_mode_never_reuses(self):
        self.seed()
        req = self.request()
        req["selective_slides"] = False
        self.cache.begin(req)
        self.assertFalse(self.cache.active)

    def test_concrete_slide_hook_preserves_original_behavior(self):
        saved = mod._SLIDE_CACHE
        try:
            mod._SLIDE_CACHE = self.cache
            self.cache.begin(self.request())
            sc = self.scene()
            # A helper's slide boundary must not be mistaken for an explicit
            # construct() boundary.
            sc.next_slide()
            self.assertEqual(sc.boundary_calls, 1)
            self.assertEqual(self.cache.scenes["Deck"]["boundaries"], [0])
            ns = {}
            exec(compile("def construct(self): self.next_slide()", str(self.source),
                         "exec"), ns)
            ns["construct"](sc)
            self.assertEqual(sc.boundary_calls, 2)
            self.assertEqual(self.cache.scenes["Deck"]["boundaries"], [0, 0])
        finally:
            mod._SLIDE_CACHE = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
