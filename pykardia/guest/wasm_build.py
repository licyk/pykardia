"""Compile bundled Python guests into the original bare-WASM ABI."""

import json
import marshal
import subprocess
import sys
import tempfile
from pathlib import Path

import wasmtime

from pykardia.guest.export import sources

STDLIB = (
    "encodings",
    "encodings.aliases",
    "encodings.utf_8",
    "encodings.ascii",
    "types",
    "operator",
    "_collections_abc",
    "keyword",
    "reprlib",
    "collections",
    "functools",
    "enum",
    "re._constants",
    "re._parser",
    "re._casefix",
    "re._compiler",
    "copyreg",
    "re",
    "json.scanner",
    "json.decoder",
    "json.encoder",
    "json",
    "dataclasses",
    "inspect",
    "ast",
    "dis",
    "opcode",
    "tokenize",
    "token",
    "linecache",
    "copy",
    "weakref",
    "_weakrefset",
    "typing",
    "collections.abc",
    "contextlib",
    "decimal",
    "_pydecimal",
    "numbers",
    "__future__",
    "importlib",
    "importlib._abc",
    "importlib.util",
    "importlib.machinery",
    "warnings",
)


def export_wasm(
    source: Path,
    guest: str,
    output: Path,
    python_source: Path,
    python_build: Path,
    wasi_sdk: Path,
    modules: tuple[str, ...] = (),
    module_root: Path | None = None,
) -> None:
    """Freeze the SDK and guest, then link a CPython 3.11 bare-WASM reactor.

    Args:
        source: Guest implementation module.
        guest: Guest class name in that module.
        output: Destination WASM file.
        python_source: CPython 3.11 source tree, including Lib and Include.
        python_build: Matching WASI build containing static Python libraries.
        wasi_sdk: WASI SDK installation root.
        modules: Additional pure-Python standard-library modules to freeze.
        module_root: Directory containing additional user modules and packages.

    No WASI imports are added to the host. Filesystem, clock, entropy and
    networking functions remain unavailable, just as in the original bare ABI.
    """
    if sys.version_info[:2] != (3, 11):
        raise ValueError("frozen bytecode must be generated with CPython 3.11")
    files = sources(source, "_pykardia_user", module_root)
    files["LICENSES/CPython-LICENSE"] = (python_source / "LICENSE").read_bytes()
    files["LICENSES/CPython-third-party"] = (python_source / "Doc" / "license.rst").read_bytes()
    files["_pykardia_entry.py"] = (
        "from _pykardia_user import " + guest + "\n"
        "from pykardia.guest.runtime import Runtime\n"
        "from pykardia.guest.abi import AbiHost\n"
        "import _pykardia_abi\n"
        "runtime = Runtime(" + guest + ")\n"
        "host = AbiHost(_pykardia_abi)\n"
        "def dispatch(export: str, data: bytes) -> tuple[int, bytes | None]:\n"
        '    """Dispatch one export with WASM-specific state replacement."""\n'
        "    if export == 'apply':\n"
        "        runtime.instance = None\n"
        "    return runtime.dispatch(export, host, data)\n"
    ).encode()
    for name in dict.fromkeys((*STDLIB, *modules)):
        relative = name.replace(".", "/") + ".py"
        path = python_source / "Lib" / relative
        if not path.is_file():
            relative = name.replace(".", "/") + "/__init__.py"
            path = python_source / "Lib" / relative
        files[relative] = path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="pykardia-wasm-") as directory:
        workdir = Path(directory)
        freeze(files, workdir / "frozen.h")
        bridge = Path(__file__).with_name("templates") / "wasm.c"
        preliminary = workdir / "with-wasi.wasm"
        link(wasi_sdk, python_source, python_build, workdir, bridge, preliminary)
        engine = wasmtime.Engine()
        module = wasmtime.Module.from_file(engine, str(preliminary))
        shim = workdir / "wasi_shim.c"
        generate_shim(module, shim)
        link(wasi_sdk, python_source, python_build, workdir, bridge, output, shim)
        notices = {
            name: text.decode("utf-8")
            for name, text in files.items()
            if name.startswith("LICENSES/")
        }
        name = b"pykardia.licenses"
        payload = leb128(len(name)) + name + json.dumps(notices, ensure_ascii=False).encode("utf-8")
        with output.open("ab") as stream:
            stream.write(b"\x00" + leb128(len(payload)) + payload)
        compiled = wasmtime.Module.from_file(engine, str(output))
        imports = [(item.module, item.name) for item in compiled.imports]
        if any(namespace != "cordis" for namespace, _ in imports):
            raise RuntimeError(f"unexpected imports: {imports}")


