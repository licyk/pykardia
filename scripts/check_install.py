"""Install both distribution artifacts into clean environments and exercise public entry points."""

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

import async_timeout


async def cli(command: list[str], directory: Path, environment: dict[str, str]) -> None:
    """Load a real WAT example with no source tree or Cargo in the runtime search path."""
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=directory,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )
    output: list[str] = []
    try:
        assert process.stdout is not None
        async with async_timeout.timeout(15):
            while True:
                line = (await process.stdout.readline()).decode()
                output.append(line)
                if "hello from wasm" in line:
                    break
                if not line:
                    raise RuntimeError("installed CLI failed to apply WAT: " + "".join(output))
        await asyncio.sleep(0.15)
        process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        _, stderr = await asyncio.wait_for(process.communicate(), 10)
        if process.returncode != 0 or stderr:
            raise RuntimeError(f"installed CLI failed: {process.returncode}, {stderr.decode()}")
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


def main() -> None:
    """Build isolated Python environments and validate wheel and sdist independently."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    artifacts = [*args.directory.glob("*.whl"), *args.directory.glob("*.tar.gz")]
    if len(artifacts) != 2:
        raise ValueError("expected exactly one wheel and one sdist in the artifact directory")
    for artifact in artifacts:
        with tempfile.TemporaryDirectory(prefix="pykardia-install-") as directory:
            base = Path(directory)
            environment = base / "venv"
            venv.EnvBuilder(with_pip=True, symlinks=sys.platform != "win32").create(environment)
            bin_dir = environment / ("Scripts" if sys.platform == "win32" else "bin")
            python = bin_dir / ("python.exe" if sys.platform == "win32" else "python")
            subprocess.run(
                [str(python), "-m", "pip", "install", str(artifact.resolve())], check=True
            )
            work = base / "work"
            work.mkdir()
            (work / "hello.wat").write_bytes((root / "examples/plugins/hello.wat").read_bytes())
            (work / "pykardia.yml").write_text('- name: "hello.wat"\n')
            (work / "counter.py").write_bytes((root / "examples/guest_counter.py").read_bytes())
            runtime = {
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONPATH", "PYTHONHOME"}
            }
            runtime["PATH"] = str(bin_dir)
            runtime["PYTHONNOUSERSITE"] = "1"
            subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    """
import importlib.metadata
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import zipfile
import pykardia
import pykardia.guest.export
import pykardia.guest.wasm_build
import pykardia.prelude
assert shutil.which("cargo") is None
assert shutil.which("kardia") is None
assert importlib.util.find_spec("kardia") is None
metadata = importlib.metadata.distribution("pykardia")
assert metadata.metadata["Name"] == "pykardia"
assert {entry.name for entry in metadata.entry_points if entry.group == "console_scripts"} == {
    "pykardia"
}
assert pathlib.Path(pykardia.__file__).is_relative_to(pathlib.Path(sys.prefix))
assert pykardia.catalog().get("include") is not None
assert importlib.metadata.version("pykardia") == pykardia.__version__
template = pathlib.Path(pykardia.guest.export.__file__).with_name("templates") / "wasm.c"
assert template.is_file()
pykardia.guest.export.export_process(pathlib.Path("counter.py"), "Counter", pathlib.Path("counter"))
pathlib.Path("counter.py").unlink()
with zipfile.ZipFile("counter") as bundle:
    assert b"MIT License" in bundle.read("LICENSES/pykardia-guest-LICENSE-MIT")
    assert b"Apache License" in bundle.read("LICENSES/pykardia-guest-LICENSE-APACHE")
    assert bundle.read("LICENSES/pykardia-NOTICE")
    assert b"Python Software Foundation" in bundle.read("LICENSES/typing_extensions-LICENSE")
guest = subprocess.run(
    [sys.executable, "-I", "-S", "counter"],
    input='{"export":"meta"}\\n{"export":"dispose"}\\n',
    text=True, capture_output=True, check=True, timeout=10,
)
records = [json.loads(line) for line in guest.stdout.splitlines()]
assert [record["done"] for record in records if "done" in record] == [0, 0]
assert json.loads(records[0]["output"])["plugin"]["name"] == "counter"
assert not guest.stderr
print("Installed", pykardia.__version__, pykardia.__file__)
""",
                ],
                cwd=work,
                env=runtime,
                check=True,
            )
            asyncio.run(
                cli(
                    [str(bin_dir / ("pykardia.exe" if sys.platform == "win32" else "pykardia"))],
                    work,
                    runtime,
                )
            )
            asyncio.run(cli([str(python), "-m", "pykardia"], work, runtime))
            print(
                f"Accepted {artifact.name}: imports, process export, licenses, CLI, WAT, SIGINT",
                flush=True,
            )


if __name__ == "__main__":
    main()
