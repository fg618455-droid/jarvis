"""Create Jarvis's isolated Claude subscription sidecar environment."""

from __future__ import annotations

import argparse
import subprocess
import sys
import venv
from pathlib import Path


DEFAULT_ENV_DIR = Path.home() / ".jarvis" / "claude-subscription-venv"
REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements-claude-sidecar.txt"


def _interpreter(environment: Path) -> Path:
    if sys.platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Create the isolated Claude subscription sidecar environment."
    )
    parser.add_argument(
        "--environment",
        type=Path,
        default=DEFAULT_ENV_DIR,
        help="environment directory (default: ~/.jarvis/claude-subscription-venv)",
    )
    args = parser.parse_args()
    target = args.environment.expanduser().resolve()

    print(f"🔧 Creating Claude subscription sidecar environment\n   {target}", flush=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True).create(target)
    python = _interpreter(target)

    print("📦 Installing the isolated Claude Agent SDK dependency set", flush=True)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "-r",
            str(REQUIREMENTS),
        ],
        check=True,
    )
    subprocess.run(
        [str(python), "-I", "-c", "import claude_agent_sdk"],
        check=True,
    )
    print("✅ Claude subscription sidecar environment is ready", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
