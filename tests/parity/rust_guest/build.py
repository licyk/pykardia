"""Build the pinned original guest source as real transport acceptance artifacts."""

import functools
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

Kind = Literal["process", "dylib", "wasm", "v1"]


@functools.cache
def artifact(kind: Kind, version: str = "v1") -> Path:
    """Build one version/transport and keep a separate hard link across subsequent builds."""
    manifest = Path(__file__).parent / "fixture" / "Cargo.toml"
    target = Path(tempfile.gettempdir()) / "pykardia-rust-guest"
    command = [
        "cargo",
        "+1.96.0",
        "build",
        "--quiet",
        "--locked",
        "--manifest-path",
        str(manifest),
        "--target-dir",
        str(target),
    ]
    if kind in ("process", "v1"):
        binary = "rust-guest-process" if kind == "process" else "rust-guest-v1"
        command.extend(["--bin", binary])
        output = target / "debug" / (f"{binary}.exe" if sys.platform == "win32" else binary)
    elif kind == "wasm":
        command.extend(["--lib", "--target", "wasm32-unknown-unknown"])
        output = target / "wasm32-unknown-unknown" / "debug" / "rust_guest.wasm"
    else:
        command.append("--lib")
        name = (
            "rust_guest.dll"
            if sys.platform == "win32"
            else "librust_guest.dylib"
            if sys.platform == "darwin"
            else "librust_guest.so"
        )
        output = target / "debug" / name
    if version in ("v2", "kardia-names"):
        command.extend(["--features", version])
    subprocess.run(command, check=True)
    saved = target / f"{version}-{kind}-{output.name}"
    saved.unlink(missing_ok=True)
    os.link(output, saved)
    return saved
