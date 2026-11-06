"""Generate the pinned source declaration and test traceability inventory."""

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

REFERENCE = "0ce472c391b6a8bf4fafa443000348e9e48af44a"
PUBLIC = re.compile(r"^\s*pub (?!use |mod )(?:(?:async|unsafe|extern \"C\") )*")
NAMED = re.compile(r"\b(?:fn|struct|enum|trait|type|const|macro_rules!)\s+(\w+)")
TEST = re.compile(r"#\[(?:tokio::)?test(?:\([^\n]*\))?\]\s*(?:async\s+)?fn\s+(\w+)")


@dataclass
class Entry:
    """A source declaration or test with its proposed Python destination."""

    kind: str
    source: str
    line: int
    name: str
    declaration: str
    contract: str
    destination: str
    status: str = "未实现"


def main() -> None:
    """Read only the pinned Git tree and regenerate its inventory documents."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    repository: Path = args.reference
    names = subprocess.check_output(
        ["git", "-C", str(repository), "ls-tree", "-r", "--name-only", REFERENCE, "crates"],
        text=True,
    ).splitlines()
    entries: list[Entry] = []
    hashes: dict[str, str] = {}
    for name in names:
        if not name.endswith(".rs"):
            continue
        source = subprocess.check_output(
            ["git", "-C", str(repository), "show", f"{REFERENCE}:{name}"], text=True
        )
        hashes[name] = hashlib.sha256(source.encode()).hexdigest()
        entries.extend(extract(name, source))
    entries.extend(generated_api())
    output = root / "archive" / "docs"
    output.mkdir(exist_ok=True)
    manifest = {
        "inventory_kind": "frozen_planning_baseline",
        "reference": REFERENCE,
        "source_sha256": hashes,
        "entries": [asdict(e) for e in entries],
    }
    (output / "parity-inventory.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "parity.md").write_text(render(entries), encoding="utf-8")
    for kind in ("API", "测试", "UI", "fixture", "导出"):
        print(f"{kind}: {sum(e.kind == kind for e in entries)}")


def extract(path: str, source: str) -> list[Entry]:
    """Collect declarations, trait members, test cases and transport fixtures.

    Args:
        path: File path inside the fixed reference tree.
        source: Unmodified Rust source at the reference commit.

    Returns:
        Conservative declaration inventory, including public implementation details.
    """
    parts = Path(path).parts
    module = parts[1].removeprefix("kardia-").replace("-", "_")
    module = "" if module == "kardia" else module
    module = "decorators" if module == "macros" else module
    namespace = ".".join(part for part in ("pykardia", module) if part)
    stem = Path(path).stem
    test_module = module or "facade"
    destination = f"tests/{test_module}/test_{stem}.py"
    result: list[Entry] = []
    for match in TEST.finditer(source):
        name = match.group(1)
        line = source.count("\n", 0, match.start()) + 1
        result.append(
            Entry(
                "测试",
                path,
                line,
                name,
                "",
                "原函数的输入、操作和断言",
                f"{destination}::test_{name}",
            )
        )
    if "/ui/" in path or "/fixtures/" in path:
        kind = "UI" if "/ui/" in path else "fixture"
        result.append(Entry(kind, path, 1, stem, "", "编译错误或真实客体构建输入", destination))
        return result
    if parts[2] != "src":
        return result
    scope: list[tuple[int, str, str]] = []
    comments: list[str] = []
    lines = source.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if "$" in stripped:
            continue
        if stripped.startswith("///"):
            comments.append(stripped.removeprefix("///").strip())
            continue
        if stripped.startswith("}"):
            scope = [item for item in scope if item[0] < indent]
        owner = scope[-1][1] if scope else ""
        category = scope[-1][2] if scope else ""
        member = category == "trait" and bool(re.match(r"(?:async )?(?:fn|type|const) ", stripped))
        variant = (
            category == "enum"
            and indent == scope[-1][0] + 4
            and bool(re.match(r"[A-Z]\w*(?:\s*[,({=]|$)", stripped))
        )
        if PUBLIC.match(line) or member or variant:
            named = NAMED.search(line)
            name = named.group(1) if named else stripped.removeprefix("pub ").split(":")[0]
            name = name.split("(")[0].split("{")[0].split(",")[0].split("=")[0].strip()
            qualified = f"{owner}.{name}" if owner else name
            contract = " ".join(comments) or "按固定源码及同模块测试核对"
            result.append(
                Entry(
                    "API",
                    path,
                    index + 1,
                    qualified,
                    stripped,
                    contract,
                    f"{namespace}.{qualified}",
                )
            )
        if stripped.startswith("pub use ") or stripped.startswith("pub mod "):
            result.append(
                Entry("导出", path, index + 1, stripped, stripped, "入口重导出关系", namespace)
            )
        declaration = re.match(r"(?:pub )?(struct|enum|trait)\s+(\w+)", stripped)
        implementation = re.match(r"impl(?:<.*>)?\s+(.+?)\s*\{$", stripped)
        if declaration and "{" in stripped:
            scope.append((indent, declaration.group(2), declaration.group(1)))
        elif implementation:
            target = implementation.group(1).split(" for ")[-1].split("<")[0]
            scope.append((indent, target, "impl"))
        if not stripped.startswith("#["):
            comments.clear()
    return result


def generated_api() -> list[Entry]:
    """Account explicitly for public APIs generated by the reference macros."""
    result: list[Entry] = []
    for name in ("FiberId", "ImplId", "FiberId.get", "ImplId.get"):
        result.append(
            Entry(
                "API",
                "crates/kardia-core/src/key.rs",
                90,
                name,
                "id_type!",
                "新鲜数值身份；get 返回数值",
                f"pykardia.core.{name}",
            )
        )
    result.append(
        Entry(
            "API",
            "crates/kardia-guest/src/export.rs",
            6,
            "export!",
            "macro_rules! export",
            "WASM/C ABI 导出生成；语言边界待处理",
            "pykardia.guest.export",
        )
    )
    return result


def render(entries: list[Entry]) -> str:
    """Render a source-linked checklist without implying implementations exist."""
    text = [
        "# Rust / Python 行为追踪清单",
        "",
        "由 `scripts/parity.py` 生成；不要手改本文件或 `parity-inventory.json`。",
        "",
        f"固定参考提交：`{REFERENCE}`。使用 `python scripts/parity.py /path/to/cordis-rs` 重建。",
        "读取固定 Git 对象，不受参考仓库后续修改影响；JSON 清单保存每个 Rust 文件的 SHA-256。",
        "",
        "这是 P0 冻结的计划清单；Python 模块前缀随包名更新，状态保留建立基准时的值。",
        "当前源测试映射见 acceptance.json，实施/API 验收见 parity-p1.md 至 parity-p10.md。",
        "测试输入、操作、预期结果以链接到的固定源码函数为准；实施时移植相同断言。",
        "API 清单保守包含公开声明和公开 trait 成员，也保留内部模块的 pub 项供审核；",
        "是否属于顶层用户 API 由导出关系及 archive/docs/api.md 决定。Rust 标准 trait 实现按",
        "Python 协议映射；宏生成的 ID API 另行登记，依赖元组展开由 Deps/Injected 条目涵盖。",
        "模块源码中的声明行不是完整跨行签名，准确参数及泛型约束需打开源码。",
        "",
        "进度与语言差异记录在 archive/todo.md、archive/docs/api.md 和 "
        "archive/docs/decisions.md；生成清单保持基准不变。",
        "",
    ]
    for kind in ("API", "导出", "测试", "UI", "fixture"):
        selected = [entry for entry in entries if entry.kind == kind]
        text.extend(
            [
                f"## {kind}（{len(selected)} 项）",
                "",
                "| 源码 | 符号 / 用例 | Python 位置 | 约束 / 验证 | 状态 |",
                "|---|---|---|---|---|",
            ]
        )
        for entry in selected:
            url = (
                f"https://github.com/licyk/cordis-rs/blob/{REFERENCE}/{entry.source}#L{entry.line}"
            )
            values = [
                f"[{entry.source}:{entry.line}]({url})",
                f"`{entry.name}`",
                f"`{entry.destination}`",
                entry.contract,
                entry.status,
            ]
            text.append("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
        text.append("")
    return "\n".join(text)


if __name__ == "__main__":
    main()
