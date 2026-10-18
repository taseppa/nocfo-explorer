"""Modal screens: first-run wizard (token/business), input, confirm, info.

All of them dismiss with a single value the caller awaits — ``None`` (or
``False`` for the confirm/auth screens) always means "cancelled".
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option


class TokenScreen(Screen[str | None]):
    """Ask for the NoCFO personal access token (first run)."""

    CSS = """
    Screen { align: center middle; }
    #box { width: 76; border: round $accent; padding: 1 2; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("NoCFO personal access token")
            yield Input(
                password=True, placeholder="paste token and press enter", id="token"
            )
            yield Label("Saved to your local config.toml (git-ignored).", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            self.notify("token cannot be empty", severity="warning")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BusinessPickerScreen(Screen[str | None]):
    """Choose which accessible business to explore."""

    CSS = """
    Screen { align: center middle; }
    #box { width: 76; border: round $accent; padding: 1 2; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, businesses: list[dict]):
        super().__init__()
        self.businesses = businesses

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("Select the business to explore")
            options = [
                Option(
                    f"{b.get('name', '?')}  [dim]({b.get('slug', '?')})[/dim]",
                    id=str(b.get("slug")),
                )
                for b in self.businesses
            ]
            yield OptionList(*options, id="biz")
        yield Footer()

    def on_mount(self) -> None:
        picker = self.query_one(OptionList)
        picker.highlighted = 0  # deterministic first option for enter/mouse
        picker.focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option.id is not None:
            self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AuthScreen(Screen[bool]):
    """The token was rejected or NoCFO is unreachable."""

    CSS = """
    Screen { align: center middle; }
    #box { width: 76; border: round $error; padding: 1 2; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("r", "retry", "Retry"),
        Binding("q,escape", "quit", "Quit"),
    ]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("[red]NoCFO refused the request[/red]")
            yield Label(self.message)
            yield Label(
                "Press [b]r[/b] to enter a different token, [b]q[/b] to quit.",
                id="hint",
            )
        yield Footer()

    def action_retry(self) -> None:
        self.dismiss(True)

    def action_quit(self) -> None:
        self.dismiss(False)


class InputScreen(Screen[str | None]):
    """Single-line input with a title; returns the value or None."""

    CSS = """
    Screen { align: center middle; }
    #box { width: 76; border: round $accent; padding: 1 2; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        title: str,
        value: str = "",
        placeholder: str = "",
        cursor_position: int | None = None,
        hint: str = "",
    ):
        super().__init__()
        self.title_text = title
        self.value = value
        self.placeholder = placeholder
        self.cursor_position = cursor_position
        self.hint_text = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.title_text)
            yield Input(value=self.value, placeholder=self.placeholder, id="value")
            if self.hint_text:
                yield Label(self.hint_text, id="hint")
        yield Footer()

    def on_mount(self) -> None:
        field = self.query_one(Input)
        field.focus()
        if self.cursor_position is not None:
            try:
                field.cursor_position = max(0, self.cursor_position)
            except AttributeError:  # very old Textual: leave cursor at the end
                pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(Screen[bool]):
    """Yazi-style centered popup: the doomed items, then a Yes/No selector.

    **Yes** is highlighted by default.  ``y`` decides immediately, ``n`` /
    ``escape`` cancel, ``enter`` activates the highlighted side, and
    ``left``/``right``/``tab`` move the highlight.
    """

    CSS = """
    Screen { align: center middle; background: $background 45%; }
    #box {
        width: 76;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #detail-scroll {
        height: auto;
        max-height: 16;
    }
    #detail {
        color: $text-muted;
    }
    #buttons {
        height: 1;
        margin-top: 1;
    }
    #buttons Label {
        margin-right: 2;
        color: $text-muted;
    }
    #buttons Label.selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    #hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n,escape", "no", "No"),
        Binding("enter", "activate", "Confirm"),
        Binding("left", "select_yes", show=False),
        Binding("right", "select_no", show=False),
        Binding("tab", "toggle", show=False),
    ]

    def __init__(self, title: str, detail: str = ""):
        super().__init__()
        self.title_text = title
        self.detail = detail
        self.choice = True  # "Yes" starts highlighted, like yazi

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"[b]{self.title_text}[/b]")
            if self.detail:
                with VerticalScroll(id="detail-scroll"):
                    yield Static(self.detail, markup=False, id="detail")
            with Horizontal(id="buttons"):
                yield Label(" Yes ", id="btn-yes")
                yield Label(" No ", id="btn-no")
            yield Label("y/n · ←/→ moves · enter confirms", id="hint")

    def on_mount(self) -> None:
        self.query_one("#detail-scroll", VerticalScroll).can_focus = False
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.query_one("#btn-yes", Label).set_class(self.choice, "selected")
        self.query_one("#btn-no", Label).set_class(not self.choice, "selected")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def action_activate(self) -> None:
        self.dismiss(self.choice)

    def action_select_yes(self) -> None:
        self.choice = True
        self._refresh_buttons()

    def action_select_no(self) -> None:
        self.choice = False
        self._refresh_buttons()

    def action_toggle(self) -> None:
        self.choice = not self.choice
        self._refresh_buttons()


class InfoScreen(Screen[None]):
    """Plain text panel, dismissed with q, esc or enter."""

    CSS = """
    #info { padding: 1 2; }
    """

    BINDINGS = [Binding("q,escape,enter", "close", "Back")]

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        yield Static(self.text, id="info", markup=False)
        yield Footer()

    def action_close(self) -> None:
        self.dismiss(None)
