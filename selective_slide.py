"""Conservative incremental *prefix* rendering for manim-slides (Cairo).

This module is inserted into the warm Python daemon, after the Cairo and
BaseSlide monkeypatches have been installed. It does not checkpoint Python
frames, pickle scene objects or skip user Python code. It only bypasses the
expensive animation hash for an unchanged prefix of slides, while letting
Manim reuse the exact pre-existing partial videos. A changed slide and every
slide after it are processed normally.

On unsupported source layouts, changed dependencies, or divergent animation
signatures it fails closed to the ordinary Manim hashing path.
"""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import sys


def _digest(value):
    if not isinstance(value, bytes):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _is_next_slide(stmt):
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    fn = stmt.value.func
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr == "next_slide"
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "self"
    )


def _has_nested_boundary(stmt):
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "next_slide"
        for node in ast.walk(stmt)
    )


def _source_layout(source, scene_name):
    """Return immutable block fingerprints or None for unsupported scene ASTs."""
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    matches = [
        cls for cls in module.body
        if isinstance(cls, ast.ClassDef) and cls.name == scene_name
    ]
    if len(matches) != 1:
        return None
    cls = matches[0]
    constructs = [
        node for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "construct"
    ]
    if len(constructs) != 1 or isinstance(constructs[0], ast.AsyncFunctionDef):
        return None
    construct = constructs[0]
    # Nested self.next_slide calls cannot be mapped reliably to source blocks.
    blocks, current = [], []
    for stmt in construct.body:
        if _is_next_slide(stmt):
            blocks.append(_digest(ast.dump(ast.Module(body=current, type_ignores=[]),
                                           include_attributes=False)))
            current = []
        elif _has_nested_boundary(stmt):
            return None
        else:
            current.append(stmt)
    blocks.append(_digest(ast.dump(ast.Module(body=current, type_ignores=[]),
                                   include_attributes=False)))
    if len(blocks) < 2:
        return None

    # All code except the target scene's construct must be identical. Changes
    # to global constants, imports, helper methods or other classes invalidate
    # every old prefix hash, even if the slide blocks themselves match.
    clean = copy.deepcopy(module)
    target_cls = next(
        node for node in clean.body
        if isinstance(node, ast.ClassDef) and node.name == scene_name
    )
    target_construct = next(
        node for node in target_cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "construct"
    )
    target_construct.body = [ast.Pass()]
    environment = _digest(ast.dump(clean, include_attributes=False))
    return {"blocks": blocks, "environment": environment}


def _dependencies(root, primary):
    """Hash project Python files except the edited scene, with hard limits."""
    root = Path(root).resolve()
    primary = Path(primary).resolve()
    if not root.is_dir() or not primary.is_file() or not primary.is_relative_to(root):
        return None
    rows, total = [], 0
    excluded = {".venv", "venv", ".git", "node_modules", "__pycache__",
                ".mypy_cache", ".pytest_cache", ".manim-slides-preview",
                "site-packages", "dist", "build"}
    try:
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in excluded
                             and not d.startswith("."))
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                item = Path(current) / name
                if item.resolve() == primary:
                    continue
                data = item.read_bytes()
                total += len(data)
                if total > 16_000_000 or len(rows) >= 400:
                    return None
                rows.append([item.relative_to(root).as_posix(), _digest(data)])
    except (OSError, ValueError):
        return None
    return _digest(json.dumps(rows, separators=(",", ":")))


def _signature(animations):
    """Cheap guard for construction drift before reusing an old play hash.

    Only stock Manim animations and mobjects are eligible. This check is not
    a general substitute for Manim's deep hash, so use the selective setting
    only with deterministic presentation code.
    """
    rows = []
    try:
        for animation in animations:
            cls = type(animation)
            mob = animation.mobject
            if not cls.__module__.startswith("manim."):
                return None
            if mob is not None and not type(mob).__module__.startswith("manim."):
                return None
            rows.append([cls.__module__, cls.__qualname__,
                         str(mob) if mob is not None else None,
                         float(animation.run_time)])
    except Exception:
        return None
    return _digest(json.dumps(rows, separators=(",", ":")))


