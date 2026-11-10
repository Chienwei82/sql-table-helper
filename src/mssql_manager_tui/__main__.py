"""Allow ``python -m mssql_manager_tui``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
