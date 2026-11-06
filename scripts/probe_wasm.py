"""Build the CPython bare-WASM feasibility fixture against the original guest ABI."""

import argparse
import marshal
import subprocess
import sys
from pathlib import Path

import wasmtime

MODULES = (
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
)


def main() -> None:
    """Generate fixture inputs, link CPython, remove WASI imports and verify the result.

    The supplied source/build must be CPython 3.11.12, compiled with WASI SDK 25.
    Generated headers and the resulting modules stay in the requested work directory.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-source", type=Path, required=True)
    parser.add_argument("--python-build", type=Path, required=True)
    parser.add_argument("--wasi-sdk", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    source: Path = args.python_source.resolve()
    build: Path = args.python_build.resolve()
    sdk: Path = args.wasi_sdk.resolve()
    workdir: Path = args.workdir.resolve()
    if sys.version_info[:2] != (3, 11):
        parser.error("frozen bytecode must be generated with CPython 3.11")
    workdir.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).resolve().parents[1] / "tests/parity/wasm_probe"
    freeze_modules(source, workdir)
    guest = fixture.joinpath("guest.py").read_bytes()
    workdir.joinpath("guest_source.h").write_text(
        "static const char guest_source[]={" + ",".join(map(str, guest)) + ",0};\n",
        encoding="utf-8",
    )
    preliminary = workdir / "with-wasi.wasm"
    link(sdk, source, build, workdir, fixture / "bridge.c", preliminary)
    module = wasmtime.Module.from_file(wasmtime.Engine(), str(preliminary))
    shim = workdir / "wasi_shim.c"
    generate_shim(module, shim)
    result = workdir / "guest.wasm"
    link(sdk, source, build, workdir, fixture / "bridge.c", result, shim)
    compiled = wasmtime.Module.from_file(wasmtime.Engine(), str(result))
    imports = [(item.module, item.name) for item in compiled.imports]
    if any(namespace != "cordis" for namespace, _ in imports):
        raise RuntimeError(f"unexpected imports: {imports}")
    print(f"Built {result} ({result.stat().st_size} bytes)")
    print(f"Imports: {imports}")


def freeze_modules(source: Path, workdir: Path) -> None:
    """Freeze the fixture's actual standard-library dependency sources into a C table."""
    declarations = ['#include "Python.h"']
    entries: list[str] = []
    for index, name in enumerate(MODULES):
        relative = name.replace(".", "/")
        path = source / "Lib" / (relative + ".py")
        package = not path.is_file()
        if package:
            path = source / "Lib" / relative / "__init__.py"
        data = marshal.dumps(compile(path.read_bytes(), f"<frozen {name}>", "exec"))
        declarations.append(
            f"static unsigned char module_{index}[]={{" + ",".join(map(str, data)) + "};"
        )
        entries.append(f'{{"{name}",module_{index},{len(data)},{int(package)},NULL}}')
    declarations.append(
        "static const struct _frozen modules[]={" + ",".join(entries) + ",{NULL,NULL,0,0,NULL}};"
    )
    workdir.joinpath("frozen.h").write_text("\n".join(declarations), encoding="utf-8")


def link(
    sdk: Path,
    source: Path,
    build: Path,
    workdir: Path,
    bridge: Path,
    output: Path,
    shim: Path | None = None,
) -> None:
    """Link the fixture and CPython's static libraries as a core WASM reactor."""
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
    The fixture does not claim filesystem, clock, networking or entropy support.
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


if __name__ == "__main__":
    main()
