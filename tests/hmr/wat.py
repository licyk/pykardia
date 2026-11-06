"""WAT fixture construction matching the original host acceptance scenarios."""

import json

from pykardia import JsonValue


def wat(meta: JsonValue, strings: list[str], body: str) -> str:
    """Embed metadata and ABI buffers, replacing @N with each buffer's pointer and length."""
    offset = 0
    data: list[str] = []

    def place(text: str) -> str:
        """Encode bytes into one data segment and return its actual ABI coordinates."""
        nonlocal offset
        encoded = text.encode("utf-8")
        escaped = "".join(f"\\{byte:02x}" for byte in encoded)
        data.append(f'(data (i32.const {offset}) "{escaped}")')
        result = f"(i32.const {offset}) (i32.const {len(encoded)})"
        offset += len(encoded)
        return result

    metadata = place(json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    for index, text in enumerate(strings):
        body = body.replace(f"@{index}", place(text))
    return (
        """(module
(import "cordis" "read" (func $read (param i32)))
(import "cordis" "write" (func $write (param i32 i32)))
(import "cordis" "log" (func $log (param i32 i32 i32)))
(import "cordis" "provide" (func $provide (param i32 i32 i32 i32) (result i32)))
(import "cordis" "view" (func $view (param i32 i32) (result i32)))
(import "cordis" "invoke" (func $invoke (param i32 i32 i32 i32 i32 i32) (result i32)))
(memory (export "memory") 1)
"""
        + "\n".join(data)
        + f"""
(func (export "cordis_meta") (result i32) (call $write {metadata}) (i32.const 0))
{body})"""
    )


def greeter(version: str, meta: JsonValue = None) -> str:
    """Build a guest exposing its apply input, version and verbatim request replies."""
    if meta is None:
        meta = {"plugin": {"name": "greeter", "provide": ["greeter", "version"]}}
    return wat(
        meta,
        [
            "greeter",
            "version",
            json.dumps(version),
            f"greeter {version} applied",
            f"greeter {version} disposed",
        ],
        """
(func (export "cordis_apply") (param $len i32) (result i32)
  (call $read (i32.const 1024))
  (drop (call $provide @0 (i32.const 1024) (local.get $len)))
  (drop (call $provide @1 @2))
  (call $log (i32.const 2) @3)
  (i32.const 0))
(func (export "cordis_call") (param $len i32) (result i32)
  (call $read (i32.const 1024))
  (call $write (i32.const 1024) (local.get $len))
  (i32.const 0))
(func (export "cordis_dispose") (call $log (i32.const 2) @4))""",
    )


def forwarder(meta: JsonValue, call: str) -> str:
    """Build a guest forwarding a host call result into a provided copy service."""
    body = """
(func (export "cordis_apply") (param $len i32) (result i32)
  (local $result i32)
  (local.set $result CALL)
  (if (i32.lt_s (local.get $result) (i32.const 0))
    (then
      (call $read (i32.const 1024))
      (call $write (i32.const 1024) (i32.sub (i32.const -1) (local.get $result)))
      (return (i32.const 1))))
  (call $read (i32.const 1024))
  (drop (call $provide @2 (i32.const 1024) (local.get $result)))
  (i32.const 0))"""
    return wat(
        meta, ["source", "greeter", "copy", "hello", "[2]", "self"], body.replace("CALL", call)
    )
