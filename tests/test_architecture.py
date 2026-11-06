"""Enforce entry-point structure and handwritten function documentation."""

import ast
import unittest
from pathlib import Path


class ArchitectureTests(unittest.TestCase):
    """Check the repository's actual Python source files."""

    def test_entry_points_only_reexport(self) -> None:
        """Allow imports and explicit export declarations while keeping implementation elsewhere."""
        root = Path(__file__).resolve().parents[1] / "pykardia"
        for path in root.rglob("__init__.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                with self.subTest(path=path, line=node.lineno):
                    is_doc = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    is_exports = (
                        isinstance(node, ast.AnnAssign)
                        and isinstance(node.target, ast.Name)
                        and node.target.id == "__all__"
                    )
                    self.assertTrue(
                        is_doc or is_exports or isinstance(node, (ast.Import, ast.ImportFrom))
                    )

    def test_functions_have_annotations_and_docstrings(self) -> None:
        """Include private helpers and test functions in documentation checks."""
        root = Path(__file__).resolve().parents[1]
        for directory in ("pykardia", "tests", "examples", "scripts"):
            for path in (root / directory).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    with self.subTest(path=path.relative_to(root), function=node.name):
                        self.assertTrue(ast.get_docstring(node), "missing docstring")
                        self.assertIsNotNone(node.returns, "missing return annotation")
                        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                        arguments.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg)
                        for arg in arguments:
                            if arg.arg not in {"self", "cls"}:
                                self.assertIsNotNone(arg.annotation, f"missing type for {arg.arg}")
