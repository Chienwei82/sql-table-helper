#!/usr/bin/env python3
"""Build a single-file executable with PyInstaller (the optional ``standalone`` extra).

The wheel is the supported distribution; this is for the machines where installing a
Python toolchain is not an option — a jump host, an air-gapped server, a locked-down
workstation. Run it with::

    uv sync --extra standalone
    uv run python scripts/build_standalone.py

and get ``dist/sql-table-swiss-knife`` (or ``.exe`` on Windows).

Two things this script will not pretend to do:

* it does **not** bundle the ODBC driver manager or the Microsoft ODBC driver — those
  are system libraries, and a frozen binary that silently cannot connect is worse than
  one that says so;
* it is **not** covered by the test suite and **not** built on CI, because a PyInstaller
  artifact is platform-specific and slow. If it breaks, it breaks in the environment
  where you run it.

The script is deliberately boring: no clever caching, no parallelism, no silent
fallbacks. It either produces the binary or tells you what to install.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

#: Where the build lands, relative to the repository root.
DIST = Path("dist")

#: The spec file, so repeat builds do not accumulate hidden imports.
SPEC = Path("build") / "sql-table-swiss-knife.spec"

#: PyInstaller's own work directory. Removed first so a build never picks up stale
#: analysis from an older source tree.
WORK = Path("build") / "sql-table-swiss-knife"


def main() -> int:
    """Run PyInstaller and report the artifact. Returns a process exit code."""
    if shutil.which("pyinstaller") is None:
        print(
            "pyinstaller is not installed.\n"
            "  uv sync --extra standalone\n"
            "or, outside uv:  pip install 'sql-table-swiss-knife[standalone]'",
            file=sys.stderr,
        )
        return 1

    root = Path(__file__).resolve().parent.parent
    for path in (SPEC, WORK):
        if path.exists():
            shutil.rmtree(path) if path.is_dir() else path.unlink()

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        # One file, because the point is to copy one artefact onto a machine.
        "--onefile",
        "--name",
        "sql-table-swiss-knife",
        # A console app: it is a terminal program, and a windowed build on Windows
        # would refuse to print --version.
        "--console",
        # Keep the bundle honest and small: Textual ships no test data, and the sample
        # SQL scripts are not needed at runtime.
        "--exclude-module",
        "pytest",
        "--exclude-module",
        "tkinter",
        "--distpath",
        str(root / DIST),
        "--workpath",
        str(root / WORK),
        "--specpath",
        str(SPEC.parent),
        str(root / "src" / "sql_table_swiss_knife" / "__main__.py"),
    ]
    print("running:", " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=root, check=False)
    if completed.returncode != 0:
        print("pyinstaller failed", file=sys.stderr)
        return completed.returncode

    name = "sql-table-swiss-knife.exe" if sys.platform == "win32" else "sql-table-swiss-knife"
    binary = root / DIST / name
    if not binary.exists():
        print(f"pyinstaller reported success but {binary} is missing", file=sys.stderr)
        return 1
    size_mb = binary.stat().st_size / (1024 * 1024)
    print(f"\nwrote {binary}  ({size_mb:.1f} MiB)")
    print(
        "note: the ODBC driver manager and the SQL Server ODBC driver are NOT bundled —\n"
        "      install them on the target machine, or the app cannot connect."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
