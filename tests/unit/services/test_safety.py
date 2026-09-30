"""Tests for the M8 safety policy: who may write, and how an Apply is confirmed.

These are the rules that stand between a mistyped keystroke and a production
database, so the tests assert the *wording* as well as the verdict: a refusal the
user cannot understand is a refusal they will work around.
"""

import pytest

from sql_table_swiss_knife.domain import (
    AuthMode,
    Column,
    ConnectionProfile,
    Environment,
    PrimaryKey,
    Table,
    TableKind,
)
from sql_table_swiss_knife.domain.changes import ChangeKind
from sql_table_swiss_knife.services.safety import (
    PRODUCTION_CONFIRM_WORD,
    BlockedReason,
    SafetyPolicy,
    TypedConfirmation,
    WritePermission,
    affected_tables,
    describe_plan,
)


def column(name: str = "Code") -> Column:
    return Column(
        name=name,
        ordinal=1,
        data_type="char",
        max_length=2,
        precision=None,
        scale=None,
        nullable=False,
        default_definition=None,
        is_identity=False,
    )


def keyed_table(name: str = "Country") -> Table:
    """A table with a primary key: the ordinary, writable case."""
    return Table(
        schema="dbo",
        name=name,
        kind=TableKind.BASE_TABLE,
        columns=(column(),),
        primary_key=PrimaryKey(name="PK_Country", columns=("Code",)),
    )


def keyless_table(name: str = "AuditEntry") -> Table:
    """A table with no key at all: UPDATE/DELETE cannot be scoped to a row (S-4)."""
    return Table(schema="dbo", name=name, kind=TableKind.BASE_TABLE, columns=(column(),))


def profile(**overrides: object) -> ConnectionProfile:
    defaults: dict[str, object] = {
        "name": "demo",
        "provider": "mssql",
        "host": "sql01",
        "username": "sa",
        "auth": AuthMode.SQL,
    }
    return ConnectionProfile(**{**defaults, **overrides})  # type: ignore[arg-type]


def writable(**overrides: object) -> SafetyPolicy:
    """A development-like policy that permits writes."""
    defaults: dict[str, object] = {"read_only": False}
    return SafetyPolicy(**{**defaults, **overrides})  # type: ignore[arg-type]


# -- environments -----------------------------------------------------------


def test_production_reads_only_by_default() -> None:
    policy = SafetyPolicy.for_profile(profile(environment=Environment.PRODUCTION))
    assert policy.read_only is True
    assert "PROD" in policy.read_only_reason


@pytest.mark.parametrize(
    "environment",
    [Environment.DEVELOPMENT, Environment.TEST, Environment.STAGING],
)
def test_non_production_profiles_write_by_default(environment: Environment) -> None:
    assert SafetyPolicy.for_profile(profile(environment=environment)).read_only is False


def test_a_profile_may_force_read_only_outside_production() -> None:
    """A shared catalog can be locked down regardless of its environment label."""
    policy = SafetyPolicy.for_profile(profile(read_only=True))
    assert policy.read_only is True


def test_a_production_profile_may_explicitly_opt_into_writing() -> None:
    policy = SafetyPolicy.for_profile(profile(environment=Environment.PRODUCTION, read_only=False))
    assert policy.read_only is False


def test_read_only_can_be_toggled_at_runtime() -> None:
    policy = writable()
    assert policy.toggle_read_only() is True
    assert policy.toggle_read_only() is False


def test_a_forced_read_only_session_refuses_the_toggle() -> None:
    """``--read-only`` starts the process in a posture the user may not leave."""
    policy = SafetyPolicy(forced_read_only=True, read_only=True)
    assert policy.toggle_read_only() is True
    assert policy.forced_read_only is True


def test_the_read_only_reason_explains_the_force() -> None:
    assert "--read-only" in SafetyPolicy(forced_read_only=True, read_only=True).read_only_reason


# -- per-table rules --------------------------------------------------------


def test_a_keyed_table_is_writable() -> None:
    permission = writable().check_table(keyed_table())
    assert permission.allowed is True
    assert permission.reason is None


def test_a_keyless_table_is_refused_with_the_reason() -> None:
    permission = writable().check_table(keyless_table())
    assert permission.allowed is False
    assert permission.reason is BlockedReason.NO_IDENTITY
    assert "no primary key" in permission.message
    assert "allow_keyless_writes" in permission.message  # says how to override


def test_the_keyless_refusal_can_be_overridden_explicitly() -> None:
    """The override is opt-in and names the setting, so it is never accidental."""
    assert writable(allow_keyless_writes=True).check_table(keyless_table()).allowed is True


def test_a_view_is_refused_as_a_view() -> None:
    view = Table(schema="dbo", name="v_Country", kind=TableKind.VIEW, columns=(column(),))
    permission = writable().check_table(view)
    assert permission.reason is BlockedReason.VIEW
    assert "view" in permission.message


def test_read_only_refuses_before_the_table_is_considered() -> None:
    """Read-only is reported as read-only, not as "no primary key"."""
    policy = writable(read_only=True)
    permission = policy.check_table(keyless_table())
    assert permission.reason is BlockedReason.READ_ONLY
    assert "read-only" in permission.message
    assert "f5" in permission.message  # tells the user how to change it


