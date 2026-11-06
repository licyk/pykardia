"""Shared WAT inputs for actual Rust/Python guest boundary observations."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def fixtures() -> dict[str, str]:
    """Return malformed metadata and ABI exports without implementing guest host behavior."""
    result: dict[str, str] = {}
    metadata = {
        "null": "null",
        "bool": "true",
        "number": "1",
        "string": '"hello"',
        "empty_array": "[]",
        "struct_array": "[[],null,null]",
        "empty": "{}",
        "deps_null": '{"deps":null}',
        "deps_number": '{"deps":[1]}',
        "plugin_bool": '{"plugin":true}',
        "name_number": '{"plugin":{"name":1}}',
        "inject_string": '{"plugin":{"inject":"x"}}',
        "provide_null": '{"plugin":{"provide":null}}',
        "duplicate": '{"deps":[],"deps":[]}',
        "unknown": '{"something":42}',
        "value_null": '{"value":null}',
        "plugin_array": '{"plugin":[null,[],[]]}',
        "plugin_short_array": '{"plugin":[]}',
        "deps_object": '{"deps":{}}',
        "deps_bool": '{"deps":[false]}',
        "deps_list": '{"deps":[[]]}',
        "deps_map": '{"deps":[{}]}',
        "ordered_errors": '{"plugin":{"inject":5},"deps":null}',
        "trailing": '{"value":1}false',
        "invalid_json": '{"plugin":',
    }
    for name, text in metadata.items():
        data = text.encode("utf-8")
        escaped = "".join(f"\\{byte:02x}" for byte in data)
        result[name] = (
            '(module (import "cordis" "write" (func $write (param i32 i32)))'
            '(memory (export "memory") 1)'
            f'(data (i32.const 0) "{escaped}")'
            '(func (export "cordis_meta") (result i32)'
            f"(call $write (i32.const 0) (i32.const {len(data)})) (i32.const 0)))"
        )
    result.update(
        {
            "missing_meta": "(module)",
            "meta_not_func": '(module (global (export "cordis_meta") i32 (i32.const 0)))',
            "meta_wrong_params": (
                '(module (func (export "cordis_meta") (param i32) (result i32) i32.const 0))'
            ),
            "meta_wrong_results": '(module (func (export "cordis_meta") (result i64) i64.const 0))',
            "dispose_wrong_params": result["empty"][:-1]
            + '(func (export "cordis_dispose") (param i32)))',
            "dispose_wrong_results": result["empty"][:-1]
            + '(func (export "cordis_dispose") (result i32) i32.const 0))',
            "meta_status": '(module (func (export "cordis_meta") (result i32) i32.const 7))',
            "meta_trap": '(module (func (export "cordis_meta") (result i32) unreachable))',
            "no_memory": (
                '(module (import "cordis" "write" (func $w (param i32 i32))) '
                '(func (export "cordis_meta") (result i32) '
                "(call $w (i32.const 0) (i32.const 0)) i32.const 0))"
            ),
            "bounds": (
                '(module (import "cordis" "write" (func $w (param i32 i32))) '
                '(memory (export "memory") 1) (func (export "cordis_meta") (result i32) '
                "(call $w (i32.const -1) (i32.const 1)) i32.const 0))"
            ),
        }
    )
    result.update(malformed_names())
    return result


def malformed_names() -> dict[str, str]:
    """Ask the real host to decode malformed UTF-8 keys during metadata provision."""
    result: dict[str, str] = {}
    for name, key in [("invalid_utf8", b"x\xff"), ("incomplete_utf8", b"x\xe4\xbd")]:
        encoded = "".join(f"\\{byte:02x}" for byte in key)
        result[name] = (
            '(module (import "cordis" "provide" (func $p (param i32 i32 i32 i32) (result i32)))'
            '(import "cordis" "read" (func $r (param i32)))'
            '(import "cordis" "write" (func $w (param i32 i32)))'
            '(memory (export "memory") 1)'
            f'(data (i32.const 0) "{encoded}")(data (i32.const 64) "null")'
            '(func (export "cordis_meta") (result i32) (local $n i32)'
            f"(local.set $n (call $p (i32.const 0) (i32.const {len(key)})"
            "(i32.const 64) (i32.const 4)))"
            "(call $r (i32.const 256))"
            "(call $w (i32.const 256) (i32.sub (i32.const -1) (local.get $n)))"
            "(i32.const 1)))"
        )
    return result


def main() -> None:
    """Regenerate the snapshot by running the compiled original Rust host driver."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        paths: list[str] = []
        for name, source in fixtures().items():
            path = Path(directory) / f"{name}.wat"
            path.write_text(source)
            paths.append(str(path))
        result = subprocess.run(
            [str(args.host.resolve()), *paths], capture_output=True, text=True, check=True
        )
    records = [json.loads(line) for line in result.stdout.splitlines()]
    Path(__file__).with_name("boundaries.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
