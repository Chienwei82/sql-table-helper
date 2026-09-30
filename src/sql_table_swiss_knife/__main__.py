"""Allow ``python -m sql_table_swiss_knife``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
