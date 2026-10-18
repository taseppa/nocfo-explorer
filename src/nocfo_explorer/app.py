"""The ``nocfo-explorer`` TUI: a yazi-style three-pane NoCFO browser.

Layout (all interaction happens in the middle pane, like yazi):

* **parent** — read-only listing of the parent folder, current folder marked.
* **current** — folders then files of the open folder; owns every key.
* **preview** — hovered item: metadata header + a lazily fetched text
  preview (or folder contents) below it, scrolled with ``J``/``K``.

Navigation is ``h``/``l`` (leave/enter), ``H``/``L`` (history), ``j``/``k``
(cursor), ``gg``/``G``.  ``space``/``ctrl-a``/``ctrl-r`` maintain the mark set
that the operation keys (yank/cut/paste/delete/rename/new-folder/search —
wired up in the operations module) act on.

Startup flows through the first-run wizard (screens.py) when the config has
no token/business yet: the token is validated against ``GET /v1/user/``, the
business is picked from ``GET /v1/business/`` (auto-chosen when there is only
one) and both are saved to config.toml.
"""

from __future__ import annotations

import asyncio
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from . import ops
from .config import Config, ConfigError, save_config
from .nocfo import NoCfoClient, NoCfoError, NocfoFile, NocfoFolder
from .screens import (
    AuthScreen,
    BusinessPickerScreen,
    ConfirmScreen,
    InfoScreen,
    InputScreen,
    TokenScreen,
)

LIST_TTL = 30.0  # seconds a folder listing stays fresh in the cache
PREVIEW_DEBOUNCE = 0.18  # hover time before the preview is fetched
PREVIEW_MAX_BYTES = 256 * 1024  # text preview download cap
PREVIEW_MAX_LINES = 500

# Extensions we show as a text preview even when the MIME type is unhelpful.
TEXT_EXTS = {
    "txt", "md", "csv", "tsv", "log", "json", "xml", "yaml", "yml", "toml",
    "ini", "cfg", "conf", "env", "py", "js", "ts", "sh", "sql", "html", "css",
    "gitignore", "rst", "rtf",
}

HINT = "space mark · h/l dir · y/x yank · p paste · d del · / search · q quit"


@dataclass
class Entry:
    """One row in a listing (folder or file)."""

    kind: str  # "folder" | "file"
    id: int | None  # None only for the virtual root
    name: str
    mime: str = ""
    file: NocfoFile | None = field(default=None, repr=False)

    @property
    def key(self) -> tuple[str, int | None]:
        return (self.kind, self.id)


ROOT = Entry("folder", None, "root")


def build_entries(
    folders: list[NocfoFolder], files: list[NocfoFile]
) -> list[Entry]:
    """Folders first, then files — each alphabetically (see module docstring)."""
    folder_ids = {f.id for f in folders}
    out = [
        Entry("folder", f.id, f.name)
        for f in sorted(folders, key=lambda f: f.name.lower())
    ]
    out += [
        Entry("file", f.id, f.name, mime=f.mime, file=f)
        for f in sorted(files, key=lambda f: f.name.lower())
        if f.id not in folder_ids  # defensive: children may repeat a file id
    ]
    return out


