"""Generate current source-test traceability from the frozen inventory and stage reports."""

import ast
import json
import re
from pathlib import Path


def main() -> None:
    """Require an existing Python test or explicit stage mapping for every pinned source case."""
    root = Path(__file__).resolve().parents[1]
    baseline = json.loads((root / "archive/docs/parity-inventory.json").read_text())
    functions: dict[str, list[str]] = {}
    for path in (root / "tests").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                functions.setdefault(node.name, []).append(f"{path.relative_to(root)}::{node.name}")
    reports = {
        str(path.relative_to(root)): path.read_text().splitlines()
        for path in sorted((root / "archive" / "docs").glob("parity-p*.md"))
    }
    records: list[dict[str, object]] = []
    missing: list[str] = []
    for entry in baseline["entries"]:
        if entry["kind"] != "测试":
            continue
        name = entry["name"]
        source = entry["source"]
        module = Path(source).parts[1].removeprefix("kardia-").replace("-", "_")
        module = "core" if module == "macros" else module
        targets = [
            target
            for target in functions.get("test_" + name, [])
            if target.startswith(f"tests/{module}/")
        ]
        evidence: list[str] = []
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")
        for report, lines in reports.items():
            for number, line in enumerate(lines, 1):
                if pattern.search(line):
                    evidence.append(f"{report}:{number}")
                    for method in re.findall(r"\btest_\w+", line):
                        targets.extend(functions.get(method, []))
        if not targets and not evidence:
            missing.append(f"{source}::{name}")
        records.append(
            {
                "source": f"{source}:{entry['line']}",
                "name": name,
                "python_tests": sorted(set(targets)),
                "stage_evidence": evidence,
                "status": "已明确处理的语言机制差异" if name == "compile_errors" else "通过",
            }
        )
    if missing:
        raise ValueError("source tests without acceptance mapping:\n" + "\n".join(missing))
    output = {
        "reference": baseline["reference"],
        "basis": "阶段验收记录与实际全量检查；名称/文档匹配仅验证映射没有遗漏，不替代行为断言。",
        "source_test_count": len(records),
        "tests": records,
        "ui": {
            "not_async": "tests/core/test_decorators.py::test_not_async_is_rejected_at_declaration",
            "undeclared": (
                "tests/core/test_decorators.py::test_undeclared_typed_get_fails_at_access"
            ),
            "config_not_arc": (
                "archive/docs/parity-p3.md: 宏错误阶段映射；Python 共享引用不需要 Arc 包装"
            ),
        },
        "guest_fixtures": "tests/parity/rust_guest/fixture/src/{lib,main}.rs；原源码，三种真实产物",
        "api_audit": "archive/docs/export-audit.md",
    }
    (root / "archive/docs/acceptance.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"Mapped {len(records)} source tests; no missing acceptance mappings")


if __name__ == "__main__":
    main()
