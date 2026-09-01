"""Minimal container entry point: migrate, then replace the process with Uvicorn."""

import os
import sys

from alembic import command
from alembic.config import Config


def main() -> None:
    configuration = Config("/app/alembic.ini")
    command.upgrade(configuration, "head")
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "ops.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--no-access-log",
            "--no-server-header",
            "--no-proxy-headers",
        ],
    )


if __name__ == "__main__":
    main()