def test_a_write_permission_always_carries_wording() -> None:
    """A refusal the UI cannot explain is a refusal the user works around."""
    assert WritePermission.allow().message == ""
    denied = WritePermission.deny(BlockedReason.VIEW, "views are read-only")
    assert denied.allowed is False
    assert denied.message == "views are read-only"


# -- the Apply verdict ------------------------------------------------------


def test_an_ordinary_apply_needs_no_typed_confirmation() -> None:
    verdict = writable().review([keyed_table()], {ChangeKind.UPDATE: 3})
    assert verdict.allowed is True
    assert verdict.confirmation.required is False
    assert verdict.confirmation.word is None


def test_the_verdict_summarises_the_counts_and_the_tables() -> None:
    """FR-7.5: the dialog names what changes, not merely how much."""
    verdict = writable().review(
        [keyed_table()], {ChangeKind.INSERT: 2, ChangeKind.UPDATE: 1, ChangeKind.DELETE: 0}
    )
    assert verdict.summary == "2 inserts / 1 update"
    assert verdict.tables == ("dbo.Country",)


def test_an_empty_change_set_is_refused() -> None:
    verdict = writable().review([keyed_table()], {})
    assert verdict.allowed is False
    assert "nothing staged" in verdict.message


def test_a_read_only_session_refuses_the_whole_apply() -> None:
    verdict = writable(read_only=True).review([keyed_table()], {ChangeKind.UPDATE: 1})
    assert verdict.allowed is False
    assert verdict.reason is BlockedReason.READ_ONLY
    assert verdict.tables == ("dbo.Country",)  # still reports what would have changed


def test_a_keyless_table_refuses_the_whole_apply() -> None:
    verdict = writable().review([keyless_table()], {ChangeKind.DELETE: 1})
    assert verdict.allowed is False
    assert verdict.reason is BlockedReason.NO_IDENTITY


def test_production_requires_the_typed_word_even_when_writable() -> None:
    policy = SafetyPolicy(environment=Environment.PRODUCTION, read_only=False)
    verdict = policy.review([keyed_table()], {ChangeKind.UPDATE: 1})
    assert verdict.allowed is True
    assert verdict.confirmation.word == PRODUCTION_CONFIRM_WORD
    assert "PRODUCTION" in verdict.confirmation.reason


def test_a_big_delete_batch_escalates_outside_production() -> None:
    """S-2: "delete 60 rows" must not read like "delete 6"."""
    verdict = writable(delete_confirm_threshold=50).review([keyed_table()], {ChangeKind.DELETE: 60})
    assert verdict.confirmation.word == "DELETE"
    assert "exceed" in verdict.confirmation.reason


def test_a_delete_batch_at_the_threshold_is_not_escalated() -> None:
    """The rule is *above* the threshold, so the boundary itself stays quiet."""
    verdict = writable(delete_confirm_threshold=50).review([keyed_table()], {ChangeKind.DELETE: 50})
    assert verdict.confirmation.required is False


def test_the_threshold_is_configurable() -> None:
    verdict = writable(delete_confirm_threshold=5).review([keyed_table()], {ChangeKind.DELETE: 6})
    assert verdict.confirmation.word == "DELETE"


def test_a_verdict_holding_only_names_still_reports_them() -> None:
    """A caller without metadata still gets the counts and the table list."""
    verdict = writable().review(["dbo.Country"], {ChangeKind.INSERT: 1})
    assert verdict.allowed is True
    assert verdict.tables == ("dbo.Country",)


def test_the_blocked_reason_alias_matches_the_reason() -> None:
    verdict = writable(read_only=True).review([keyed_table()], {ChangeKind.UPDATE: 1})
    assert verdict.blocked_reason is verdict.reason


def test_a_typed_confirmation_reports_whether_it_is_required() -> None:
    assert TypedConfirmation().required is False
    assert TypedConfirmation(word="DELETE").required is True
    assert TypedConfirmation(word="DELETE").label == "type DELETE"
    assert TypedConfirmation().label == "confirm"


# -- the summary and the table list -----------------------------------------


def test_the_summary_omits_zero_counts() -> None:
    """Three numbers are readable; "1 update / 0 deletes" is not."""
    assert describe_plan({ChangeKind.INSERT: 1, ChangeKind.UPDATE: 1, ChangeKind.DELETE: 0}) == (
        "1 insert / 1 update"
    )


def test_the_summary_singularizes_one() -> None:
    assert describe_plan({ChangeKind.DELETE: 1}) == "1 delete"


def test_an_empty_summary_says_nothing() -> None:
    assert describe_plan({}) == "nothing"


def test_affected_tables_are_deduplicated_and_sorted() -> None:
    names = affected_tables([keyed_table("Zebra"), keyed_table("Apple"), keyed_table("Zebra")])
    assert names == ("dbo.Apple", "dbo.Zebra")


def test_affected_tables_accepts_plain_names() -> None:
    assert affected_tables(["dbo.B", "dbo.A"]) == ("dbo.A", "dbo.B")


def test_affected_tables_mixes_objects_and_names() -> None:
    assert affected_tables([keyed_table(), "dbo.Alpha"]) == ("dbo.Alpha", "dbo.Country")
