"""Prepare the pinned CPython 3.11 / WASI SDK toolchain used by guest export acceptance."""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

PYTHON = (
    "https://www.python.org/ftp/python/3.11.12/Python-3.11.12.tar.xz",
    "849da87af4df137710c1796e276a955f7a85c9f971081067c8f565d15c352a09",
)
SDK = (
    "https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-25/"
    "wasi-sdk-25.0-x86_64-linux.tar.gz",
    "52640dde13599bf127a95499e61d6d640256119456d1af8897ab6725bcf3d89c",
)


def main() -> None:
    """Download verified build inputs, compile static libraries and report acceptance paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "Python-3.11.12"
    sdk = directory / "wasi-sdk-25.0-x86_64-linux"
    prepare(PYTHON, directory / "Python.tar.xz", source)
    prepare(SDK, directory / "wasi-sdk.tar.gz", sdk)
    build = directory / "build"
    build.mkdir(exist_ok=True)
    if not (build / "Makefile").exists():
        environment = dict(os.environ)
        environment["CONFIG_SITE"] = str(source / "Tools/wasm/config.site-wasm32-wasi")
        environment["WASI_SDK_PATH"] = str(sdk)
        subprocess.run(
            [
                str(source / "Tools/wasm/wasi-env"),
                str(source / "configure"),
                "--host=wasm32-unknown-wasi",
                "--build=x86_64-pc-linux-gnu",
                f"--with-build-python={sys.executable}",
                "--without-ensurepip",
                "--disable-test-modules",
            ],
            cwd=build,
            env=environment,
            check=True,
        )
    subprocess.run(
        [
            "make",
            f"-j{args.jobs}",
            "libpython3.11.a",
            "Modules/_decimal/libmpdec/libmpdec.a",
            "Modules/expat/libexpat.a",
        ],
        cwd=build,
        check=True,
    )
    values = {
        "PYKARDIA_PYTHON_SOURCE": source,
        "PYKARDIA_PYTHON_BUILD": build,
        "PYKARDIA_WASI_SDK": sdk,
    }
    for name, path in values.items():
        print(f"{name}={path}")
    if env_file := os.environ.get("GITHUB_ENV"):
        with Path(env_file).open("a", encoding="utf-8") as stream:
            stream.writelines(f"{name}={path}\n" for name, path in values.items())


def prepare(specification: tuple[str, str], archive: Path, extracted: Path) -> None:
    """Verify each archive at the download boundary and extract it using the data filter."""
    url, digest = specification
    if not archive.exists():
        request = urllib.request.Request(url, headers={"User-Agent": "pykardia-build"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError(f"archive checksum mismatch: {archive}")
    if not extracted.exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(archive.parent, filter="data")


if __name__ == "__main__":
    main()
