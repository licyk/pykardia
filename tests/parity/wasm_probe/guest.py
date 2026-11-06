"""Actual CPython code frozen into the bare-WASM feasibility fixture."""

import json

count: int = 0


def meta(message: bytes) -> bytes:
    """Describe the service that the fixture exposes through the original ABI."""
    return json.dumps(
        {"deps": [], "plugin": {"name": "python-probe", "inject": [], "provide": ["counter"]}},
        separators=(",", ":"),
    ).encode()


def apply(message: bytes) -> bytes:
    """Initialize this instance and return its service's JSON view."""
    global count
    value = json.loads(message)
    count = int(value["config"]["initial"])
    return json.dumps({"initial": count, "language": "Python"}, separators=(",", ":")).encode()


def call(message: bytes) -> bytes:
    """Update instance-local state, returning UTF-8 JSON or a real Python exception."""
    global count
    request = json.loads(message)
    if request["method"] != "increment":
        raise ValueError(f"counter has no method {request['method']}")
    count += int(request["args"])
    return json.dumps({"count": count, "text": "你好"}, ensure_ascii=False).encode()


def dispose(message: bytes) -> bytes:
    """Report that the original Rust host reached Python cleanup."""
    return b"Python guest disposed"
