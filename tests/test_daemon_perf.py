"""Regression checks for the embedded warm-daemon Cairo optimizations.

Runs without Manim installed. These tests execute the actual functions from the
daemon AST with representative stubs, catching errors that static syntax checks
cannot (in particular, counter initialization with profiling disabled).
"""
import ast
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from functools import lru_cache
from pathlib import Path
from time import perf_counter

DAEMON = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/manim-preview-daemon.py")
# unittest.main should not interpret the daemon filename as a test selector.
sys.argv = [sys.argv[0]]

tree = ast.parse(DAEMON.read_text(encoding="utf-8"))
assignments = [
    node for node in ast.walk(tree)
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "_CACHE_FAST"
            for target in node.targets)
]
assert len(assignments) == 1, "expected one _CACHE_FAST initializer"
initial = ast.literal_eval(assignments[0].value)
assert "skipped_fades" in initial, "cached-FadeOut counter must exist without profiling"

methods = {}
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
        "_begin_cached_fades", "_measure_play"
    }:
        methods[node.name] = node
assert set(methods) == {"_begin_cached_fades", "_measure_play"}

@lru_cache(maxsize=20)
def cached_source(x):
    return x

class FakeScene:
    def __init__(self):
        self.animations = [object()]
        self.renderer = None
        self.moving_mobjects = [object()]
        self.static_mobjects = [object()]
        self._msp_cached_fades = False
        self.original_begin_calls = 0


class FakeRenderer:
    num_plays = 0
    skip_animations = True

    def __init__(self, scene):
        self.scene = scene
        scene.renderer = self


class CacheFastTest(unittest.TestCase):
    def setUp(self):
        self.ns = {
            "_CACHE_FAST": dict(initial),
            "_clock": perf_counter,
            "_cached_code_source": cached_source,
            "_simple_cached_fades": lambda scene: True,
        }
        self.scene = FakeScene()
        self.renderer = FakeRenderer(self.scene)

        def original_begin(scene):
            scene.original_begin_calls += 1
            return "original begin"

        def original_play(renderer):
            self.ns["_begin_cached_fades"](renderer.scene)
            return "played"

        self.ns["_original_begin_animations"] = original_begin
        self.ns["_original_cairo_play"] = original_play
        code = compile(ast.Module(body=list(methods.values()), type_ignores=[]),
                       filename=str(DAEMON), mode="exec")
        exec(code, self.ns)

    def test_cached_fade_when_profiling_disabled(self):
        self.ns["_CACHE_FAST"]["profile"] = False
        self.assertEqual(self.ns["_measure_play"](self.renderer), "played")
        self.assertTrue(self.scene._msp_cached_fades)
        self.assertEqual(self.ns["_CACHE_FAST"]["skipped_fades"], 1)
        self.assertEqual(self.scene.original_begin_calls, 0)

    def test_cached_fade_when_profiling_enabled(self):
        self.ns["_CACHE_FAST"]["profile"] = True
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.ns["_measure_play"](self.renderer), "played")
        self.assertIn("fast_fades=1", output.getvalue())

    def test_cached_fade_without_counter_key(self):
        # Mimics a partially stale daemon state; never crash inside
        # begin_animations even when the profiling counter is missing.
        del self.ns["_CACHE_FAST"]["skipped_fades"]
        self.ns["_begin_cached_fades"](self.scene)
        self.assertEqual(self.ns["_CACHE_FAST"]["skipped_fades"], 1)

    def test_fallback_when_fast_path_disabled(self):
        self.ns["_CACHE_FAST"]["enabled"] = False
        self.assertEqual(self.ns["_measure_play"](self.renderer), "played")
        self.assertFalse(self.scene._msp_cached_fades)
        self.assertEqual(self.scene.original_begin_calls, 1)


class PurgeUserModulesTest(unittest.TestCase):
    """The warm daemon must not render with stale helper modules (e.g. slide_utils)."""

    def setUp(self):
        wanted_defs = {"_library_roots", "_purge_user_modules"}
        body = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted_defs:
                body.append(node)
            elif isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_LIB_ROOTS" for t in node.targets):
                body.append(node)
            elif isinstance(node, ast.Import) and any(
                    a.asname in {"_importlib", "_sysconfig"} for a in node.names):
                body.append(node)
        self.assertEqual(len(body), 4, "purge helpers missing from the daemon script")
        self.ns = {"os": os, "sys": sys}
        exec(compile(ast.Module(body=body, type_ignores=[]), str(DAEMON), "exec"), self.ns)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        sys.path.insert(0, self.folder.name)
        self.addCleanup(sys.path.remove, self.folder.name)
        self.addCleanup(sys.modules.pop, "msp_helper_mod", None)
        self.module = Path(self.folder.name) / "msp_helper_mod.py"

    def test_edited_helper_module_is_reimported(self):
        self.module.write_text("VALUE = 1", encoding="utf-8")
        import msp_helper_mod
        self.assertEqual(msp_helper_mod.VALUE, 1)
        self.module.write_text("VALUE = 22", encoding="utf-8")
        self.assertIn("msp_helper_mod", self.ns["_purge_user_modules"]())
        import msp_helper_mod as fresh
        self.assertEqual(fresh.VALUE, 22)

    def test_library_and_entry_modules_are_kept(self):
        import json
        before = {"json", "os", "unittest", "__main__"} & set(sys.modules)
        dropped = set(self.ns["_purge_user_modules"]())
        self.assertFalse(before & dropped)
        self.assertIs(sys.modules["json"], json)

    def test_manim_packages_are_never_purged(self):
        fake = types.ModuleType("manim_slides.msp_fake")
        fake.__file__ = str(self.module)
        sys.modules["manim_slides.msp_fake"] = fake
        self.addCleanup(sys.modules.pop, "manim_slides.msp_fake", None)
        self.assertNotIn("manim_slides.msp_fake", self.ns["_purge_user_modules"]())


if __name__ == "__main__":
    unittest.main()
