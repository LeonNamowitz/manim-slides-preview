
"""Opt-in independent heading or explicit marker sections; each is a Manim Slide scene."""
import ast, hashlib, json, os, re, sys
from pathlib import Path

VERSION=2
SKIP={".git",".venv","venv","node_modules","__pycache__", ".manim-slides-preview","site-packages","dist","build",".pytest_cache"}
def sha(x):
    return hashlib.sha256(x.encode("utf-8") if isinstance(x,str) else x).hexdigest()
def heading(n):
    return (isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and
            isinstance(n.value.func,ast.Attribute) and
            n.value.func.attr=="heading" and isinstance(n.value.func.value,ast.Name) and
            n.value.func.value.id=="self")
def section_marker(n):
    # Exact self.section() statement, with no arguments, is an explicit
    # boundary. Unlike heading(), it need not display anything. The caller
    # must provide a no-op section(self) method in its Scene/Slide subclass
    # (or base class), so ordinary unsplit renders remain valid too.
    return (isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and
            isinstance(n.value.func,ast.Attribute) and
            n.value.func.attr=="section" and
            isinstance(n.value.func.value,ast.Name) and
            n.value.func.value.id=="self" and
            not n.value.args and not n.value.keywords)

def boundary_call(n):
    return (isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
            and n.func.attr in {"heading","section"} and
            isinstance(n.func.value,ast.Name) and n.func.value.id=="self")

def dependencies(root,source):
    rows=[];size=0
    for folder,dirs,files in os.walk(root):
        dirs[:]=sorted(d for d in dirs if d not in SKIP and not d.startswith("."))
        for filename in sorted(files):
            if not filename.endswith(".py"): continue
            item=(Path(folder)/filename).resolve()
            if item==source: continue
            blob=item.read_bytes();size+=len(blob)
            if len(rows)>=400 or size>16000000: raise ValueError("dependency scan too large")
            rows.append([item.relative_to(root).as_posix(),sha(blob)])
    return sha(json.dumps(rows,separators=(",",":")))
