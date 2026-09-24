"""Per-file symbol extraction: what a file defines (with signatures) and what it references.

Python uses the stdlib `ast` module and is precise. JavaScript/TypeScript use conservative regexes:
good enough to spot "this function's parameter list changed" and "this line calls that function",
which is all tangle needs. Unsupported languages simply yield no symbols (tangle still reports
textual conflicts and hotspot overlaps for them).
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field

PY_EXT = (".py", ".pyi")
JS_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")


@dataclass(frozen=True)
class Definition:
    name: str  # short name, what call sites use
    qualname: str  # e.g. Cart.total
    kind: str  # function | method | class | variable | type
    signature: str | None  # None = not signature-bearing
    line: int
    body_hash: str


@dataclass(frozen=True)
class Reference:
    name: str
    line: int
    strength: str  # "direct" (call/import/bare name) or "attribute" (obj.name)


@dataclass
class FileSymbols:
    defs: dict[str, Definition] = field(default_factory=dict)  # keyed by qualname
    refs: list[Reference] = field(default_factory=list)
    parsed: bool = True


def language_of(path: str) -> str | None:
    lower = path.lower()
    if lower.endswith(PY_EXT):
        return "python"
    if lower.endswith(JS_EXT):
        return "js"
    return None


def extract(path: str, source: str | None) -> FileSymbols:
    if source is None:
        return FileSymbols()
    lang = language_of(path)
    if lang == "python":
        return _extract_python(source)
    if lang == "js":
        return _extract_js(source)
    return FileSymbols(parsed=False)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


# ---------------------------------------------------------------------------- python


def _py_signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        sig = f"({ast.unparse(node.args)})"
        if node.returns is not None:
            sig += f" -> {ast.unparse(node.returns)}"
        return sig
    if isinstance(node, ast.ClassDef):
        init = next(
            (n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__init__"),
            None,
        )
        return _py_signature(init) if init is not None else "()"
    return ""


def _extract_python(source: str) -> FileSymbols:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return FileSymbols(parsed=False)

    result = FileSymbols()

    def add_def(node: ast.AST, qual: str, kind: str, signature: str | None) -> None:
        name = qual.rsplit(".", 1)[-1]
        result.defs[qual] = Definition(name, qual, kind, signature, node.lineno, _digest(ast.dump(node)))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_def(node, node.name, "function", _py_signature(node))
        elif isinstance(node, ast.ClassDef):
            add_def(node, node.name, "class", _py_signature(node))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add_def(item, f"{node.name}.{item.name}", "method", _py_signature(item))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    add_def(node, t.id, "variable", None)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            result.refs.append(Reference(node.id, node.lineno, "direct"))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            result.refs.append(Reference(node.attr, node.lineno, "attribute"))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                result.refs.append(Reference(alias.name, node.lineno, "direct"))
    return result


# ---------------------------------------------------------------------------- js / ts

_IDENT = r"[A-Za-z_$][\w$]*"
_JS_FUNC = re.compile(
    rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*({_IDENT})\s*(?:<[^>]*>)?\s*\(([^)]*)\)"
)
_JS_ARROW = re.compile(
    rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_IDENT})\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\(([^)]*)\)|({_IDENT}))\s*(?::[^=]+)?=>"
)
_JS_CLASS = re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+({_IDENT})")
_JS_TYPE = re.compile(rf"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+({_IDENT})")
_JS_METHOD = re.compile(rf"^\s+(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*({_IDENT})\s*\(([^)]*)\)\s*(?::[^{{]+)?\{{")
_JS_STRINGS = re.compile(r"""(["'`])(?:\\.|(?!\1).)*\1""")
_JS_KEYWORDS = frozenset(
    "if else for while do switch case break continue return function const let var class new this "
    "super extends import export from default async await try catch finally throw typeof instanceof "
    "in of void delete yield true false null undefined interface type enum implements public private "
    "protected static readonly as declare abstract".split()
)


def _norm_params(params: str) -> str:
    return "(" + ", ".join(p.strip() for p in params.split(",") if p.strip()) + ")"


def _extract_js(source: str) -> FileSymbols:
    result = FileSymbols()
    lines = source.splitlines()
    starts: list[tuple[int, str, str, str | None]] = []  # (line, qual, kind, signature)
    current_class: str | None = None
    depth_at_class = 0
    depth = 0

    for idx, raw in enumerate(lines, start=1):
        # Definitions are matched on the raw line (default values may be strings); references and brace
        # depth on a copy with string literals blanked and comments removed.
        line = _JS_STRINGS.sub('""', raw).split("//", 1)[0]
        if m := _JS_FUNC.match(raw):
            starts.append((idx, m.group(1), "function", _norm_params(m.group(2))))
        elif m := _JS_ARROW.match(raw):
            params = m.group(2) if m.group(2) is not None else m.group(3)
            starts.append((idx, m.group(1), "function", _norm_params(params)))
        elif m := _JS_CLASS.match(raw):
            starts.append((idx, m.group(1), "class", None))
            current_class, depth_at_class = m.group(1), depth
        elif m := _JS_TYPE.match(raw):
            starts.append((idx, m.group(1), "type", None))
        elif current_class and (m := _JS_METHOD.match(raw)) and m.group(1) not in _JS_KEYWORDS:
            starts.append((idx, f"{current_class}.{m.group(1)}", "method", _norm_params(m.group(2))))

        depth += line.count("{") - line.count("}")
        if current_class and depth <= depth_at_class and "}" in line:
            current_class = None

        for tok in re.finditer(rf"(\.)?\b({_IDENT})\b", line):
            name = tok.group(2)
            if name in _JS_KEYWORDS or name[0].isdigit():
                continue
            result.refs.append(Reference(name, idx, "attribute" if tok.group(1) else "direct"))

    for i, (line_no, qual, kind, sig) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
        body = "\n".join(l.strip() for l in lines[line_no - 1 : end] if l.strip())
        name = qual.rsplit(".", 1)[-1]
        result.defs[qual] = Definition(name, qual, kind, sig, line_no, _digest(body))

    # A definition line is not a reference to itself.
    def_lines = {d.line: d.name for d in result.defs.values()}
    result.refs = [r for r in result.refs if def_lines.get(r.line) != r.name]
    return result
