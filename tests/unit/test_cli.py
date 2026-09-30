"""Tests for the CLI: argument parsing and the temporary ``inspect`` command."""

import argparse
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from sql_table_swiss_knife import cli
from sql_table_swiss_knife.domain import (
    AuthMode,
    CheckConstraint,
    Column,
    ConnectionProfile,
    ForeignKey,
    PrimaryKey,
    ReferentialAction,
    Table,
    TableKind,
    Trigger,
    UniqueConstraint,
)
from tests.fakes import FakeProvider

COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
        Column("Name", 2, "nvarchar", 100, None, None, False, None, False),
        Column("Population", 3, "int", None, None, None, False, "((0))", False),
        Column(
            "NameUpper",
            4,
            "nvarchar",
            100,
            None,
            None,
            True,
            None,
            False,
            is_computed=True,
            computed_definition="UPPER([Name])",
            computed_persisted=False,
        ),
        Column("RowVer", 5, "rowversion", 8, None, None, False, None, False, is_rowversion=True),
    ),
    primary_key=PrimaryKey("PK_Country", ("Code",)),
    foreign_keys=(
        ForeignKey(
            "FK_Region_Country",
            ("CountryCode",),
            "dbo",
            "Country",
            ("Code",),
            ReferentialAction.CASCADE,
            ReferentialAction.NO_ACTION,
        ),
    ),
    unique_constraints=(UniqueConstraint("UQ_Country_Name", ("Name",)),),
    check_constraints=(CheckConstraint("CK_Country_Code", "([Code]=upper([Code]))"),),
    triggers=(Trigger("trg_Country", ("UPDATE",), "AFTER"),),
    approximate_row_count=3,
)


# -- parser -------------------------------------------------------------------


def test_no_subcommand_launches_tui() -> None:
    args = cli.build_parser().parse_args([])
    assert args.command is None


def test_inspect_requires_profile_and_table() -> None:
    args = cli.build_parser().parse_args(["inspect", "local", "dbo.Country"])
    assert args.command == "inspect"
    assert args.profile == "local"
    assert args.table == "dbo.Country"
    assert args.list is False
    assert args.databases is False


def test_inspect_flags() -> None:
    args = cli.build_parser().parse_args(["inspect", "local", "x", "--list", "--databases"])
    assert args.list is True
    assert args.databases is True


def test_split_table_arg() -> None:
    assert cli._split_table_arg("dbo.Country") == ("dbo", "Country")
    assert cli._split_table_arg("Country") == ("dbo", "Country")
    assert cli._split_table_arg("sales.Order.Header") == ("sales", "Order.Header")
    with pytest.raises(ValueError, match="expected"):
        cli._split_table_arg("dbo.")


def test_type_label_renders_exact_types() -> None:
    assert (
        cli._type_label(data_type="nvarchar", max_length=100, precision=None, scale=None)
        == "nvarchar(100)"
    )
    assert (
        cli._type_label(data_type="decimal", max_length=None, precision=18, scale=2)
        == "decimal(18,2)"
    )
    assert cli._type_label(data_type="bit", max_length=None, precision=None, scale=None) == "bit"


# -- rendering ----------------------------------------------------------------


def test_render_metadata_contains_column_and_key_details() -> None:
    console = Console(width=200, record=True, force_terminal=False)
    console.print(cli.render_metadata(COUNTRY))
    output = console.export_text()
    assert "dbo.Country" in output
    assert "char(2)" in output
    assert "nvarchar(100)" in output
    assert "rowversion" in output
    assert "identity" not in output
    assert "UPPER" in output
    assert "PK" in output
    assert "FK →" in output
    assert "FK" in output and "dbo.Region" not in output
    assert "CASCADE" in output
    assert "CK_Country_Code" in output
    assert "trg_Country" in output
    assert "rows≈3" in output


def test_render_metadata_flags_missing_primary_key() -> None:
    table = Table(
        schema="dbo",
        name="Log",
        kind=TableKind.BASE_TABLE,
        columns=(Column("Message", 1, "nvarchar", 400, None, None, True, None, False),),
    )
    console = Console(width=200, record=True, force_terminal=False)
    console.print(cli.render_metadata(table))
    assert "NO (read-only rows)" in console.export_text()


def test_print_table_summaries(capsys: pytest.CaptureFixture[str]) -> None:
    from sql_table_swiss_knife.domain import TableSummary

    cli._print_table_summaries(
        [
            TableSummary("dbo", "Country", TableKind.BASE_TABLE, True, 3, False),
            TableSummary("dbo", "Region", TableKind.BASE_TABLE, True, 4, True),
            TableSummary("dbo", "v_Country", TableKind.VIEW, False, 0, True),
        ]
    )
    output = capsys.readouterr().out
    assert "Country" in output and "Region" in output and "v_Country" in output
    assert "view" in output


# -- inspect command ----------------------------------------------------------


@pytest.fixture
def fake_profile(tmp_path: Path) -> ConnectionProfile:
    return ConnectionProfile(
        name="fake-profile",
        provider="fake",
        host="localhost",
        database="test",
        auth=AuthMode.INTEGRATED,
    )


def _patch_inspect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: ConnectionProfile,
    provider: Any,
    password: str | None,
) -> list[str]:
    """Point the inspect command at a profile file, a provider and a password."""
    from sql_table_swiss_knife.storage import ProfileStore

    ProfileStore(tmp_path / "profiles.toml").upsert(profile)
    monkeypatch.setenv("SWISSKNIFE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "get_provider", lambda name: provider)

    from sql_table_swiss_knife import storage

    class _Store:
        persistent = False

        def get(self, ref: str) -> str | None:
            return password

    monkeypatch.setattr(storage, "default_secret_store", lambda: _Store())
    monkeypatch.setattr(cli, "default_secret_store", lambda: _Store())
    return []


async def test_inspect_prints_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_profile: ConnectionProfile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeProvider([COUNTRY])
    _patch_inspect(monkeypatch, tmp_path, fake_profile, provider, None)
    code = await cli._run_inspect(
        argparse.Namespace(profile="fake-profile", table="dbo.Country", list=False, databases=False)
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "dbo.Country" in output
    assert "nvarchar(100)" in output


async def test_inspect_lists_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_profile: ConnectionProfile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeProvider([COUNTRY])
    _patch_inspect(monkeypatch, tmp_path, fake_profile, provider, None)
    code = await cli._run_inspect(
        argparse.Namespace(profile="fake-profile", table="ignored", list=True, databases=False)
    )
    assert code == 0
    assert "Country" in capsys.readouterr().out


async def test_inspect_lists_databases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_profile: ConnectionProfile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeProvider([COUNTRY])
    _patch_inspect(monkeypatch, tmp_path, fake_profile, provider, None)
    code = await cli._run_inspect(
        argparse.Namespace(profile="fake-profile", table="ignored", list=False, databases=True)
    )
    assert code == 0
    assert "test" in capsys.readouterr().out


async def test_inspect_reports_unknown_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_run_inspect` propagates storage errors; `main` is what turns them into exit 1."""
    from sql_table_swiss_knife.storage import ProfileError

    monkeypatch.setenv("SWISSKNIFE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ProfileError, match="unknown profile"):
        await cli._run_inspect(
            argparse.Namespace(profile="nope", table="dbo.Country", list=False, databases=False)
        )


def test_main_returns_error_code_for_failing_inspect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SWISSKNIFE_CONFIG_DIR", str(tmp_path))
    code = cli.main(["inspect", "missing", "dbo.Country"])
    assert code == 1
    assert "error:" in capsys.readouterr().err
