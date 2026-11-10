# SPDX-License-Identifier: MIT OR Apache-2.0
"""Build process, native and WASM guest artifacts from a Python module and class."""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import sysconfig
import tempfile
import uuid
import zipfile
from importlib.metadata import distribution
from pathlib import Path

import typing_extensions

HOST_API = """
typedef struct {
    void *host;
    void (*read)(void *, unsigned char *);
    void (*write)(void *, const unsigned char *, size_t);
    void (*log)(void *, int, const unsigned char *, size_t);
    int (*provide)(void *, const unsigned char *, size_t, const unsigned char *, size_t);
    int (*view)(void *, const unsigned char *, size_t);
    int (*invoke)(void *, const unsigned char *, size_t, const unsigned char *, size_t,
                  const unsigned char *, size_t);
} HostApi;
"""
EXPORTS = """
void *cordis_new(void);
int cordis_meta(void *, const HostApi *);
int cordis_apply(void *, const HostApi *, size_t);
int cordis_call(void *, const HostApi *, size_t);
void cordis_dispose(void *, const HostApi *);
void *kardia_new(void);
int kardia_meta(void *, const HostApi *);
int kardia_apply(void *, const HostApi *, size_t);
int kardia_call(void *, const HostApi *, size_t);
void kardia_dispose(void *, const HostApi *);
"""


def sources(source: Path, module: str, module_root: Path | None = None) -> dict[str, bytes]:
    """Collect the SDK and user source without importing or executing the guest.

    The guest can use the standard library and bundled files. Third-party native
    modules remain dependencies of its selected Python runtime.
    """
    package = Path(__file__).parent.parent
    files = {
        "pykardia/__init__.py": b'"""Embedded guest SDK."""\n',
        "pykardia/core/__init__.py": b'"""Guest configuration and JSON primitives."""\n',
        "typing_extensions.py": Path(typing_extensions.__file__).read_bytes(),
        f"{module}.py": source.read_bytes(),
    }
    for name in ("config", "value", "json", "wire"):
        files[f"pykardia/core/{name}.py"] = (package / "core" / f"{name}.py").read_bytes()
    for name in ("__init__", "api", "meta", "runtime", "process", "abi", "native"):
        files[f"pykardia/guest/{name}.py"] = (package / "guest" / f"{name}.py").read_bytes()
    if module_root is not None:
        for path in module_root.rglob("*.py"):
            files[path.relative_to(module_root).as_posix()] = path.read_bytes()
    # the SDK's own licence: the guest and the modules above are MIT OR Apache-2.0
    for name in ("LICENSE-MIT", "LICENSE-APACHE"):
        files[f"LICENSES/pykardia-guest-{name}"] = (package / "guest" / name).read_bytes()
    for project, names in (
        ("pykardia", ("NOTICE",)),
        ("typing_extensions", ("LICENSE",)),
    ):
        metadata = distribution(project)
        licenses = {
            path.name: path
            for path in metadata.files or ()
            if len(path.parts) == 1 or path.parent.name == "licenses"
        }
        for name in names:
            path = licenses.get(name)
            if path is None:
                raise FileNotFoundError(f"{project} distribution is missing license file {name}")
            files[f"LICENSES/{project}-{name}"] = path.read_binary()
    return files


