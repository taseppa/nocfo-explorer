"""Modal screens: first-run wizard (token/business), input, confirm, info.

All of them dismiss with a single value the caller awaits — ``None`` (or
``False`` for the confirm/auth screens) always means "cancelled".
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
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
    """Yes/no confirmation.  Enter does nothing, so ``y`` is deliberate."""

    CSS = """
    Screen { align: center middle; }
    #box { width: 76; border: round $accent; padding: 1 2; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n,escape", "no", "No"),
    ]

    def __init__(self, title: str, detail: str = ""):
        super().__init__()
        self.title_text = title
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"[b]{self.title_text}[/b]")
            if self.detail:
                yield Label(self.detail)
            yield Label("[b]y[/b] yes     [b]n[/b] no", id="hint")
        yield Footer()

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


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