def is_text_preview(name: str, mime: str) -> bool:
    """Whether the file's content can be shown as text."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else name.lower()
    if ext in TEXT_EXTS:
        return True
    return mime.startswith("text/") or mime.endswith("+json") or mime.endswith("+xml")


def rename_cursor_position(name: str) -> int:
    """Yazi puts the rename cursor before the last extension."""
    stem, dot, _ext = name.rpartition(".")
    return len(stem) if dot and stem else len(name)


class _Pane(OptionList):
    """OptionList whose cursor stops at the edges instead of wrapping.

    Textual's default ``cursor_down``/``cursor_up`` cycle around the list;
    yazi keeps the cursor put at the top/bottom, and preview tests rely on
    that being deterministic.
    """

    DEFAULT_CSS = """
    _Pane {
        background: $background;
    }
    _Pane > .option-list--option {
        background: $background;
    }
    _Pane > .option-list--option-highlighted {
        background: $accent 40%;
    }
    _Pane > .option-list--option-selected {
        background: $accent;
    }
    """

    def action_cursor_down(self) -> None:
        if not self.option_count:
            return
        current = self.highlighted
        self.highlighted = (
            0 if current is None else min(current + 1, self.option_count - 1)
        )
        self.scroll_to_highlight()

    def action_cursor_up(self) -> None:
        if not self.option_count:
            return
        current = self.highlighted
        self.highlighted = (
            self.option_count - 1
            if current is None
            else max(current - 1, 0)
        )
        self.scroll_to_highlight()


class ExplorerApp(App[None]):
    """The three-pane browser."""

    TITLE = "nocfo-explorer"

    CSS = """
    #path {
        height: 1;
        padding: 0 1;
        background: $accent;
        color: $text;
    }
    #cols {
        height: 1fr;
        background: $background;
    }
    #parent {
        height: 100%;
        width: 26;
        min-width: 12;
        padding: 0 1;
        background: $background;
    }
    #current {
        height: 100%;
        width: 1fr;
        padding: 0 1;
        background: $background;
    }
    #previewcol {
        height: 100%;
        width: 44%;
        min-width: 24;
        background: $background;
    }
    #preview-head {
        height: auto;
        max-height: 11;
        padding: 1 1 0 1;
        background: $background;
    }
    #preview-scroll {
        height: 1fr;
        padding: 0 1 1 1;
        background: $background;
    }
    #preview-body {
        width: 100%;
        height: auto;
        color: $text-muted;
        background: $background;
    }
    #status {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        # navigation — yazi's keys
        Binding("h,left", "leave", "Parent dir"),
        Binding("l,right", "enter", "Open"),
        Binding("H", "back", "Back", show=False),
        Binding("L", "forward", "Forward", show=False),
        Binding("j", "cursor_next", "Down", show=False),
        Binding("k", "cursor_prev", "Up", show=False),
        Binding("ctrl+u", "half_up", show=False),
        Binding("ctrl+d", "half_down", show=False),
        # "gg" (go to top) is a key *chord*; Textual has no native sequence
        # bindings, so it is implemented manually in on_key.
        Binding("G", "last", show=False),
        # preview
        Binding("J", "seek_down", show=False),
        Binding("K", "seek_up", show=False),
        Binding("o", "open", "Open in browser"),
        # marks
        Binding("space", "toggle_mark", "Mark"),
        Binding("ctrl+a", "mark_all", show=False),
        Binding("ctrl+r", "invert_marks", show=False),
        Binding("escape", "escape", "Clear", show=False),
        # operations
        Binding("y", "yank", "Yank"),
        Binding("x", "cut", "Cut"),
        Binding("Y", "unyank", show=False),
        Binding("X", "unyank", show=False),
        Binding("p", "paste", "Paste"),
        Binding("P", "paste_force", show=False),
        Binding("d", "delete", "Delete"),
        Binding("a", "new_folder", show=False),
        Binding("r", "rename", show=False),
        # search
        Binding("/", "search", "Search"),
        # misc
        Binding("R", "reload", "Refresh", show=False),
    ]

    def __init__(
        self,
        cfg: Config,
        client_factory: Callable[..., Any] = NoCfoClient,
    ):
        super().__init__()
        self.cfg = cfg
        self.client_factory = client_factory
        self.client: Any | None = None

        self.path: list[Entry] = [ROOT]
        self.history: list[tuple[Entry, ...]] = [(ROOT,)]
        self.hist_i = 0

        self.entries: list[Entry] = []
        self.parent_entries: list[Entry] = []
        self.marks: dict[tuple[str, int | None], Entry] = {}
        # NB: named "yank", not "clipboard" — Textual's App already owns a
        # read-only ``clipboard`` property (system clipboard API).
        self.yank_items: list[Entry] | None = None
        self.yank_cut = False

        self.attached_ids: set[int] | None = None  # None = not fetched yet
        self._attached_failed = False

        # search mode: while set, the current pane shows whole-business
        # file matches instead of the open folder's listing.
        self.search_term: str | None = None
        self._search_prev_cursor_key: tuple[str, int | None] | None = None

        # caches: folder key -> (time, entries); folder id -> (time, detail)
        self._listing_cache: dict[int | None, tuple[float, list[Entry]]] = {}
        self._folder_detail_cache: dict[int, tuple[float, NocfoFolder]] = {}
        self._file_detail_cache: dict[int, NocfoFile] = {}
        self._text_cache: dict[int, str] = {}
        self._preview_cache: dict[tuple[str, int | None], tuple[Text, Text]] = {}

        self._load_seq = 0
        self._preview_seq = 0
        self._gg_pending = False  # saw "g"; a second "g" jumps to the top
        self._op_busy = False  # a mutation is running: ignore repeat keys

    # ------------------------------------------------------------------
    # compose / startup
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="path", markup=False)
        with Horizontal(id="cols"):
            yield _Pane(id="parent")
            yield _Pane(id="current")
            with Vertical(id="previewcol"):
                yield Static("", id="preview-head", markup=False)
                with VerticalScroll(id="preview-scroll"):
                    yield Static("", id="preview-body", markup=False)
        yield Static("", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        # Only the current pane is interactive: without this the first
        # focusable widget (#parent) grabs focus, and "enter" would select
        # in the read-only pane instead of acting on the cursor.
        self.query_one("#parent", OptionList).can_focus = False
        self.query_one("#preview-scroll", VerticalScroll).can_focus = False
        self.query_one("#current", OptionList).focus()
        self._update_path()
        self._update_status()
        self.run_worker(self._bootstrap)

    async def _bootstrap(self) -> None:
        if not self.cfg.token:
            if not await self._wizard_token():
                return
        elif self.client is None:
            self.client = self.client_factory(
                base_url=self.cfg.base_url,
                token=self.cfg.token,
                business_slug=self.cfg.business_slug,
            )
        if not self.cfg.business_slug:
            if not await self._wizard_business():
                return
        self.client.business_slug = self.cfg.business_slug
        self._update_path()
        self.reload()

    async def _wizard_token(self) -> bool:
        """Ask for a token until it validates (or the user gives up)."""
        while True:
            token = await self._ask(TokenScreen())
            if token is None:
                self.exit()
                return False
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
            self.client = self.client_factory(
                base_url=self.cfg.base_url,
                token=token,
                business_slug=self.cfg.business_slug,
            )
            try:
                await asyncio.to_thread(self.client.current_user)
            except NoCfoError as e:
                retry = await self._ask(AuthScreen(str(e)))
                if not retry:
                    self.exit()
                    return False
                continue
            self.cfg.token = token
            self._save_quietly()
            return True

    async def _wizard_business(self) -> bool:
        try:
            businesses = await asyncio.to_thread(self.client.list_businesses)
        except NoCfoError as e:
            await self._ask(InfoScreen(f"Could not list businesses:\n{e}"))
            self.exit()
            return False
        if not businesses:
            await self._ask(InfoScreen("This token can access no businesses."))
            self.exit()
            return False
        if len(businesses) == 1:
            slug = str(businesses[0].get("slug", ""))
        else:
            slug = await self._ask(BusinessPickerScreen(businesses))
            if slug is None:
                self.exit()
                return False
        self.cfg.business_slug = slug
        self._save_quietly()
        return True

    async def _ask(self, screen: Any) -> Any:
        """Push a modal screen and wait for its dismissed value."""
        result: dict[str, Any] = {}
        done = asyncio.Event()

        def callback(value: Any) -> None:
            result["v"] = value
            done.set()

        self.push_screen(screen, callback)
        await done.wait()
        return result.get("v")

    def _save_quietly(self) -> None:
        try:
            save_config(self.cfg)
        except (ConfigError, OSError) as e:
            self.notify(f"could not save config: {e}", severity="warning")

    # ------------------------------------------------------------------
    # listings
    # ------------------------------------------------------------------

    def reload(self, *, cursor_key=None, cursor_name: str | None = None) -> None:
        """Fetch the current + parent listings in a worker thread."""
        if self.client is None:
            return
        self._load_seq += 1
        seq = self._load_seq
        path = tuple(self.path)
        self.run_worker(
            lambda: self._load_worker(seq, path, cursor_key, cursor_name),
            thread=True,
            group="load",
            exclusive=True,
        )

    def _load_worker(
        self,
        seq: int,
        path: tuple[Entry, ...],
        cursor_key,
        cursor_name: str | None,
    ) -> None:
        try:
            entries = self._fetch_listing(path[-1].id)
            if len(path) >= 2:
                parent_entries = self._fetch_listing(path[-2].id)
            else:
                parent_entries = []
        except Exception as e:  # NoCfoError or anything unexpected
            self._post(lambda: self._apply_load_error(seq, str(e)))
            return
        self._post(
            lambda: self._apply_load(
                seq, entries, parent_entries, cursor_key, cursor_name
            )
        )

    def _post(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` on the app thread (called from worker threads)."""
        try:
            self.call_from_thread(callback)
        except RuntimeError:
            pass  # the app is shutting down

    def _fetch_listing(self, folder_id: int | None) -> list[Entry]:
        """Folder + file entries, cached for LIST_TTL seconds (worker thread)."""
        now = time.monotonic()
        hit = self._listing_cache.get(folder_id)
        if hit is not None and now - hit[0] < LIST_TTL:
            return hit[1]
        if folder_id is None:
            folders = self.client.list_root_folders()
            files = list(self.client.list_files(root=True))
        else:
            detail = self._folder_detail(folder_id)
            folders = [
                NocfoFolder(id=c["id"], name=c["name"], parent=folder_id)
                for c in (detail.children or [])
                if isinstance(c, dict) and c.get("id") is not None
            ]
            files = list(self.client.list_files(folder=folder_id))
        entries = build_entries(folders, files)
        self._listing_cache[folder_id] = (now, entries)
        return entries

    def _folder_detail(self, folder_id: int) -> NocfoFolder:
        now = time.monotonic()
        hit = self._folder_detail_cache.get(folder_id)
        if hit is not None and now - hit[0] < LIST_TTL:
            return hit[1]
        detail = self.client.get_folder(folder_id)
        self._folder_detail_cache[folder_id] = (now, detail)
        return detail

    def _file_detail(self, file_id: int) -> NocfoFile:
        hit = self._file_detail_cache.get(file_id)
        if hit is not None:
            return hit
        detail = self.client.get_file(file_id)
        self._file_detail_cache[file_id] = detail
        return detail

    def invalidate(self) -> None:
        """Drop every cached listing/detail/preview (after a mutation)."""
        self._listing_cache.clear()
        self._folder_detail_cache.clear()
        self._file_detail_cache.clear()
        self._text_cache.clear()
        self._preview_cache.clear()

    def _apply_load(
        self,
        seq: int,
        entries: list[Entry],
        parent_entries: list[Entry],
        cursor_key,
        cursor_name: str | None,
    ) -> None:
        if seq != self._load_seq:
            return  # superseded by a newer navigation
        previous = self._highlighted_entry()  # against the OLD entries
        self.search_term = None  # a folder load ends search mode
        self.entries = entries
        self.parent_entries = parent_entries
        self._render_current(
            cursor_key=cursor_key, cursor_name=cursor_name, previous=previous
        )
        self._render_parent()
        self._update_path()
        self._update_status()

    def _apply_load_error(self, seq: int, message: str) -> None:
        if seq != self._load_seq:
            return
        self.search_term = None
        self.entries = []
        self.parent_entries = []
        self._render_current()
        self._render_parent()
        self._update_path()
        self._update_status(error=message)
        self.notify(f"load failed: {message}", severity="error")

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _option(self, e: Entry) -> Option:
        marked = e.key in self.marks
        prompt = Text("• " if marked else "  ", style="bold cyan" if marked else "")
        if e.kind == "folder":
            prompt.append(e.name + "/", style="bold")
        else:
            prompt.append(e.name)
        return Option(prompt, id=f"{e.kind}:{e.id}")

    def _render_current(
        self,
        cursor_key=None,
        cursor_name: str | None = None,
        previous: Entry | None | object = ...,
    ) -> None:
        cl = self.query_one("#current", OptionList)
        if previous is ...:
            previous = self._highlighted_entry()  # entries unchanged: keep cursor
        cl.clear_options()
        if self.entries:
            cl.add_options([self._option(e) for e in self.entries])
        idx = self._cursor_index(cursor_key, cursor_name, previous)  # type: ignore[arg-type]
        cl.highlighted = idx if self.entries else None
        if idx is not None:
            cl.scroll_to_highlight()
        if not self.entries:
            # nothing hovered: preview the open folder itself
            self._schedule_preview(self.path[-1])

    def _cursor_index(self, cursor_key, cursor_name, previous) -> int | None:
        """Where the cursor lands: by key, then by name, then the old spot,
        then the top (yazi puts the cursor at the top after entering a
        folder — the old spot is simply the folder we came from, which is
        not in the new listing, so it falls through to 0)."""
        if not self.entries:
            return None
        if cursor_key is not None:
            for i, e in enumerate(self.entries):
                if e.key == cursor_key:
                    return i
        if cursor_name is not None:
            for i, e in enumerate(self.entries):
                if e.name == cursor_name:
                    return i
        if isinstance(previous, Entry):
            for i, e in enumerate(self.entries):
                if e.key == previous.key:
                    return i
        return 0

    def _render_parent(self) -> None:
        pl = self.query_one("#parent", OptionList)
        pl.clear_options()
        if not self.parent_entries:
            return
        pl.add_options([self._option(e) for e in self.parent_entries])
        current_key = self.path[-1].key
        for i, e in enumerate(self.parent_entries):
            if e.key == current_key:
                pl.highlighted = i
                break

    def _update_path(self) -> None:
        business = self.cfg.business_slug or "setup"
        path_str = "/" + "/".join(seg.name for seg in self.path[1:])
        t = Text()
        t.append("nocfo-explorer", style="bold")
        t.append(f" · {business} · ", style="dim")
        t.append(path_str)
        if self.search_term is not None:
            t.append(f"   [search: {self.search_term}]", style="bold cyan")
            t.append("  (esc exits)", style="dim")
        self.query_one("#path", Static).update(t)

    def _update_status(self, error: str | None = None) -> None:
        parts: list[tuple[str, str]] = []
        if error:
            parts.append((error, "bold red"))
        else:
            parts.append((HINT, "dim"))
        if self.marks:
            parts.append((f"{len(self.marks)} marked", "bold cyan"))
        if self.yank_items:
            verb = "cut" if self.yank_cut else "yank"
            parts.append((f"{verb}: {len(self.yank_items)} items", "bold yellow"))
        t = Text()
        for i, (text, style) in enumerate(parts):
            if i:
                t.append("  ·  ")
            t.append(text, style=style)
        self.query_one("#status", Static).update(t)

    # ------------------------------------------------------------------
    # hover / preview
    # ------------------------------------------------------------------

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        control = event.control
        if control is None or control.id != "current":
            return
        entry = self._highlighted_entry()
        if entry is not None:
            self._schedule_preview(entry)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        control = event.control
        entry = self._entry_from_option(getattr(event, "option", None), control)
        if entry is None:
            return
        if entry.kind == "folder":
            self._descend(entry)
        elif control is not None and control.id == "current":
            if self.search_term is not None:
                self._jump_to_entry(entry)
            else:
                self._open_in_browser(entry)

    def _entry_from_option(self, option, control) -> Entry | None:
        if option is None or option.id is None:
            return None
        key = _parse_option_id(option.id)
        if key is None:
            return None
        pool = self.entries if (control and control.id == "current") else self.parent_entries
        for e in pool:
            if e.key == key:
                return e
        return None

    def _highlighted_entry(self) -> Entry | None:
        cl = self.query_one("#current", OptionList)
        idx = cl.highlighted
        if idx is None or not (0 <= idx < len(self.entries)):
            return None
        return self.entries[idx]

    def _schedule_preview(self, entry: Entry) -> None:
        self._preview_seq += 1
        seq = self._preview_seq
        cached = self._preview_cache.get(entry.key)
        if cached is not None:
            self._apply_preview(seq, cached[0], cached[1])
            return
        self.set_timer(
            PREVIEW_DEBOUNCE,
            lambda: self._start_preview(entry, seq),
        )

    def _start_preview(self, entry: Entry, seq: int) -> None:
        if seq != self._preview_seq or self.client is None:
            return
        self.run_worker(
            lambda: self._preview_worker(entry, seq),
            thread=True,
            group="preview",
            exclusive=True,
        )

    def _preview_worker(self, entry: Entry, seq: int) -> None:
        try:
            head, body = self._build_preview(entry)
        except Exception as e:  # never let a preview crash the app
            head = Text(entry.name + "\n", style="bold")
            body = Text(f"preview failed: {e}", style="red")
        self._post(lambda: self._apply_preview(seq, head, body))

    def _apply_preview(self, seq: int, head: Text, body: Text) -> None:
        if seq != self._preview_seq:
            return
        self.query_one("#preview-head", Static).update(head)
        self.query_one("#preview-body", Static).update(body)
        self.query_one("#preview-scroll", VerticalScroll).scroll_to(y=0, animate=False)

    def _build_preview(self, entry: Entry) -> tuple[Text, Text]:
        if entry.kind == "folder":
            return self._preview_folder(entry)
        return self._preview_file(entry)

    def _preview_folder(self, entry: Entry) -> tuple[Text, Text]:
        head = Text()
        head.append(entry.name + "/\n", style="bold")
        children = self._fetch_listing(entry.id)  # worker thread; also prefetches
        if entry.id is None:
            head.append(f"root folder · {self.cfg.business_slug}\n", style="dim")
        else:
            detail = self._folder_detail(entry.id)
            head.append(f"id {entry.id}")
            if detail.parent is not None:
                head.append(f" · parent {detail.parent}")
            head.append("\n", style="dim")
            crumbs = [
                c for c in (detail.breadcrumbs or []) if isinstance(c, dict)
            ]
            if crumbs:
                names = [str(c.get("name", c.get("id", "?"))) for c in crumbs]
                has_self = any(c.get("id") == entry.id for c in crumbs)
                parts = names if has_self else [*names, entry.name]
                head.append("/" + "/".join(parts) + "\n", style="dim")
        n_folders = sum(1 for e in children if e.kind == "folder")
        n_files = len(children) - n_folders
        head.append(f"{n_folders} folders, {n_files} files\n", style="dim")
        if not children:
            return head, Text("(empty folder)")
        lines = Text()
        for e in children:
            if e.kind == "folder":
                lines.append(e.name + "/\n", style="bold")
            else:
                lines.append(e.name + "\n")
        return head, lines

    def _preview_file(self, entry: Entry) -> tuple[Text, Text]:
        f = self._file_detail(entry.id) if entry.id is not None else entry.file
        name = f.name or entry.name
        mime = f.mime or entry.mime

        # attachment sweep — one paginated call per session, best effort
        if self.attached_ids is None and not self._attached_failed:
            try:
                self.attached_ids = self.client.attached_file_ids()
            except NoCfoError:
                self._attached_failed = True

        head = Text()
        head.append(name + "\n", style="bold")
        head.append(f"id {f.id} · ")
        head.append(mime or "unknown type")
        head.append("\n", style="dim")
        if f.created_at:
            head.append(f"created {f.created_at}\n", style="dim")
        if f.updated_at:
            head.append(f"updated {f.updated_at}\n", style="dim")
        if f.notes:
            head.append(f"notes: {f.notes}\n", style="italic")
        if f.is_deletable is False:
            head.append("not deletable (locked)\n", style="bold red")
        elif self.attached_ids is not None and f.id in self.attached_ids:
            head.append("attached to a document\n", style="yellow")
        if f.preview_url:
            head.append(f"preview {f.preview_url}\n", style="dim")

        if is_text_preview(name, mime):
            text = self._text_content(f)
            if not text:
                return head, Text("(empty file)")
            lines = text.splitlines()
            shown = lines[:PREVIEW_MAX_LINES]
            body = Text("\n".join(shown))
            if len(lines) > PREVIEW_MAX_LINES:
                body.append(
                    f"\n… preview truncated ({len(lines)} lines total)",
                    style="dim",
                )
            return head, body
        if mime.startswith("image/"):
            body = Text("(image — no text preview)")
            if f.preview_url:
                body.append(f"\n{f.preview_url}", style="dim")
            return head, body
        return head, Text(f"(no preview for {mime or 'binary file'})")

    def _text_content(self, f: NocfoFile) -> str:
        hit = self._text_cache.get(f.id)
        if hit is not None:
            return hit
        content = self.client.download_file(f, max_bytes=PREVIEW_MAX_BYTES)
        text = content.decode("utf-8", errors="replace")
        self._text_cache[f.id] = text
        return text

    def on_key(self, event: events.Key) -> None:
        """Manual ``gg`` chord — Textual has no native key sequences.

        Any other key between the two g's resets the chord, so ``g j g``
        does not fire it (App.on_key sees every key, even ones consumed
        by focused widget bindings).  Modal screens keep their own keys.
        """
        if len(self.screen_stack) > 1:
            self._gg_pending = False
            return
        if event.key == "g":
            if self._gg_pending:
                self._gg_pending = False
                self.action_first()
            else:
                self._gg_pending = True
        else:
            self._gg_pending = False

    def on_key(self, event: events.Key) -> None:
        """Manual ``gg`` chord — Textual has no native key sequences.

        Any other key between the two g's resets the chord, so ``g j g``
        does not fire it (App.on_key sees every key, even ones consumed
        by focused widget bindings).  Modal screens keep their own keys.
        """
        if len(self.screen_stack) > 1:
            self._gg_pending = False
            return
        if event.key == "g":
            if self._gg_pending:
                self._gg_pending = False
                self.action_first()
            else:
                self._gg_pending = True
        else:
            self._gg_pending = False

    # ------------------------------------------------------------------
    # navigation
    # ------------------------------------------------------------------

    def action_cursor_next(self) -> None:
        cl = self.query_one("#current", OptionList)
        cl.action_cursor_down()

    def action_cursor_prev(self) -> None:
        cl = self.query_one("#current", OptionList)
        cl.action_cursor_up()

    def _cursor_by(self, delta: int) -> None:
        cl = self.query_one("#current", OptionList)
        if not self.entries:
            return
        idx = (cl.highlighted or 0) + delta
        cl.highlighted = max(0, min(idx, len(self.entries) - 1))
        cl.scroll_to_highlight()

    def action_half_up(self) -> None:
        self._cursor_by(-max(1, self.query_one("#current").size.height // 2))

    def action_half_down(self) -> None:
        self._cursor_by(max(1, self.query_one("#current").size.height // 2))

    def action_first(self) -> None:
        cl = self.query_one("#current", OptionList)
        if self.entries:
            cl.highlighted = 0
            cl.scroll_to_highlight()

    def action_last(self) -> None:
        cl = self.query_one("#current", OptionList)
        if self.entries:
            cl.highlighted = len(self.entries) - 1
            cl.scroll_to_highlight()

    def _descend(self, entry: Entry) -> None:
        if self.client is None or entry.id is None:
            return
        self.path.append(entry)
        self._push_history()
        self.reload()  # cursor starts at the top, like yazi

    def _activate(self, entry: Entry | None) -> None:
        """Enter/l on an entry: descend, jump out of search, or open it."""
        if entry is None:
            return
        if entry.kind == "folder":
            self._descend(entry)
        elif self.search_term is not None:
            self._jump_to_entry(entry)  # search results: go to where it lives
        else:
            self._open_in_browser(entry)

    def action_enter(self) -> None:
        self._activate(self._highlighted_entry())

    def action_leave(self) -> None:
        if len(self.path) <= 1:
            self.notify("already at the NoCFO root")
            return
        leaving = self.path[-1].name
        self.path.pop()
        self._push_history()
        self.reload(cursor_name=leaving)

    def _push_history(self) -> None:
        self.history = self.history[: self.hist_i + 1]
        self.history.append(tuple(self.path))
        self.hist_i = len(self.history) - 1

    def action_back(self) -> None:
        if self.hist_i <= 0:
            return
        came_from = self.path[-1].name
        self.hist_i -= 1
        self.path = list(self.history[self.hist_i])
        self.reload(cursor_name=came_from)

    def action_forward(self) -> None:
        if self.hist_i >= len(self.history) - 1:
            return
        came_from = self.path[-1].name
        self.hist_i += 1
        self.path = list(self.history[self.hist_i])
        self.reload(cursor_name=came_from)

    def action_reload(self) -> None:
        self.invalidate()
        cursor = self._highlighted_entry()
        self.reload(cursor_key=cursor.key if cursor else None)

    # ------------------------------------------------------------------
    # marks
    # ------------------------------------------------------------------

    def action_toggle_mark(self) -> None:
        entry = self._highlighted_entry()
        if entry is None:
            return
        if entry.key in self.marks:
            del self.marks[entry.key]
        else:
            self.marks[entry.key] = entry
        self._rerender_after_marks(advance=True)

    def action_mark_all(self) -> None:
        for e in self.entries:
            self.marks[e.key] = e
        self._rerender_after_marks()

    def action_invert_marks(self) -> None:
        for e in self.entries:
            if e.key in self.marks:
                del self.marks[e.key]
            else:
                self.marks[e.key] = e
        self._rerender_after_marks()

    def action_escape(self) -> None:
        self.query_one("#current", OptionList).focus()
        if self.search_term is not None:
            self.search_term = None
            self.reload(cursor_key=self._search_prev_cursor_key)
            self.notify("search closed")
            return
        if self.marks:
            n = len(self.marks)
            self.marks.clear()
            self._rerender_after_marks()
            self.notify(f"cleared {n} marks")

    def _rerender_after_marks(self, advance: bool = False) -> None:
        self._render_current()
        self._render_parent()
        self._update_status()
        if advance:
            cl = self.query_one("#current", OptionList)
            cl.action_cursor_down()

    # ------------------------------------------------------------------
    # operations: yank/cut, paste, delete, new folder, rename
    # ------------------------------------------------------------------

    def _selected_entries(self) -> list[Entry]:
        """The marked items if any, else just the hovered one."""
        if self.marks:
            return list(self.marks.values())
        entry = self._highlighted_entry()
        return [entry] if entry else []

    @staticmethod
    def _targets(entries: list[Entry]) -> list[ops.Target]:
        return [
            ops.Target(e.kind, e.id, e.name) for e in entries if e.id is not None
        ]

    def _refocus(self) -> None:
        """Back on the interactive pane after a modal screen closes."""
        self.query_one("#current", OptionList).focus()

    def _forget_keys(self, keys: set) -> None:
        """Drop entries (marks, yank) that no longer apply."""
        for key in keys:
            self.marks.pop(key, None)
        if self.yank_items:
            self.yank_items = [e for e in self.yank_items if e.key not in keys]
            if not self.yank_items:
                self.yank_items = None
                self.yank_cut = False
        self._update_status()

    def _survivor_key(self, removed: set) -> tuple | None:
        """Where the cursor lands after ``removed`` entries disappear: the
        first kept entry at or after them, else the last one before."""
        first = next(
            (i for i, e in enumerate(self.entries) if e.key in removed), None
        )
        if first is None:
            current = self._highlighted_entry()
            return current.key if current else None
        for e in self.entries[first:]:
            if e.key not in removed:
                return e.key
        for e in reversed(self.entries[:first]):
            if e.key not in removed:
                return e.key
        return None

    def _report(self, verb: str, total: int, failures: list[str]) -> None:
        if failures:
            shown = "; ".join(failures[:3])
            if len(failures) > 3:
                shown += f" (+{len(failures) - 3} more)"
            self.notify(
                f"{verb} {total - len(failures)}/{total} — {shown}",
                severity="warning",
                timeout=8,
            )
        else:
            self.notify(f"{verb} {total} item" + ("s" if total != 1 else ""))

    def action_yank(self) -> None:
        entries = self._selected_entries()
        if not entries:
            return
        self.yank_items = entries
        self.yank_cut = False
        self._update_status()
        self.notify(f"yanked {len(entries)} — p pastes a copy")

    def action_cut(self) -> None:
        entries = self._selected_entries()
        if not entries:
            return
        self.yank_items = entries
        self.yank_cut = True
        self._update_status()
        self.notify(f"cut {len(entries)} — p moves them here")

    def action_unyank(self) -> None:
        if self.yank_items:
            self.notify(f"forgot {len(self.yank_items)} items")
        self.yank_items = None
        self.yank_cut = False
        self._update_status()

    # Textual awaits key actions inline in the message pump, so anything
    # that awaits a modal dialog or a network call must run in a worker
    # task instead — otherwise the pump deadlocks (the key that would
    # dismiss the dialog can never be processed).

    def action_paste(self) -> None:
        self._start_paste(force=False)

    def action_paste_force(self) -> None:
        self._start_paste(force=True)

    def _start_paste(self, *, force: bool) -> None:
        if self.client is None or self._op_busy:
            return
        if not self.yank_items:
            self.notify("nothing to paste — yank (y) or cut (x) first")
            return
        entries = list(self.yank_items)
        if not self._targets(entries):
            return
        self._op_busy = True
        self.run_worker(
            self._paste_flow(entries, force=force), group="op", exclusive=True
        )

    async def _paste_flow(self, entries: list[Entry], *, force: bool) -> None:
        try:
            targets = self._targets(entries)
            client = self.client
            dest_id = self.path[-1].id  # None = the folder root
            cut = self.yank_cut
            if cut:
                failures = await asyncio.to_thread(
                    ops.move_entries, client, targets, dest_id
                )
                verb = "moved"
            else:
                failures = await asyncio.to_thread(
                    ops.copy_entries, client, targets, dest_id, force=force
                )
                verb = "copied"
            keys = {e.key for e in entries}
            if cut:
                self._forget_keys(keys)
                self.invalidate()
                self.reload(cursor_key=self._survivor_key(keys))
            else:
                # copies leave this listing's keys intact, but new items appeared
                self.invalidate()
                self.reload()
                # the yank stays so it can be pasted into more folders
            self._report(verb, len(targets), failures)
            if cut and failures:
                # keep the cut so the failed ones can be retried
                self.yank_items = entries
                self.yank_cut = True
                self._update_status()
        finally:
            self._op_busy = False

    def action_delete(self) -> None:
        if self.client is None or self._op_busy:
            return
        entries = self._selected_entries()
        if not entries:
            return
        self._op_busy = True
        self.run_worker(self._delete_flow(entries), group="op", exclusive=True)

    async def _delete_flow(self, entries: list[Entry]) -> None:
        try:
            client = self.client
            if client is None:
                return
            detail = await asyncio.to_thread(self._delete_detail, entries, client)
            title = (
                f"Delete {entries[0].name!r} permanently?"
                if len(entries) == 1
                else f"Delete {len(entries)} items permanently?"
            )
            confirmed = await self._ask(ConfirmScreen(title, detail))
            self._refocus()
            if not confirmed:
                return
            targets = self._targets(entries)
            failures = await asyncio.to_thread(ops.delete_entries, client, targets)
            keys = {e.key for e in entries}
            self._forget_keys(keys)
            self.invalidate()
            self.reload(cursor_key=self._survivor_key(keys))
            if isinstance(failures, tuple):
                failures_list, deleted_total = failures[0], failures[1]
            else:
                failures_list, deleted_total = failures, len(targets)
            self._report("deleted", deleted_total, failures_list)
        finally:
            self._op_busy = False

    def _delete_detail(self, entries: list[Entry], client: Any) -> str:
        """The confirm dialog body: one line per target, warnings inline
        (runs in a thread — it may sweep the attached-file index)."""
        if self.attached_ids is None and not self._attached_failed:
            try:
                self.attached_ids = client.attached_file_ids()
            except NoCfoError:
                self._attached_failed = True
        lines: list[str] = []
        for e in entries[:15]:
            label = e.name + ("/" if e.kind == "folder" else "")
            suffix = ""
            if e.kind == "folder":
                try:
                    if client.get_folder(e.id).has_content:
                        suffix = "  (recursive — deletes everything inside)"
                except NoCfoError as exc:
                    suffix = f"  ({exc})"
            else:
                file = e.file
                if file is not None and getattr(file, "is_deletable", None) is False:
                    suffix = "  (locked — may fail)"
                elif self.attached_ids is not None and e.id in self.attached_ids:
                    suffix = "  (attached to a document)"
            lines.append(f"  {label}{suffix}")
        if len(entries) > 15:
            lines.append(f"  … and {len(entries) - 15} more")
        return "\n".join(lines)

    def action_new_folder(self) -> None:
        if self.client is None or self._op_busy:
            return
        self._op_busy = True
        self.run_worker(self._new_folder_flow(), group="op", exclusive=True)

    async def _new_folder_flow(self) -> None:
        try:
            name = await self._ask(
                InputScreen(
                    "New folder",
                    placeholder="folder name",
                    hint=f"created inside {self.path[-1].name}/",
                )
            )
            self._refocus()
            if name is None:
                return
            client = self.client
            dest_id = self.path[-1].id
            try:
                new_id = await asyncio.to_thread(
                    ops.create_folder, client, name, dest_id
                )
            except ops.OpsError as e:
                self.notify(str(e), severity="error", timeout=8)
                return
            self.invalidate()
            self.reload(cursor_key=("folder", new_id))
            self.notify(f"created {name.strip()!r}")
        finally:
            self._op_busy = False

    def action_rename(self) -> None:
        if self.client is None or self._op_busy:
            return
        entry = self._highlighted_entry()
        if entry is None:
            return
        self._op_busy = True
        self.run_worker(self._rename_flow(entry), group="op", exclusive=True)

    async def _rename_flow(self, entry: Entry) -> None:
        try:
            new_name = await self._ask(
                InputScreen(
                    f"Rename {entry.name}",
                    value=entry.name,
                    cursor_position=rename_cursor_position(entry.name),
                )
            )
            self._refocus()
            if new_name is None or new_name.strip() == entry.name:
                return
            client = self.client
            target = ops.Target(entry.kind, entry.id, entry.name)
            parent_id = self.path[-1].id
            try:
                await asyncio.to_thread(
                    ops.rename_entry, client, target, new_name, parent_id=parent_id
                )
            except ops.OpsError as e:
                self.notify(str(e), severity="error", timeout=8)
                return
            self.invalidate()
            self.reload(cursor_key=entry.key)
            self.notify(f"renamed to {new_name.strip()!r}")
        finally:
            self._op_busy = False

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def action_search(self) -> None:
        if self.client is None or self._op_busy:
            return
        self._op_busy = True
        self.run_worker(self._search_flow(), group="op", exclusive=True)

    async def _search_flow(self) -> None:
        """Ask for a term, then fetch the whole-business file matches."""
        try:
            term = await self._ask(
                InputScreen(
                    "Search files",
                    placeholder="name contains…",
                    hint="enter searches every folder; esc cancels",
                )
            )
            self._refocus()
            if term is None:
                return
            term = term.strip()
            if not term:
                self.notify("empty search — nothing to do")
                return
            client = self.client
            if client is None:
                return
            files = await asyncio.to_thread(client.search_files, term)
            entries = build_entries([], list(files))
            self._apply_search(term, entries)
        except Exception as e:  # NoCfoError or anything unexpected
            self.notify(f"search failed: {e}", severity="error", timeout=8)
        finally:
            self._op_busy = False

    def _apply_search(self, term: str, entries: list[Entry]) -> None:
        prev = self._highlighted_entry()
        self._search_prev_cursor_key = prev.key if prev else None
        self.search_term = term
        self.entries = entries
        cl = self.query_one("#current", OptionList)
        cl.clear_options()
        if entries:
            cl.add_options([self._option(e) for e in entries])
        cl.highlighted = 0 if entries else None
        cl.scroll_home(animate=False)
        self._render_parent()
        self._update_path()
        self._update_status()
        noun = "match" if len(entries) == 1 else "matches"
        self.notify(f"{len(entries)} {noun} for {term!r}")

    def _jump_to_entry(self, entry: Entry) -> None:
        """Leave search mode and open the folder that holds this file."""
        if self.client is None or self._op_busy:
            return
        self._op_busy = True
        self.run_worker(self._jump_flow(entry), group="op", exclusive=True)

    async def _jump_flow(self, entry: Entry) -> None:
        try:
            folder_id = entry.file.folder_id if entry.file is not None else None
            if folder_id is None:
                new_path = [ROOT]  # lives at the business root
            else:
                detail = await asyncio.to_thread(self._folder_detail, folder_id)
                crumbs = [
                    c
                    for c in (detail.breadcrumbs or [])
                    if isinstance(c, dict) and c.get("id") is not None
                ]
                new_path = [
                    Entry("folder", int(c["id"]), str(c.get("name", c["id"])))
                    for c in crumbs
                ]
            # breadcrumbs only list real folders: the pseudo-root (id None)
            # that every normal path starts with is missing — put it back.
            if not new_path or new_path[0].id is not None:
                new_path = [ROOT, *new_path]
            self.path = new_path
            self.search_term = None
            self._push_history()
            self.reload(cursor_key=entry.key)
            self.notify(f"jumped to {entry.name}")
        except Exception as e:
            self.notify(f"could not locate {entry.name}: {e}", severity="warning")
        finally:
            self._op_busy = False

    # ------------------------------------------------------------------
    # preview seek / open / quit
    # ------------------------------------------------------------------

    def action_seek_down(self) -> None:
        self.query_one("#preview-scroll", VerticalScroll).scroll_relative(
            y=5, animate=False
        )

    def action_seek_up(self) -> None:
        self.query_one("#preview-scroll", VerticalScroll).scroll_relative(
            y=-5, animate=False
        )

    def _open_in_browser(self, entry: Entry | None = None) -> None:
        entry = entry or self._highlighted_entry()
        if entry is None or entry.kind != "file":
            return
        url = entry.file.url if entry.file else ""
        if not url:
            self.notify("this file has no download URL yet", severity="warning")
            return
        webbrowser.open(url)
        self.notify(f"opening {entry.name} in the browser")

    def action_open(self) -> None:
        self._open_in_browser()

    def action_quit(self) -> None:
        if len(self.screen_stack) > 1:
            return  # a modal is open: its own keys decide
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.exit()


def _parse_option_id(option_id: str | None) -> tuple[str, int] | None:
    if not option_id or ":" not in option_id:
        return None
    kind, _sep, rest = option_id.partition(":")
    try:
        return (kind, int(rest))
    except ValueError:
        return None