def freeze(files: dict[str, bytes], output: Path) -> None:
    """Generate CPython's frozen module table from the actual packaged sources."""
    declarations = ['#include "Python.h"']
    entries: list[str] = []
    for index, (path, source) in enumerate(files.items()):
        if not path.endswith(".py"):
            continue
        package = path.endswith("/__init__.py")
        name = path.removesuffix("/__init__.py") if package else path.removesuffix(".py")
        name = name.replace("/", ".")
        data = marshal.dumps(compile(source, f"<frozen {name}>", "exec"))
        declarations.append(
            f"static unsigned char module_{index}[]={{" + ",".join(map(str, data)) + "};"
        )
        entries.append(f'{{"{name}",module_{index},{len(data)},{int(package)},NULL}}')
    declarations.append(
        "static const struct _frozen modules[]={" + ",".join(entries) + ",{NULL,NULL,0,0,NULL}};"
    )
    output.write_text("\n".join(declarations), encoding="utf-8")


def leb128(value: int) -> bytes:
    """Encode an unsigned custom-section length in the WASM binary format."""
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def link(
    sdk: Path,
    source: Path,
    build: Path,
    workdir: Path,
    bridge: Path,
    output: Path,
    shim: Path | None = None,
) -> None:
    """Link the SDK and CPython's static libraries as a core WASM reactor."""
    command = [
        str(sdk / "bin/clang"),
        f"--sysroot={sdk / 'share/wasi-sysroot'}",
        f"-I{workdir}",
        f"-I{build}",
        f"-I{source / 'Include'}",
        "-O2",
        "-mexec-model=reactor",
        "-Wl,-z,stack-size=8388608",
        "-Wl,--strip-debug",
        str(bridge),
    ]
    if shim is not None:
        command.append(str(shim))
    command.extend(
        [
            str(build / "libpython3.11.a"),
            str(build / "Modules/_decimal/libmpdec/libmpdec.a"),
            str(build / "Modules/expat/libexpat.a"),
            "-ldl",
            "-lwasi-emulated-getpid",
            "-lwasi-emulated-signal",
            "-lwasi-emulated-mman",
            "-lwasi-emulated-process-clocks",
            "-lm",
            "-o",
            str(output),
        ]
    )
    subprocess.run(command, check=True)


def generate_shim(module: wasmtime.Module, path: Path) -> None:
    """Provide empty environment/stdio glue and fail unavailable OS calls with ENOSYS.

    These definitions live inside the guest. No WASI capability is added to the host.
    The guest does not have filesystem, clock, networking or entropy support.
    """
    definitions = [
        "#include <stdint.h>",
        '__attribute__((import_module("cordis"),import_name("log"))) '
        "extern void host_log(int,const char*,int);",
    ]
    implementations = {
        "environ_sizes_get": "*(int*)p0=0; *(int*)p1=0; return 0;",
        "environ_get": "return 0;",
        "fd_prestat_get": "return 8;",
        "fd_fdstat_get": "return 8;",
        "fd_write": (
            "if(p0!=1 && p0!=2) return 8; "
            "uint32_t *iov=(uint32_t*)p1; uint32_t length=0; "
            "for(int i=0;i<p2;i++){host_log(0,(char*)iov[2*i],iov[2*i+1]); "
            "length+=iov[2*i+1];} *(uint32_t*)p3=length; return 0;"
        ),
        "proc_exit": "__builtin_trap();",
    }
    for item in module.imports:
        if item.module != "wasi_snapshot_preview1":
            continue
        signature = item.type
        if not isinstance(signature, wasmtime.FuncType):
            raise RuntimeError(f"unsupported WASI import: {item.name}")
        result = "int32_t" if signature.results else "void"
        arguments = ",".join(
            f"{'int64_t' if str(kind) == 'i64' else 'int32_t'} p{index}"
            for index, kind in enumerate(signature.params)
        )
        body = implementations.get(item.name, "return 52;")
        definitions.append(
            f"{result} __imported_wasi_snapshot_preview1_{item.name}"
            f"({arguments or 'void'}){{{body}}}"
        )
    path.write_text("\n".join(definitions), encoding="utf-8")
