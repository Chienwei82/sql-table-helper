"""Themes: three built-ins plus runtime switching and persistence (FR-3.7).

Each theme declares the same *semantic* palette — the roles the UI needs to talk
about (primary key, foreign key, identity, computed, nullable, error, warning,
pending change) — so a screen never hard-codes a colour: it names a role and the
active theme decides what that looks like. Textual adds a ``-theme-<name>`` class
to the app when a theme is activated, which is how theme-specific CSS files in
``tui/themes/`` attach themselves.
"""

from textual.theme import Theme

from ..storage import Settings, SettingsError, SettingsStore

__all__ = [
    "DEFAULT_THEME",
    "SEMANTIC_ROLES",
    "THEMES",
    "apply_theme",
    "cycle_theme",
    "load_theme",
    "next_theme",
    "register_themes",
    "theme_names",
]

#: Theme activated on first run (also the default in ``settings.toml``).
DEFAULT_THEME = "default-dark"

#: Semantic colour roles every theme must define (DESIGN §9.4).
SEMANTIC_ROLES = (
    "pk",
    "fk",
    "identity",
    "computed",
    "nullable",
    "error",
    "warning",
    "pending",
)

_DARK: Theme = Theme(
    name="default-dark",
    primary="#4a9eff",
    secondary="#7a5cff",
    accent="#ffb454",
    warning="#ffb454",
    error="#ff6b6b",
    success="#4ec9a5",
    foreground="#e6e6e6",
    background="#12141a",
    surface="#1b1e26",
    panel="#232733",
    dark=True,
    variables={
        # Semantic palette: readable on both $surface and $panel.
        "pk": "#ffd166",
        "fk": "#7a5cff",
        "identity": "#4ec9a5",
        "computed": "#9aa4b2",
        "nullable": "#6c7686",
        "error": "#ff6b6b",
        "warning": "#ffb454",
        "pending": "#4a9eff",
        "muted": "#8b93a3",
    },
)

_LIGHT: Theme = Theme(
    name="light",
    primary="#0b62d6",
    secondary="#5b3fd6",
    accent="#9a5b00",
    warning="#9a5b00",
    error="#c02626",
    success="#0f7a52",
    foreground="#1b1f24",
    background="#f7f8fa",
    surface="#ffffff",
    panel="#eceff3",
    dark=False,
    variables={
        # Same roles, re-picked for contrast on a light background.
        "pk": "#8a5a00",
        "fk": "#5b3fd6",
        "identity": "#0f7a52",
        "computed": "#5c6673",
        "nullable": "#7b8595",
        "error": "#c02626",
        "warning": "#9a5b00",
        "pending": "#0b62d6",
        "muted": "#5c6673",
    },
)

_HIGH_CONTRAST: Theme = Theme(
    name="high-contrast",
    primary="#ffffff",
    secondary="#ffd700",
    accent="#ffd700",
    warning="#ffd700",
    error="#ff5c5c",
    success="#00ff9c",
    foreground="#ffffff",
    background="#000000",
    surface="#101010",
    panel="#1c1c1c",
    dark=True,
    luminosity_spread=0.0,
    text_alpha=1.0,
    variables={
        # Maximum separation: every role differs in *lightness* too, so the
        # themes stay readable without relying on hue alone (FR-3.7).
        "pk": "#ffd700",
        "fk": "#00bfff",
        "identity": "#00ff9c",
        "computed": "#ffffff",
        "nullable": "#9e9e9e",
        "error": "#ff5c5c",
        "warning": "#ffd700",
        "pending": "#00bfff",
        "muted": "#c0c0c0",
    },
)

#: All built-in themes, in the order the cycle command walks them.
THEMES: dict[str, Theme] = {theme.name: theme for theme in (_DARK, _LIGHT, _HIGH_CONTRAST)}


def theme_names() -> tuple[str, ...]:
    """Names of the built-in themes, in cycle order."""
    return tuple(THEMES)


def register_themes(app: object) -> None:
    """Register the built-in themes with a Textual app.

    Typed loosely to keep this module importable without importing ``textual.app``
    at module scope (the tests use it with a stub).
    """
    for theme in THEMES.values():
        app.register_theme(theme)  # type: ignore[attr-defined]


def next_theme(current: str) -> str:
    """The theme that follows ``current`` in cycle order (wraps around)."""
    names = theme_names()
    try:
        index = names.index(current)
    except ValueError:
        return DEFAULT_THEME
    return names[(index + 1) % len(names)]


def cycle_theme(current: str) -> str:
    """Alias of :func:`next_theme` reading better at the call site."""
    return next_theme(current)


def load_theme(settings_store: SettingsStore, app_theme: str | None = None) -> str:
    """Resolve the theme to start with.

    Precedence: explicit argument (tests, ``--theme``) → persisted setting → default.
    An unknown persisted name falls back to the default instead of crashing at
    startup; the caller surfaces the fallback as an informational toast.
    """
    if app_theme is not None:
        return app_theme if app_theme in THEMES else DEFAULT_THEME
    try:
        stored = settings_store.load().theme
    except SettingsError:
        return DEFAULT_THEME
    return stored if stored in THEMES else DEFAULT_THEME


def apply_theme(settings_store: SettingsStore, settings: Settings, name: str) -> Settings:
    """Persist ``name`` as the active theme and return the updated settings.

    Raises:
        SettingsError: if the name is not a registered theme — switching to an
            unknown theme is a programming error, not a user choice.
    """
    if name not in THEMES:
        raise SettingsError(f"unknown theme {name!r}; available: {', '.join(theme_names())}")
    return settings_store.update(settings, theme=name)
