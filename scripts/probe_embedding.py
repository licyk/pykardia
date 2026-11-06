"""Reproduce the Linux CFFI feasibility probe without adding a product build workflow."""

import argparse
import subprocess
from pathlib import Path

from cffi import FFI


def main() -> None:
    """Compile one embedded Python C export and call it from a standalone C process.

    Requires the development environment's CFFI, a C compiler and a shared libpython.
    This deliberately validates only an exported symbol, not the full guest ABI.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--libpython", type=Path, required=True)
    args = parser.parse_args()
    directory: Path = args.directory.resolve()
    library: Path = args.libpython.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    builder = FFI()
    builder.embedding_api("int cordis_meta(void);")
    builder.set_source(
        "_pykardia_probe",
        "",
        extra_link_args=[str(library), f"-Wl,-rpath,{library.parent}"],
    )
    builder.embedding_init_code('''
from _pykardia_probe import ffi

@ffi.def_extern()
def cordis_meta() -> int:
    """Return a Python-computed status through an exported C symbol."""
    return sum([17, 25])
''')
    guest = directory / "guest.so"
    builder.compile(target=str(guest), tmpdir=str(directory), verbose=False)
    host_source = directory / "host.c"
    host_source.write_text(
        r"""
#include <dlfcn.h>
#include <stdio.h>

int main(int argc, char **argv) {
    if (argc != 2) return 4;
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_GLOBAL);
    if (!library) { puts(dlerror()); return 1; }
    int (*meta)(void) = dlsym(library, "cordis_meta");
    if (!meta) { puts(dlerror()); return 2; }
    int result = meta();
    printf("Python code via native host: %d\n", result);
    return result == 42 ? 0 : 3;
}
""",
        encoding="utf-8",
    )
    host = directory / "host"
    subprocess.run(["cc", str(host_source), "-ldl", "-o", str(host)], check=True)
    subprocess.run([str(host), str(guest)], check=True)


if __name__ == "__main__":
    main()
