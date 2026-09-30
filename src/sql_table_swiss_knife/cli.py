"""Command-line entry point for ``sql-table-swiss-knife`` / ``stsk``.

Default (no subcommand): launch the TUI. Subcommands are temporary developer tools
landed with the corresponding milestone; ``inspect`` prints table metadata as a Rich
table so introspection can be verified without the TUI.
"""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence

from rich.console import Console, Group
from rich.table import Table as RichTable

from . import __version__
from .domain import AuthMode, Database, Table, TableSummary
from .providers import get_provider
from .storage import ProfileStore, default_secret_store, resolve_password
from .tui.app import SwissKnifeApp

__all__ = ["build_parser", "main", "render_metadata"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sql-table-swiss-knife",
        description=(
            "View and edit rows of catalog/lookup tables without writing SQL "
            "— and see the SQL it runs."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help=(
            "refuse every write for this session and lock the read-only toggle: "
            "no staging, no Apply, whatever the profile says (S-9)"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    inspect = subparsers.add_parser(
        "inspect",
        help="print the metadata of one table (schema.table) as a Rich table",
        description=(
            "Temporary M2 developer command: connect using a stored profile and print "
            "the introspected metadata of <schema>.<table>."
        ),
    )
    inspect.add_argument("profile", help="name of a profile in profiles.toml")
    inspect.add_argument("table", help="table to inspect, as <schema>.<table>")
    inspect.add_argument(
        "--list",
        action="store_true",
        help="instead of the table, list the tables of the connected database",
    )
    inspect.add_argument(
        "--databases",
        action="store_true",
        help="list the databases visible to the login and exit",
    )
    return parser


def _split_table_arg(value: str) -> tuple[str, str]:
    """Split ``schema.table``; a bare name defaults to the ``dbo`` schema."""
    if "." in value:
        schema, _, name = value.partition(".")
    else:
        schema, name = "dbo", value
    if not schema or not name:
        raise ValueError(f"cannot parse table reference {value!r}; expected <schema>.<table>")
    return schema, name


def _type_label(
    *, data_type: str, max_length: int | None, precision: int | None, scale: int | None
) -> str:
    """Render a column's exact SQL type, e.g. ``nvarchar(100)`` or ``decimal(18,2)``."""
    if precision is not None and scale is not None:
        return f"{data_type}({precision},{scale})"
    if precision is not None and scale is None:
        return f"{data_type}({precision})"
    if max_length is not None:
        return f"{data_type}({max_length})"
    return data_type


def render_metadata(table: Table) -> Group:
    """Build the Rich renderable describing a :class:`~domain.catalog.Table`."""
    columns = RichTable(title=f"{table.schema}.{table.name}  ({table.kind.value})", show_lines=True)
    columns.add_column("#", justify="right", style="dim")
    columns.add_column("Column", style="bold")
    columns.add_column("Type")
    columns.add_column("Null", justify="center")
    columns.add_column("Key", justify="center")
    columns.add_column("Extra")

    for column in table.columns:
        extra: list[str] = []
        if column.is_identity:
            extra.append(f"identity({column.identity_seed},{column.identity_increment})")
        if column.is_computed:
            persisted = "persisted" if column.computed_persisted else "not persisted"
            extra.append(f"computed {persisted}: {column.computed_definition}")
        if column.is_rowversion:
            extra.append("rowversion")
        if column.default_definition:
            extra.append(f"default {column.default_definition}")
        if column.collation:
            extra.append(f"collation {column.collation}")
        flags = []
        if column.is_primary_key:
            flags.append("PK")
        if column.is_foreign_key:
            flags.append("FK")
        if column.is_server_managed:
            flags.append("ro")
        columns.add_row(
            str(column.ordinal),
            column.name,
            _type_label(
                data_type=column.data_type,
                max_length=column.max_length,
                precision=column.precision,
                scale=column.scale,
            ),
            "yes" if column.nullable else "no",
            ",".join(flags),
            "; ".join(extra),
        )

    info = RichTable(title=f"{table.schema}.{table.name} — keys, constraints & triggers")
    info.add_column("Kind", style="bold")
    info.add_column("Name")
    info.add_column("Details")
    info.add_row(
        "Table",
        "",
        f"rows≈{table.approximate_row_count if table.approximate_row_count is not None else 'n/a'}"
        f" · primary key: {'yes' if table.primary_key else 'NO (read-only rows)'}"
        f" · system-versioned: {'yes' if table.is_system_versioned else 'no'}"
        f" · history table: {'yes' if table.is_history_table else 'no'}"
        + (
            f" · history: {table.history_schema}.{table.history_table}"
            if table.is_system_versioned
            else ""
        ),
    )
    if table.primary_key:
        info.add_row(
            "PK", table.primary_key.name or "(unnamed)", ", ".join(table.primary_key.columns)
        )
    for unique in table.unique_constraints:
        info.add_row("UNIQUE", unique.name, ", ".join(unique.columns))
    for fk in table.foreign_keys:
        info.add_row(
            "FK →",
            fk.name,
            f"{', '.join(fk.columns)} → {fk.referenced_schema}.{fk.referenced_table}"
            f" ({', '.join(fk.referenced_columns)})"
            f" on delete {fk.on_delete.value}, on update {fk.on_update.value}",
        )
    for incoming in table.incoming_foreign_keys:
        info.add_row(
            "FK ←",
            incoming.name,
            f"referenced by {incoming.ref} ({', '.join(incoming.columns)})"
            f" on delete {incoming.on_delete.value}",
        )
    for check in table.check_constraints:
        info.add_row("CHECK", check.name, check.definition)
    for trigger in table.triggers:
        state = "enabled" if trigger.enabled else "DISABLED"
        info.add_row(
            f"TRIGGER {trigger.firing}",
            trigger.name,
            f"on {', '.join(trigger.events)} ({state})",
        )
    return Group(columns, info)


def _print_table_summaries(summaries: Sequence[TableSummary]) -> None:
    console = Console()
    listing = RichTable(title="tables")
    listing.add_column("Schema", style="bold")
    listing.add_column("Name", style="bold")
    listing.add_column("Kind")
    listing.add_column("Rows≈", justify="right")
    listing.add_column("PK", justify="center")
    listing.add_column("Triggers", justify="center")
    for summary in summaries:
        listing.add_row(
            summary.schema,
            summary.name,
            summary.kind.value,
            "" if summary.approximate_row_count is None else str(summary.approximate_row_count),
            "yes" if summary.has_primary_key else "no",
            "yes" if summary.has_triggers else "",
        )
    console.print(listing)


async def _run_inspect(args: argparse.Namespace) -> int:
    store = ProfileStore()
    profile = store.get(args.profile)
    provider = get_provider(profile.provider)

    password: str | None = None
    if profile.auth is AuthMode.SQL:
        password = resolve_password(default_secret_store(), profile, prompt=getpass.getpass)
        if password is None:
            print(
                f"error: no password available for profile {profile.name!r} "
                "(store it in the OS keyring or run interactively)",
                file=sys.stderr,
            )
            return 2

    conn = await provider.connect(profile, password)
    try:
        console = Console()
        if args.databases:
            databases: list[Database] = await provider.list_databases(conn)
            listing = RichTable(title="databases")
            listing.add_column("Name", style="bold")
            listing.add_column("Current", justify="center")
            for database in databases:
                listing.add_row(database.name, "*" if database.is_current else "")
            console.print(listing)
            return 0
        if args.list:
            _print_table_summaries(await provider.list_tables(conn))
            return 0
        schema, name = _split_table_arg(args.table)
        console.print(render_metadata(await provider.get_table_metadata(conn, schema, name)))
        return 0
    finally:
        await provider.disconnect(conn)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments, then run the requested command (default: the TUI)."""
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        try:
            return asyncio.run(_run_inspect(args))
        except Exception as exc:  # user-facing CLI error, no traceback
            print(f"error: {exc}", file=sys.stderr)
            return 1
    app = SwissKnifeApp(read_only=args.read_only)
    app.run()
    return 0