def archive(files: dict[str, bytes]) -> bytes:
    """Create a self-contained source archive using standard-library zipimport."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return buffer.getvalue()


def export_process(
    source: Path,
    guest: str,
    output: Path,
    interpreter: str = sys.executable,
    module_root: Path | None = None,
) -> None:
    """Write an executable zipapp containing the guest source and the Python SDK.

    The artifact needs the specified Python interpreter, but does not need an
    installed pykardia distribution or the original source file at runtime.
    """
    module = f"_pykardia_guest_{uuid.uuid4().hex}"
    files = sources(source, module, module_root)
    files["__main__.py"] = (
        f"from {module} import {guest}\nfrom pykardia.guest import serve\nserve({guest})\n"
    ).encode()
    output.write_bytes(f"#!{interpreter}\n".encode() + archive(files))
    output.chmod(output.stat().st_mode | 0o111)


def export_dylib(
    source: Path,
    guest: str,
    output: Path,
    libpython: Path | None = None,
    module_root: Path | None = None,
) -> None:
    """Compile the C exports, cordis_* and kardia_*, with CFFI and embed the SDK and source.

    Requires CFFI, a C compiler and this interpreter's shared libpython. The built
    artifact uses that Python runtime and CFFI backend, without a pykardia install.
    """
    from cffi import FFI

    module = f"_pykardia_guest_{uuid.uuid4().hex}"
    embedded = base64.b64encode(archive(sources(source, module, module_root))).decode("ascii")
    builder = FFI()
    builder.embedding_api(HOST_API + EXPORTS)
    links: list[str] = []
    library_dirs: list[str] = []
    prelude = HOST_API
    if sys.platform != "win32":
        if libpython is not None:
            library = libpython
        elif framework := sysconfig.get_config_var("PYTHONFRAMEWORK"):
            library = (
                Path(sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX"))
                / f"{framework}.framework"
                / "Versions"
                / sysconfig.get_config_var("VERSION")
                / framework
            )
        else:
            name = (
                f"libpython{sys.version_info.major}.{sys.version_info.minor}{sys.abiflags}"
                f"{sysconfig.get_config_var('SHLIB_SUFFIX')}"
            )
            library = Path(sysconfig.get_config_var("LIBDIR")) / name
            if not library.is_file():
                # Relocated interpreters may retain their original build prefix in LIBDIR.
                library = Path(sys.base_prefix) / "lib" / name
        library = library.resolve(strict=True)
        library_dirs.append(str(library.parent))
        if sysconfig.get_config_var("PYTHONFRAMEWORK"):
            # CFFI also adds -lpythonX.Y; framework builds keep that alias under lib/.
            library_dirs.append(str(library.parent / "lib"))
        links.extend([str(library), f"-Wl,-rpath,{library.parent}"])
        # Rust's loader uses RTLD_LOCAL; extension modules need libpython's symbols globally.
        prelude += (
            "#include <dlfcn.h>\n"
            "__attribute__((constructor)) static void expose_python(void) {\n"
            f"    (void)dlopen({json.dumps(str(library))}, RTLD_NOW | RTLD_GLOBAL);\n"
            "}\n"
        )
    elif libpython is not None:
        links.append(str(libpython.resolve(strict=True)))
    builder.set_source(f"{module}_ffi", prelude, library_dirs=library_dirs, extra_link_args=links)
    builder.embedding_init_code(
        f"""
import base64
import atexit
import shutil
import sys
import tempfile
from {module}_ffi import ffi
_bundle = tempfile.mkdtemp(prefix="pykardia-guest-")
atexit.register(shutil.rmtree, _bundle)
_path = _bundle + "/guest.zip"
with open(_path, "wb") as _stream:
    _stream.write(base64.b64decode({embedded!r}))
sys.path.insert(0, _path)
from {module} import {guest}
from pykardia.guest.native import install
install(ffi, {guest})
"""
    )
    with tempfile.TemporaryDirectory(prefix="pykardia-build-") as directory:
        builder.compile(target=str(output.resolve()), tmpdir=directory, verbose=False)


def main() -> None:
    """Build an artifact without importing the user module in the build process."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("process", "dylib", "wasm"))
    parser.add_argument("source", type=Path)
    parser.add_argument("guest", help="Guest implementation class in the source module")
    parser.add_argument("output", type=Path)
    parser.add_argument("--python-source", type=Path)
    parser.add_argument("--python-build", type=Path)
    parser.add_argument("--wasi-sdk", type=Path)
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument(
        "--module-root", type=Path, help="Bundle Python modules below this directory"
    )
    args = parser.parse_args()
    if args.kind == "process":
        export_process(args.source, args.guest, args.output, module_root=args.module_root)
    elif args.kind == "dylib":
        export_dylib(args.source, args.guest, args.output, module_root=args.module_root)
    else:
        from pykardia.guest.wasm_build import export_wasm

        if not all((args.python_source, args.python_build, args.wasi_sdk)):
            parser.error("wasm requires --python-source, --python-build and --wasi-sdk")
        export_wasm(
            args.source,
            args.guest,
            args.output,
            args.python_source,
            args.python_build,
            args.wasi_sdk,
            tuple(args.module),
            args.module_root,
        )


if __name__ == "__main__":
    main()
