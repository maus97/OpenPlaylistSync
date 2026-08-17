"""Check the local runtime assumptions used by OPS."""

from __future__ import annotations

import argparse
import shutil
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strict-python",
        action="store_true",
        help="return a failure when the interpreter is not Python 3.12",
    )
    args = parser.parse_args()

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"Python: {python_version}")
    if (sys.version_info.major, sys.version_info.minor) != (3, 12):
        message = "warning: OPS targets Python 3.12"
        print(message)
        if args.strict_python:
            return 1

    docker_path = shutil.which("docker")
    if docker_path:
        print(f"Docker: {docker_path}")
    else:
        print("warning: Docker is not installed or is not on PATH")

    print("environment check complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
