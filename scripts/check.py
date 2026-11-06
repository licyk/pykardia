"""Run the same static and behavioral checks locally and in CI."""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Run all configured checks and return failure if any check fails."""
    root = Path(__file__).resolve().parents[1]
    paths = [
        name for name in ("pykardia", "tests", "examples", "scripts") if (root / name).exists()
    ]
    commands = [
        ["ruff", "check", *paths],
        ["ruff", "format", "--check", *paths],
        ["ty", "check", "--python", sys.executable, *paths],
        ["unittest", "discover", "-s", "tests", "-t", ".", "-v"],
    ]
    failed = False
    for command in commands:
        print(f"Checking: {' '.join(command)}", flush=True)
        result = subprocess.run([sys.executable, "-m", *command], cwd=root, check=False)
        failed |= result.returncode != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
