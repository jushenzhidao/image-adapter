"""AST security scanner for adaptation scripts.

Scripts now arrive as in-memory text from a channel header, so this scan is
the only gate between a caller and the interpreter. Policy:

  1. Imports must be in ALLOWED_STDLIB. No adapter internals, no aiohttp,
     no redis, no minio: infra reaches scripts only through ctx.
  2. Introspection and dynamic-execution builtins are refused.
  3. Dunder attribute access is refused, which closes the
     ().__class__.__bases__ sandbox-escape family.

Subscript and Await are allowed on purpose: scripts are async and work on
dicts.
"""

from __future__ import annotations

import ast

from adapter.errors import SecurityError

ALLOWED_STDLIB = frozenset(
    {
        "json",
        "re",
        "base64",
        "binascii",
        "datetime",
        "time",
        "math",
        "random",
        "string",
        "typing",
        "collections",
        "collections.abc",
        "itertools",
        "functools",
        "hashlib",
        "hmac",
        "uuid",
        "urllib.parse",
        "textwrap",
        "decimal",
        "enum",
        "dataclasses",
        "copy",
    }
)

FORBIDDEN_NAMES = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "__import__",
        "open",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "input",
        "breakpoint",
        "memoryview",
        "help",
        "exit",
        "quit",
    }
)


def _import_allowed(module_name: str) -> bool:
    """Only fully qualified whitelist entries pass; bare urllib does not."""
    return module_name in ALLOWED_STDLIB


class SandboxScanner(ast.NodeVisitor):
    """Raises SecurityError on the first policy violation."""

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not _import_allowed(alias.name):
                raise SecurityError(f"Forbidden import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level != 0:
            raise SecurityError("Relative imports are not allowed in scripts")
        if not _import_allowed(module):
            raise SecurityError(f"Forbidden import from: {module or '<unknown>'}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__") and node.attr.endswith("__"):
            raise SecurityError(f"Forbidden dunder attribute access: {node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # Covers both eval(...) and the aliasing dodge f = eval.
        if node.id in FORBIDDEN_NAMES:
            raise SecurityError(f"Forbidden name reference: {node.id}")
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        raise SecurityError("global statements are not allowed in scripts")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        raise SecurityError("nonlocal statements are not allowed in scripts")


def scan_source(source: str, filename: str = "<script>") -> ast.Module:
    """Parses and scans script text. Returns the AST when the policy holds."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise SecurityError(
            f"Script has a syntax error: {exc.msg} (line {exc.lineno})"
        ) from exc
    SandboxScanner().visit(tree)
    return tree
