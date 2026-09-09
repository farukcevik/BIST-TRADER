"""Dependency-free static checks used by local and hosted CI."""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def configured_paths() -> list[Path]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    values = config["tool"]["bistbot_ci"]["python_paths"]
    return [ROOT / value for value in values]


def python_files(paths: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():
            files.update(path.rglob("*.py"))
        elif path.suffix == ".py":
            files.add(path)
    return sorted(files)


class QualityVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[str] = []

    def report(self, node: ast.AST, message: str) -> None:
        relative = self.path.relative_to(ROOT)
        self.errors.append(f"{relative}:{node.lineno}: {message}")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if any(alias.name == "*" for alias in node.names):
            self.report(node, "wildcard imports are prohibited")
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.type is None:
            self.report(node, "bare except clauses are prohibited")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if isinstance(node.func, ast.Name) and node.func.id == "breakpoint":
            self.report(node, "debug breakpoint must not be committed")
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_trace"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pdb"
        ):
            self.report(node, "pdb.set_trace must not be committed")
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:  # noqa: N802
        literal_keys: set[object] = set()
        for key in node.keys:
            if not isinstance(key, ast.Constant):
                continue
            try:
                duplicate = key.value in literal_keys
                literal_keys.add(key.value)
            except TypeError:
                continue
            if duplicate:
                self.report(key, f"duplicate dictionary key {key.value!r}")
        self.generic_visit(node)


def check(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return [f"{path.relative_to(ROOT)}: cannot parse: {exc}"]
    visitor = QualityVisitor(path)
    visitor.visit(tree)
    return visitor.errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = [path.resolve() for path in args.paths] if args.paths else configured_paths()
    files = python_files(paths)
    errors = [error for path in files for error in check(path)]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Static checks passed for {len(files)} Python files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