def _scene_method_call(stmt):
    """Recognize only a bare, argument-free self.show_*() call."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    call = stmt.value
    fn = call.func
    if (call.args or call.keywords or not isinstance(fn, ast.Attribute)
            or not isinstance(fn.value, ast.Name) or fn.value.id != "self"
            or not re.fullmatch(r"show_[A-Za-z0-9_]+", fn.attr)):
        return None
    return fn.attr


def _unsafe_section_statements(group, *, reject_section_calls=False):
    """Check for explicitly detectable dependence on previous scene state."""
    for stmt in group:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await,
                                 ast.Global, ast.Nonlocal)):
                raise ValueError("unsupported section control flow")
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, (ast.Store, ast.Del))
                    and isinstance(node.value, ast.Name) and node.value.id == "self"):
                raise ValueError("direct scene attribute mutations unsupported")
            if (reject_section_calls and isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                    and (node.func.attr.startswith("show_")
                         or node.func.attr == "section")):
                raise ValueError(
                    "nested show_*()/section() calls in section methods unsupported"
                )


def _module_scene(module, scene):
    classes = [cls for cls in module.body
               if isinstance(cls, ast.ClassDef) and cls.name == scene]
    if len(classes) != 1:
        raise ValueError("expected one top-level scene class")
    cls = classes[0]
    methods = [method for method in cls.body
               if isinstance(method, ast.FunctionDef) and method.name == "construct"]
    if len(methods) != 1:
        raise ValueError("expected a synchronous construct method")
    construct = methods[0]
    if (construct.decorator_list or construct.args.posonlyargs
            or len(construct.args.args) != 1
            or construct.args.args[0].arg != "self"
            or construct.args.kwonlyargs or construct.args.vararg
            or construct.args.kwarg):
        raise ValueError("construct must be undecorated construct(self)")
    return cls, construct


def _helper_sections(cls, construct):
    """Return (groups, own-method ASTs), or None when not a helper deck.

    Helper-mode requires the ENTIRE construct body to be an ordered sequence
    of bare self.show_*() calls. Anything else falls back rather than
    accidentally splitting state-dependent code.
    """
    statements = list(construct.body)
    # A method docstring has no effect on section ordering.
    if (statements and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)):
        statements.pop(0)
    if not statements or not _scene_method_call(statements[0]):
        return None
    calls = [_scene_method_call(stmt) for stmt in statements]
    if any(name is None for name in calls):
        raise ValueError(
            "helper sections require construct() to contain only "
            "argument-free self.show_*() calls, without other statements"
        )
    if len(calls) < 2:
        raise ValueError("need at least two independent show_*() sections")
    if len(set(calls)) != len(calls):
        raise ValueError("repeated show_*() section calls are unsupported")
    definitions = {}
    for method in cls.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(method.name, []).append(method)

    groups, methods = [], []
    for stmt, name in zip(statements, calls):
        found = definitions.get(name, [])
        if len(found) != 1 or not isinstance(found[0], ast.FunctionDef):
            raise ValueError(
                f"section method {name} must be defined once in the scene class"
            )
        method = found[0]
        args = method.args
        if (method.decorator_list or args.posonlyargs
                or len(args.args) != 1 or args.args[0].arg != "self"
                or args.kwonlyargs or args.vararg or args.kwarg):
            raise ValueError(
                f"section method {name} must be undecorated {name}(self)"
            )
        body = list(method.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
        if not body or not heading(body[0]):
            raise ValueError(f"section method {name} must start with self.heading(...)")
        _unsafe_section_statements(body, reject_section_calls=True)
        # An additional heading would reset objects inside one section.
        # It is safer to reject it than silently creating unexpected slides.
        for node in ast.walk(ast.Module(body=body[1:], type_ignores=[])):
            if isinstance(node, ast.Call) and boundary_call(node):
                raise ValueError(
                    f"section method {name} contains another heading/section boundary"
                )
        groups.append([stmt])  # Execute the ORIGINAL bound method.
        methods.append(method)
    return groups, methods


def _private_helpers(cls, module, methods):
    """Return {name: FunctionDef} for scene-local helpers safe to track per section.

    Conservative: only undecorated, uniquely defined ``_name`` methods of the
    scene class (not dunders) whose name is referenced nowhere except as
    ``self._name`` inside the scene class's own section/helper methods.
    Anything else (string mentions, ``getattr``, other classes, module code,
    construct) keeps the helper in the shared environment.
    """
    section_names = {m.name for m in methods}
    found = {}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, []).append(node)
    helpers = {
        name: defs[0] for name, defs in found.items()
        if name.startswith("_") and not name.startswith("__")
        and name not in section_names and len(defs) == 1
        and isinstance(defs[0], ast.FunctionDef) and not defs[0].decorator_list
    }
    if not helpers:
        return {}
    tracked = set(section_names) | set(helpers)
    tracked_ids = {id(n) for n in cls.body
                   if isinstance(n, ast.FunctionDef) and n.name in tracked}
    allowed = set()  # ids of self._name attribute nodes inside tracked bodies
    for node in cls.body:
        if id(node) in tracked_ids:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load)
                        and isinstance(sub.value, ast.Name) and sub.value.id == "self"):
                    allowed.add(id(sub))
    for sub in ast.walk(module):
        if isinstance(sub, ast.Name) and sub.id in {"getattr", "setattr", "vars", "__dict__"}:
            return {}
        if isinstance(sub, ast.Attribute) and sub.attr == "__dict__":
            return {}
        if isinstance(sub, ast.Attribute) and sub.attr in helpers and id(sub) not in allowed:
            helpers.pop(sub.attr)
        elif isinstance(sub, ast.Name) and sub.id in helpers:
            helpers.pop(sub.id)
        elif (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
              and sub.value in helpers):
            helpers.pop(sub.value)
    return helpers


def _self_refs(func):
    """Names used as self.<name>; None when self escapes (passed or aliased)."""
    refs, attr_values = set(), set()
    for sub in ast.walk(func):
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)                 and sub.value.id == "self":
            refs.add(sub.attr)
            attr_values.add(id(sub.value))
    for sub in ast.walk(func):
        if isinstance(sub, ast.Name) and sub.id == "self" and id(sub) not in attr_values:
            return None
    return refs


def _helper_closure(method, helpers):
    """Helpers transitively reachable from method, or None when unknowable."""
    seen, todo = set(), [method]
    while todo:
        refs = _self_refs(todo.pop())
        if refs is None:
            return None
        for name in sorted(refs & set(helpers) - seen):
            seen.add(name)
            todo.append(helpers[name])
    return seen


def _inline_sections(construct):
    groups = []
    for node in construct.body:
        is_boundary = heading(node) or section_marker(node)
        # Adjacent section() then heading() is one section, as before.
        if is_boundary:
            if not groups or any(not (heading(n) or section_marker(n))
                                 for n in groups[-1]):
                groups.append([])
        elif not groups:
            raise ValueError(
                "construct must start with self.heading(...) or self.section(), "
                "or consist entirely of argument-free self.show_*() calls"
            )
        if any(boundary_call(child) for child in ast.walk(node)
               if isinstance(child, ast.Call) and
               not (is_boundary and child is node.value)):
            raise ValueError("nested or invalid section/heading call")
        if (not is_boundary and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and boundary_call(node.value)):
            raise ValueError("self.section() takes no arguments")
        _unsafe_section_statements([node])
        groups[-1].append(node)
    if len(groups) < 2:
        raise ValueError("need at least two independent sections")
    if all(section_marker(n) for n in groups[-1]):
        raise ValueError("trailing self.section() has no content")
    prior = set()
    for group in groups:
        stored = {n.id for stmt in group for n in ast.walk(stmt)
                  if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        loaded = {n.id for stmt in group for n in ast.walk(stmt)
                  if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        conflict = (loaded & prior) - stored
        if conflict:
            raise ValueError("cross-section locals: " + ", ".join(sorted(conflict)[:5]))
        prior.update(stored)
    return groups


def partition_details(code, scene):
    """Return environment, generated bodies, section hashes, and mode.

    The common environment deliberately excludes the body of construct()
    AND the bodies of selected section methods. Changes to one selected
    method then invalidate only its fingerprint, whereas changes to other
    methods, imports, globals and external Python files invalidate all.
    """
    module = ast.parse(code)
    cls, construct = _module_scene(module, scene)
    helper = _helper_sections(cls, construct)
    if helper is not None:
        groups, methods = helper
        mode = "helper-methods"
        helpers = _private_helpers(cls, module, methods)
        closures = [_helper_closure(method, helpers) for method in methods]
        if any(c is None for c in closures):
            helpers = {}  # self escapes somewhere: keep every helper shared.
            closures = [set() for _ in methods]
        fingerprints = [
            ast.dump(method, include_attributes=False) + "".join(
                "|" + name + "|" + ast.dump(helpers[name], include_attributes=False)
                for name in sorted(closure))
            for method, closure in zip(methods, closures)
        ]
    else:
        groups = _inline_sections(construct)
        methods = []
        mode = "inline"
        fingerprints = [
            ast.dump(ast.Module(body=group, type_ignores=[]),
                     include_attributes=False)
            for group in groups
        ]
    original_body = construct.body
    if mode == "helper-methods":
        # Section-local helper bodies are hashed in their sections' fingerprints;
        # their signatures stay in the shared environment.
        methods = methods + [helpers[name] for name in sorted(helpers)]
    own_bodies = [method.body for method in methods]
    try:
        construct.body = [ast.Pass()]
        for method in methods:
            method.body = [ast.Pass()]
        environment = sha(ast.dump(module, include_attributes=False))
    finally:
        construct.body = original_body
        for method, original in zip(methods, own_bodies):
            method.body = original
    return environment, groups, fingerprints, mode


def partition(code, scene):
    """Retain the original two-value parser interface for existing callers."""
    env, groups, _, _ = partition_details(code, scene)
    return env, groups

def wrapper(source,root,scene,names,groups):
    lines=[
        "# Generated by Manim Slides Preview. Do not edit.",
        "import sys as _sys",
        "from pathlib import Path as _Path",
        "_original_path="+repr(str(source)),
        "_sys.path.insert(0,str(_Path(_original_path).parent))",
        "_sys.path.insert(0,"+repr(str(root))+")",
        "_ns={'__name__':'_msp_original','__file__':_original_path}",
        "exec(compile(_Path(_original_path).read_text(encoding='utf-8-sig'),_original_path,'exec'),_ns)",
        "_base=_ns["+repr(scene)+"]",
    ]
    for i,(name,group) in enumerate(zip(names,groups)):
        method="_msp_construct_"+str(i)
        body=ast.unparse(ast.Module(body=group,type_ignores=[]))
        definition="def "+method+"(self):\n"+"\n".join("    "+line for line in body.splitlines())
        lines.append("exec("+repr(definition)+",_ns)")
        lines.append("globals()["+repr(name)+"]=type("+repr(name)+",(_base,),{'construct':_ns["+repr(method)+"],'__module__':__name__})")
    return "\n".join(lines)+"\n"
def plan(source,root,preview,settings,scene):
    source=Path(source).resolve();root=Path(root).resolve();preview=Path(preview).resolve()
    if not source.is_file() or not root.is_dir() or not source.is_relative_to(root):
        raise ValueError("source must belong to workspace")
    code=source.read_text(encoding="utf-8-sig")
    env,groups,section_fingerprints,mode=partition_details(code,scene)
    deps=dependencies(root,source)
    salt=sha(str(source).lower()+"\0"+scene)[:10]
    safe=re.sub("[^A-Za-z0-9_]","_",scene)
    names=[safe+"_MSP_"+salt+"_"+str(i).zfill(3) for i in range(len(groups))]
    params=sha(json.dumps(settings,sort_keys=True))
    stamps=[sha(json.dumps([VERSION,mode,env,deps,params,fingerprint]))
            for fingerprint in section_fingerprints]
    folder=preview/"cache"/"independent-sections";folder.mkdir(parents=True,exist_ok=True)
    manifest=folder/(salt+".json");candidate=folder/(salt+".pending.json")
    script=folder/(salt+"_sections.py")
    try: old=json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError,ValueError): old={}
    previous=old.get("sections",{}) if old.get("version")==VERSION else {}
    dirty=[];reuse=[];slides=source.parent/"slides"
    for name,stamp in zip(names,stamps):
        assets=slides/"files"/name
        exists=( (slides/(name+".json")).is_file() and assets.is_dir()
                 and any(assets.rglob("*.mp4")) )
        (reuse if exists and previous.get(name)==stamp else dirty).append(name)
    content=wrapper(source,root,scene,names,groups)
    if not script.is_file() or script.read_text(encoding="utf-8")!=content:
        script.write_text(content,encoding="utf-8")
    candidate.write_text(json.dumps({"version":VERSION,"sections":dict(zip(names,stamps))}),
                         encoding="utf-8")
    return {"enabled":True,"mode":mode,"source":str(script),"scenes":names,
            "dirty":dirty,"reused":reuse,"manifest":str(manifest),
            "candidate":str(candidate)}
def main():
    if len(sys.argv)!=6: raise ValueError("usage: SOURCE ROOT PREVIEW SETTINGS_JSON SCENE")
    try:
        print(json.dumps(plan(sys.argv[1],sys.argv[2],sys.argv[3],
                              json.loads(sys.argv[4]),sys.argv[5])))
    except Exception as exc:
        print(json.dumps({"enabled":False,"reason":str(exc)[:240]}))
if __name__=="__main__": main()
