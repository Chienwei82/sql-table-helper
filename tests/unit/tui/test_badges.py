"""Table-picker badges: glyphs are the contract, colour only reinforces (FR-2.3, FR-3.7)."""

import pytest

from sql_table_swiss_knife.domain import TableKind, TableSummary
from sql_table_swiss_knife.tui.widgets import badges_for, node_label

PLAIN = TableSummary("dbo", "AuditLog", TableKind.BASE_TABLE)
WITH_PK = TableSummary("dbo", "Country", TableKind.BASE_TABLE, has_primary_key=True)
EVERYTHING = TableSummary(
    "sales",
    "Order",
    TableKind.BASE_TABLE,
    has_primary_key=True,
    has_foreign_keys=True,
    has_triggers=True,
    approximate_row_count=1234,
)
VIEW = TableSummary("reporting", "v_Customer", TableKind.VIEW)


def test_badges_of_a_table_with_everything() -> None:
    """Badge order is fixed so the picker reads consistently."""
    assert [badge.glyph for badge in badges_for(EVERYTHING)] == ["🔑", "🔗", "⚡"]
    assert [badge.role for badge in badges_for(EVERYTHING)] == ["pk", "fk", "warning"]


def test_missing_primary_key_is_the_alarming_badge() -> None:
    """No PK means read-only rows (S-4), and it replaces the 🔑 badge."""
    assert [badge.glyph for badge in badges_for(PLAIN)] == ["⚠"]
    assert badges_for(PLAIN)[0].label.endswith("read-only")


def test_view_is_marked_as_read_only_too() -> None:
    assert "👁" in [badge.glyph for badge in badges_for(VIEW)]


def test_badge_labels_explain_themselves() -> None:
    labels = {badge.glyph: badge.label for badge in badges_for(EVERYTHING)}
    assert labels["🔑"] == "primary key"
    assert labels["🔗"] == "foreign key"
    assert labels["⚡"] == "has triggers"


def test_node_label_shows_name_badges_and_row_estimate() -> None:
    text = node_label(EVERYTHING).plain
    assert "Order" in text
    assert "🔑" in text and "⚡" in text and "🔗" in text
    assert "rows≈1234" in text


def test_node_label_omits_the_row_estimate_when_unknown() -> None:
    assert "rows" not in node_label(WITH_PK).plain


def test_node_label_without_a_palette_is_still_readable() -> None:
    """Colour is optional: the glyphs alone carry the meaning (FR-3.7)."""
    assert node_label(WITH_PK, {}).plain == node_label(WITH_PK).plain


@pytest.mark.parametrize("summary", [PLAIN, WITH_PK, EVERYTHING, VIEW])
def test_every_summary_renders_without_a_palette(summary: TableSummary) -> None:
    assert node_label(summary).plain
