"""Regression checks for the embedded warm-daemon Cairo optimizations.

Runs without Manim installed. These tests execute the actual functions from the
daemon AST with representative stubs, catching errors that static syntax checks
cannot (in particular, counter initialization with profiling disabled).
"""
import ast
import contextlib
import io
import sys
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


if __name__ == "__main__":
    unittest.main()