class SlidePrefixCache:
    FORMAT = 1

    def __init__(self):
        self.scenes = {}
        self.active = False
        self.store = None

    def begin(self, request):
        self.scenes = {}
        self.active = False
        self.store = None
        if (not request.get("selective_slides", False)
                or not request.get("in_process", True)):
            return
        args = request.get("args", [])
        if len(args) < 3 or args[0] != "render":
            return
        source_file = Path(args[1]).resolve()
        root_dir = request.get("selective_root")
        if not root_dir:
            return
        deps = _dependencies(root_dir, source_file)
        if deps is None:
            print("[msp-selective] fallback: project dependency scan unsupported",
                  flush=True)
            return
        try:
            source = source_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return
        render_params = [str(value) for value in args[3:]]
        settings = _digest(json.dumps(
            [render_params, sys.executable, sys.version, deps],
            separators=(",", ":")))
        directory = Path(request.get("selective_store") or "")
        if not directory.is_absolute():
            return
        self.store = directory
        targets = args[2:3]
        # Multi-scene rendering can interleave other settings; the plugin
        # currently supports one scene per selective plan, otherwise fallback.
        if len(args) >= 4 and not args[3].startswith("-"):
            print("[msp-selective] fallback: multi-scene render", flush=True)
            return
        for name in targets:
            layout = _source_layout(source, name)
            if not layout:
                print("[msp-selective] fallback: unsupported next_slide layout "
                      + name, flush=True)
                return
            key = _digest(str(source_file).lower() + "\0" + name)[:32]
            manifest_path = directory / (key + ".json")
            old = None
            try:
                old = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            prefix = 0
            if (isinstance(old, dict) and old.get("version") == self.FORMAT
                    and old.get("settings") == settings
                    and old.get("environment") == layout["environment"]
                    and isinstance(old.get("blocks"), list)
                    and isinstance(old.get("boundaries"), list)
                    and isinstance(old.get("hashes"), list)
                    and isinstance(old.get("signatures"), list)
                    and len(old["blocks"]) == len(old["boundaries"])
                    and len(old["hashes"]) == len(old["signatures"])):
                equal = 0
                for a, b in zip(layout["blocks"], old["blocks"]):
                    if a != b:
                        break
                    equal += 1
                if equal:
                    # Boundary i is the number of plays before slide block i.
                    # Even if the current slide differs, prior slides reuse
                    # their old content-addressed partial videos.
                    if equal == len(layout["blocks"]) == len(old["blocks"]):
                        prefix = len(old["hashes"])
                    elif equal < len(old["boundaries"]):
                        prefix = old["boundaries"][equal]
                    if not isinstance(prefix, int) or prefix < 0:
                        prefix = 0
                    prefix = min(prefix, len(old["hashes"]))
            self.scenes[name] = {
                "path": manifest_path, "source_file": source_file,
                "layout": layout, "settings": settings,
                "old": old, "allow": prefix, "hashes": [], "signatures": [],
                "pending": {}, "boundaries": [0], "reused": 0,
                "failed": False,
            }
            print("[msp-selective] scene={} reusable_prefix_plays={}".format(
                name, prefix), flush=True)
        self.active = bool(self.scenes)

    def _state(self, scene):
        if not self.active:
            return None
        return self.scenes.get(type(scene).__name__)

    def hash_or_none(self, scene, animations):
        state = self._state(scene)
        if state is None:
            return None
        i = scene.renderer.num_plays
        if i != len(state["hashes"]):
            state["allow"] = 0
            state["failed"] = True
            return None
        sig = _signature(animations)
        state["pending"][i] = sig
        if sig is None or i >= state["allow"]:
            return None
        old = state["old"]
        if (old is None or i >= len(old["signatures"])
                or sig != old["signatures"][i]):
            print("[msp-selective] dynamic animation mismatch at {}; "
                  "normal hashing from here".format(i), flush=True)
            state["allow"] = 0
            return None
        old_hash = old["hashes"][i]
        if not isinstance(old_hash, str) or not old_hash:
            state["allow"] = 0
            return None
        state["reused"] += 1
        return old_hash

    def play_finished(self, scene, renderer, prior_index):
        state = self._state(scene)
        if state is None:
            return
        if renderer.num_plays != prior_index + 1:
            state["failed"] = True
            return
        if prior_index != len(state["hashes"]):
            state["failed"] = True
            return
        hashes = renderer.animations_hashes
        if not hashes or not isinstance(hashes[-1], str):
            state["failed"] = True
            return
        state["hashes"].append(hashes[-1])
        state["signatures"].append(state["pending"].pop(prior_index, None))

    def slide_boundary(self, scene, caller_frame):
        state = self._state(scene)
        if state is None:
            return
        # Helper routines (reveal/chart/finish_slide) can call next_slide()
        # dozens of times. The parser uses ONLY explicit next_slide() calls
        # inside construct(), so checkpoints must use the same definition.
        try:
            if (caller_frame.f_code.co_name == "construct"
                    and Path(caller_frame.f_code.co_filename).resolve()
                    == state["source_file"]):
                state["boundaries"].append(scene.renderer.num_plays)
        except (OSError, ValueError):
            pass

    def complete(self, success):
        if not self.active:
            return
        for name, state in self.scenes.items():
            if not success or state["failed"]:
                continue
            bounds = state["boundaries"]
            if len(bounds) != len(state["layout"]["blocks"]):
                print("[msp-selective] fallback: runtime slide count mismatch "
                      + name, flush=True)
                continue
            if any(a > b for a, b in zip(bounds, bounds[1:])):
                continue
            if bounds[-1] > len(state["hashes"]) or None in state["signatures"]:
                continue
            obj = {
                "version": self.FORMAT, "settings": state["settings"],
                "environment": state["layout"]["environment"],
                "blocks": state["layout"]["blocks"], "boundaries": bounds,
                "hashes": state["hashes"], "signatures": state["signatures"],
            }
            try:
                self.store.mkdir(parents=True, exist_ok=True)
                tmp = state["path"].with_suffix(".tmp")
                tmp.write_text(json.dumps(obj, separators=(",", ":")),
                               encoding="utf-8")
                os.replace(tmp, state["path"])
            except OSError as exc:
                print("[msp-selective] manifest not saved: " + str(exc)[:150],
                      flush=True)
            print("[msp-selective] scene={} reused_hashes={} total_plays={}"
                  .format(name, state["reused"], len(state["hashes"])),
                  flush=True)
        self.active = False


_SLIDE_CACHE = SlidePrefixCache()
# Slide.next_slide calls BaseSlide.next_slide.__wrapped__ internally: never
# monkeypatch BaseSlide.next_slide, or the upstream __wrapped__ lookup breaks.
# Patch the concrete Manim Slide boundary instead.
try:
    from manim_slides.slide.manim import Slide as _ManimSlide
    from functools import wraps as _wraps
    _original_slide_boundary = _ManimSlide.next_slide

    @_wraps(_original_slide_boundary)
    def _selective_next_slide(self, *args, **kwargs):
        result = _original_slide_boundary(self, *args, **kwargs)
        _SLIDE_CACHE.slide_boundary(self, sys._getframe(1))
        return result

    _ManimSlide.next_slide = _selective_next_slide
except Exception as _selective_patch_error:
    print("[msp-selective] unavailable: " + str(_selective_patch_error)[:160],
          flush=True)
