"""ExplorerApp behaviour: navigation, preview, marks, wizard, errors.

Everything runs against :class:`tests.conftest.FakeClient` — no network.
Timing note: listings and previews load in worker threads behind a
debounce, so tests ``pilot.pause()`` past those (PREVIEW_DEBOUNCE=0.18s).
"""

from __future__ import annotations

from pathlib import Path

import tomllib

from nocfo_explorer.app import (
    ExplorerApp,
    build_entries,
    is_text_preview,
    rename_cursor_position,
    type_tag,
    _parse_option_id,
)
from nocfo_explorer.config import Config
from nocfo_explorer.nocfo import NoCfoError, NocfoFile, NocfoFolder
from nocfo_explorer.screens import (
    AuthScreen,
    BusinessPickerScreen,
    ConfirmScreen,
    InputScreen,
    TokenScreen,
)
from tests.conftest import FakeClient

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def seed_tree(fake: FakeClient) -> dict[str, int]:
    """Root: folders Alpha/Zeta, files note.txt/report.pdf; Alpha/Sub/deep…"""
    ids: dict[str, int] = {}
    ids["alpha"] = fake.add_folder("Alpha")
    ids["zeta"] = fake.add_folder("Zeta")
    ids["sub"] = fake.add_folder("Sub", parent=ids["alpha"])
    ids["note"] = fake.add_file("note.txt", content=b"top secret\nsecond line\n")
    ids["alpha_notes"] = fake.add_file(
        "alpha-notes.md", folder=ids["alpha"], content=b"# Alpha\n"
    )
    ids["deep"] = fake.add_file("deep.csv", folder=ids["sub"], content=b"a,b\n1,2\n")
    ids["report"] = fake.add_file(
        "report.pdf", mime="application/pdf", content=b"%PDF-1.4 fake"
    )
    return ids


def make_app(cfg: Config, fake: FakeClient) -> ExplorerApp:
    return ExplorerApp(cfg, client_factory=lambda **kw: fake)


def names(app: ExplorerApp) -> list[str]:
    return [e.name for e in app.entries]


def prompts(app: ExplorerApp, pane: str = "#current") -> list[str]:
    return [str(o.prompt) for o in app.query_one(pane).options]


def highlighted(app: ExplorerApp, pane: str = "#current") -> int | None:
    return app.query_one(pane).highlighted


def path_text(app: ExplorerApp) -> str:
    return str(app.query_one("#path").content)


def status_text(app: ExplorerApp) -> str:
    return str(app.query_one("#status").content)


def preview_head(app: ExplorerApp) -> str:
    return str(app.query_one("#preview-head").content)


def preview_body(app: ExplorerApp) -> str:
    return str(app.query_one("#preview-body").content)


async def settle(pilot, t: float = 0.25) -> None:
    """Wait out a listing load (worker thread + call_from_thread)."""
    await pilot.pause(t)


async def settle_preview(pilot) -> None:
    """Wait out the hover debounce (0.18s) plus the preview worker."""
    await pilot.pause(0.5)


async def settle_op(pilot) -> None:
    """Wait out a mutation (thread) plus its follow-up listing load."""
    await pilot.pause(0.6)


def file_id(fake: FakeClient, name: str) -> int:
    return next(f["id"] for f in fake.files.values() if f["name"] == name)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_rename_cursor_position_goes_before_last_extension():
    assert rename_cursor_position("report.pdf") == 6
    assert rename_cursor_position("archive.tar.gz") == 11
    assert rename_cursor_position("README") == 6
    assert rename_cursor_position(".gitignore") == len(".gitignore")


def test_is_text_preview():
    assert is_text_preview("a.md", "")
    assert is_text_preview("a.weird", "text/csv")
    assert is_text_preview("a.weird", "application/vnd.x+json")
    assert is_text_preview("a.weird", "application/ld+xml")
    assert not is_text_preview("a.pdf", "application/pdf")
    assert not is_text_preview("a.bin", "application/octet-stream")


def test_type_tag_prefers_extension_then_mime():
    assert type_tag("report.pdf", "application/pdf") == "PDF"
    assert type_tag("photo.heic", "") == "HEIC"
    assert type_tag("archive.superlongext", "application/zip") == "ZIP"
    assert type_tag("data", "application/json") == "JSON"
    assert type_tag("data", "") == ""


def test_build_entries_folders_first_then_files_each_sorted():
    folders = [
        NocfoFolder(id=2, name="zeta", parent=None),
        NocfoFolder(id=1, name="Alpha", parent=None),
    ]
    files = [
        NocfoFile(id=9, name="b.txt", mime="text/plain", folder_id=None),
        NocfoFile(id=3, name="a.pdf", mime="application/pdf", folder_id=None),
    ]
    assert [(e.kind, e.name) for e in build_entries(folders, files)] == [
        ("folder", "Alpha"),
        ("folder", "zeta"),
        ("file", "a.pdf"),
        ("file", "b.txt"),
    ]


def test_parse_option_id():
    assert _parse_option_id("folder:12") == ("folder", 12)
    assert _parse_option_id("file:3") == ("file", 3)
    assert _parse_option_id("bogus") is None
    assert _parse_option_id("file:x") is None
    assert _parse_option_id("folder:") is None
    assert _parse_option_id(None) is None


# ---------------------------------------------------------------------------
# listing / rendering
# ---------------------------------------------------------------------------


async def test_root_listing_order_tags_and_headers(cfg, fake: FakeClient):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        assert names(app) == ["Alpha", "Zeta", "note.txt", "report.pdf"]
        rows = prompts(app)
        assert rows[0].startswith("  ") and "Alpha/" in rows[0]
        assert "TXT" in rows[2]
        assert "PDF" in rows[3]
        assert path_text(app) == "nocfo-explorer · acme · /"
        assert "space mark" in status_text(app)


async def test_load_error_is_shown_in_status(cfg):
    class BrokenListing(FakeClient):
        def list_files(self, *args, **kwargs):
            raise NoCfoError("500 backend exploded", 500)

    fake = BrokenListing()
    fake.add_folder("Alpha")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        assert app.entries == []
        assert "500 backend exploded" in status_text(app)
        await pilot.press("q")
    assert not app.is_running


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------


async def test_enter_folder_updates_panes_and_leave_restores_cursor(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j")  # Zeta (empty)
        assert highlighted(app) == 1
        await pilot.press("l")
        await settle(pilot)
        assert [seg.name for seg in app.path] == ["root", "Zeta"]
        assert path_text(app).endswith("/Zeta")
        assert app.query_one("#current").option_count == 0
        # parent pane lists root with Zeta highlighted
        assert "Zeta" in prompts(app, "#parent")[highlighted(app, "#parent")]
        await pilot.press("h")
        await settle(pilot)
        assert path_text(app).endswith("· /")
        assert highlighted(app) == 1  # back on Zeta


async def test_left_right_arrows_navigate(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("right")  # into Alpha (cursor 0)
        await settle(pilot)
        assert [seg.name for seg in app.path] == ["root", "Alpha"]
        assert names(app) == ["Sub", "alpha-notes.md"]
        await pilot.press("left")  # back to root
        await settle(pilot)
        assert [seg.name for seg in app.path] == ["root"]
        assert highlighted(app) == 0  # cursor restored on Alpha


async def test_history_back_and_forward(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("l")  # Alpha
        await settle(pilot)
        await pilot.press("l")  # Sub
        await settle(pilot)
        assert [seg.name for seg in app.path] == ["root", "Alpha", "Sub"]
        assert names(app) == ["deep.csv"]
        await pilot.press("H")
        await settle(pilot)
        assert [seg.name for seg in app.path] == ["root", "Alpha"]
        assert highlighted(app) == 0  # cursor back on Sub
        await pilot.press("L")
        await settle(pilot)
        assert [seg.name for seg in app.path] == ["root", "Alpha", "Sub"]


async def test_leave_at_root_stays_put(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("h")
        await settle(pilot)
        assert [seg.name for seg in app.path] == ["root"]


async def test_cursor_keys_and_top_bottom(cfg, fake):
    seed_tree(fake)  # 4 entries at root
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j")
        assert highlighted(app) == 2
        await pilot.press("k")
        assert highlighted(app) == 1
        await pilot.press("G")
        assert highlighted(app) == 3
        await pilot.press("j")  # clamped at the bottom — no wrap
        assert highlighted(app) == 3
        await pilot.press("g", "g")
        assert highlighted(app) == 0
        await pilot.press("k")  # clamped at the top — no wrap
        assert highlighted(app) == 0


async def test_reload_reflects_external_changes(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        fake.add_folder("Newborn")
        await pilot.press("R")
        await settle(pilot)
        assert "Newborn" in names(app)


async def test_listing_cache_prevents_duplicate_fetches(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await settle_preview(pilot)  # hovering Alpha prefetched its listing
        await pilot.press("l")  # into Alpha — cursor hovers Sub, prefetching it
        await settle(pilot)
        await settle_preview(pilot)
        fetches = fake.count("list_files")
        details = fake.count("get_folder")
        await pilot.press("h")  # back to root — everything cached
        await settle(pilot)
        await settle_preview(pilot)
        assert fake.count("list_files") == fetches
        assert fake.count("get_folder") == details


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


async def test_preview_text_file_details_and_content(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j")  # note.txt
        await settle_preview(pilot)
        head = preview_head(app)
        assert "note.txt" in head
        assert str(ids["note"]) in head and "text/plain" in head
        assert "attached" not in head
        body = preview_body(app)
        assert "top secret" in body and "second line" in body


async def test_preview_long_text_is_truncated_at_500_lines(cfg, fake):
    fake.add_file("big.txt", content="\n".join(f"line {i}" for i in range(1000)).encode())
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await settle_preview(pilot)  # the only entry is hovered at load
        body = preview_body(app)
        assert body.endswith("total)")
        assert len(body.splitlines()) == 501


async def test_preview_binary_file_says_no_preview(cfg, fake):
    fake.add_file("report.pdf", mime="application/pdf", content=b"%PDF")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await settle_preview(pilot)
        assert "(no preview for application/pdf)" in preview_body(app)


async def test_preview_folder_shows_children_counts_and_path(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await settle_preview(pilot)  # cursor starts on Alpha
        head = preview_head(app)
        assert "Alpha/" in head
        assert "/Alpha" in head  # breadcrumb path
        assert "1 folders, 1 files" in head
        body = preview_body(app)
        assert "Sub/" in body and "alpha-notes.md" in body


async def test_preview_failure_is_contained(cfg):
    class BrokenDetail(FakeClient):
        def get_file(self, file_id):
            raise NoCfoError("404 file gone", 404)

    fake = BrokenDetail()
    fake.add_file("note.txt", content=b"x")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await settle_preview(pilot)
        assert "preview failed" in preview_body(app)
        await pilot.press("q")  # the app is still alive
    assert not app.is_running


async def test_empty_folder_shows_self_preview(cfg, fake):
    fake.add_folder("Empty")
    fake.add_file("note.txt", content=b"x")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("l")  # into Empty
        await settle(pilot)
        await settle_preview(pilot)
        assert app.query_one("#current").option_count == 0
        assert preview_body(app) == "(empty folder)"


async def test_preview_badges_attached_and_locked(cfg, fake):
    att = fake.add_file("attached.txt", content=b"a")
    fake.attached.add(att)
    lock = fake.add_file("locked.txt", content=b"b")
    fake.locked.add(lock)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await settle_preview(pilot)  # attached.txt hovered at load
        assert "attached to a document" in preview_head(app)
        await pilot.press("j")
        await settle_preview(pilot)
        assert "not deletable (locked)" in preview_head(app)


async def test_preview_debounced_to_the_final_hover(cfg, fake):
    fake.add_folder("Alpha")
    fake.add_file("one.txt", content=b"ONE\n")
    fake.add_file("two.txt", content=b"TWO\n")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "j")  # quickly pass one.txt to two.txt
        await settle_preview(pilot)
        assert fake.count("get_file") == 1  # only the final hover fetched
        assert "TWO" in preview_body(app)


async def test_j_and_k_seek_the_preview(cfg, fake):
    fake.add_file("big.txt", content="\n".join(f"line {i}" for i in range(600)).encode())
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await settle_preview(pilot)
        scroll = app.query_one("#preview-scroll")
        assert scroll.scroll_y == 0
        await pilot.press("J", "J")
        assert scroll.scroll_y > 0
        await pilot.press("K", "K")
        assert scroll.scroll_y == 0


# ---------------------------------------------------------------------------
# open in browser
# ---------------------------------------------------------------------------


async def test_enter_on_file_opens_browser(cfg, fake, monkeypatch):
    ids = seed_tree(fake)
    opened: list[str] = []
    monkeypatch.setattr(
        "nocfo_explorer.app.webbrowser.open", lambda url: opened.append(url)
    )
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("G")  # report.pdf
        await pilot.press("enter")
        assert opened == [f"https://api.test/media/{ids['report']}"]


async def test_o_opens_hovered_file_in_browser(cfg, fake, monkeypatch):
    ids = seed_tree(fake)
    opened: list[str] = []
    monkeypatch.setattr(
        "nocfo_explorer.app.webbrowser.open", lambda url: opened.append(url)
    )
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "o")  # note.txt
        assert opened == [f"https://api.test/media/{ids['note']}"]


# ---------------------------------------------------------------------------
# marks
# ---------------------------------------------------------------------------


async def test_space_marks_advances_and_escape_clears(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("space")
        assert len(app.marks) == 1
        assert highlighted(app) == 1  # advanced
        assert "1 marked" in status_text(app)
        assert prompts(app)[0].startswith("•")
        await pilot.press("space")  # toggle-marks Zeta too, cursor moves on
        assert len(app.marks) == 2
        assert highlighted(app) == 2
        await pilot.press("escape")
        assert app.marks == {}
        assert "marked" not in status_text(app)
        assert all(p.startswith("  ") for p in prompts(app))


async def test_ctrl_a_marks_everything_and_escape_clears(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+a")
        assert len(app.marks) == 4
        assert "4 marked" in status_text(app)
        await pilot.press("escape")
        assert app.marks == {}


async def test_ctrl_r_inverts_marks(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("space")  # mark Alpha
        await pilot.press("ctrl+r")
        assert ("folder", ids["alpha"]) not in app.marks
        assert ("folder", ids["zeta"]) in app.marks
        assert ("file", ids["note"]) in app.marks
        assert ("file", ids["report"]) in app.marks
        assert len(app.marks) == 3


# ---------------------------------------------------------------------------
# first-run wizard
# ---------------------------------------------------------------------------


async def test_wizard_full_flow_saves_config_and_loads_root(fake: FakeClient):
    cfg = Config(base_url="https://api.test")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, TokenScreen)
        await pilot.press(*list("sekrit123"), "enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, BusinessPickerScreen)
        await pilot.press("enter")  # first option: acme
        await settle(pilot)
        assert cfg.token == "sekrit123"
        assert cfg.business_slug == "acme"
        saved = Path.home() / ".config" / "nocfo-explorer" / "config.toml"
        data = tomllib.loads(saved.read_text())
        assert data["nocfo"]["token"] == "sekrit123"
        assert data["nocfo"]["business_slug"] == "acme"
        assert path_text(app) == "nocfo-explorer · acme · /"


async def test_wizard_single_business_is_chosen_without_a_picker(fake: FakeClient):
    fake.businesses = [{"slug": "solo", "name": "Solo"}]
    cfg = Config(base_url="https://api.test")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press(*list("tok"), "enter")
        await settle(pilot)
        assert not isinstance(app.screen, BusinessPickerScreen)
        assert cfg.business_slug == "solo"
        assert fake.business_slug == "solo"  # the client was pointed at it
        assert path_text(app).endswith("· /")


async def test_wizard_business_step_only_when_token_present(fake: FakeClient):
    fake.businesses = [{"slug": "one", "name": "One"}]
    cfg = Config(base_url="https://api.test", token="tok")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        assert not isinstance(app.screen, TokenScreen)
        assert cfg.business_slug == "one"
        assert path_text(app).endswith("· /")


async def test_wizard_rejects_bad_token_and_can_quit(fake: FakeClient):
    fake.fail_token = True
    cfg = Config(base_url="https://api.test")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press(*list("bad"), "enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, AuthScreen)
        assert cfg.token == ""  # the bad token was not kept
        await pilot.press("q")
        await pilot.pause(0.2)
        assert not app.is_running


async def test_wizard_retry_after_bad_token_succeeds(fake: FakeClient):
    fake.fail_token = True
    fake.businesses = [{"slug": "solo", "name": "Solo"}]
    cfg = Config(base_url="https://api.test")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press(*list("bad"), "enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, AuthScreen)
        fake.fail_token = False
        await pilot.press("r")  # retry -> token screen again
        await pilot.pause(0.3)
        assert isinstance(app.screen, TokenScreen)
        await pilot.press(*list("good"), "enter")
        await settle(pilot)
        assert cfg.token == "good"
        assert cfg.business_slug == "solo"


async def test_wizard_cancel_exits_the_app(fake: FakeClient):
    cfg = Config(base_url="https://api.test")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, TokenScreen)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not app.is_running


async def test_wizard_refuses_an_empty_token(fake: FakeClient):
    cfg = Config(base_url="https://api.test")
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("enter")  # empty input
        await pilot.pause(0.2)
        assert isinstance(app.screen, TokenScreen)  # still asking
        await pilot.press("escape")


# ---------------------------------------------------------------------------
# quit
# ---------------------------------------------------------------------------


async def test_q_quits(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("q")
        assert not app.is_running

# ---------------------------------------------------------------------------
# operations: yank/cut + paste
# ---------------------------------------------------------------------------


async def test_yank_cut_and_unyank_show_in_status(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("y")
        assert "yank: 1 items" in status_text(app)
        await pilot.press("Y")
        assert "items" not in status_text(app)
        await pilot.press("x")
        assert "cut: 1 items" in status_text(app)
        await pilot.press("X")
        assert "items" not in status_text(app)


async def test_cut_and_paste_moves_the_file(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "x")  # cut note.txt
        assert "cut: 1 items" in status_text(app)
        await pilot.press("k")  # back up to Zeta
        await pilot.press("l")  # into the empty folder
        await settle(pilot)
        await pilot.press("p")
        await settle_op(pilot)
        assert fake.files[ids["note"]]["folder_id"] == ids["zeta"]
        assert names(app) == ["note.txt"]
        assert "items" not in status_text(app)  # cut consumed
        await pilot.press("h")
        await settle(pilot)
        assert "note.txt" not in names(app)
        assert names(app) == ["Alpha", "Zeta", "report.pdf"]


async def test_yank_and_paste_copies_the_file(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "y")  # yank note.txt
        await pilot.press("k", "l")  # into Zeta
        await settle(pilot)
        await pilot.press("p")
        await settle_op(pilot)
        copies = [f for f in fake.files.values() if f["folder_id"] == ids["zeta"]]
        assert len(copies) == 1
        assert fake.contents[copies[0]["id"]] == b"top secret\nsecond line\n"
        assert fake.files[ids["note"]]["folder_id"] is None  # source intact
        assert "yank: 1 items" in status_text(app)  # yank survives a copy


async def test_paste_collision_suffix_then_force_overwrite(cfg, fake):
    ids = seed_tree(fake)
    victim = fake.add_file("note.txt", folder=ids["zeta"], content=b"OLD DEST\n")
    src_bytes = b"top secret\nsecond line\n"
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "y")  # yank the root copy
        await pilot.press("k", "l")  # into Zeta (which already has note.txt)
        await settle(pilot)
        await pilot.press("p")  # plain paste steps aside
        await settle_op(pilot)
        copy = next(
            f for f in fake.files.values() if f["name"] == "note (copy).txt"
        )
        assert copy["folder_id"] == ids["zeta"]
        assert fake.contents[copy["id"]] == src_bytes
        # force paste replaces the destination's own note.txt
        await pilot.press("P")
        await settle_op(pilot)
        assert victim not in fake.files
        replacements = [
            f
            for f in fake.files.values()
            if f["name"] == "note.txt" and f["folder_id"] == ids["zeta"]
        ]
        assert len(replacements) == 1
        assert fake.contents[replacements[0]["id"]] == src_bytes
        assert "yank: 1 items" in status_text(app)


async def test_paste_into_the_cut_folder_itself_is_refused(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("x")  # cut Alpha (cursor starts on it)
        await pilot.press("l")  # enter Alpha
        await settle(pilot)
        await pilot.press("p")
        await settle_op(pilot)
        assert fake.folders[ids["alpha"]]["parent"] is None  # unchanged
        assert names(app) == ["Sub", "alpha-notes.md"]
        assert "cut: 1 items" in status_text(app)  # kept for a retry


async def test_paste_with_nothing_yanked_is_harmless(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        before = set(fake.files)
        await pilot.press("p")
        await settle_op(pilot)
        assert set(fake.files) == before
        assert names(app) == ["Alpha", "Zeta", "note.txt", "report.pdf"]
        assert "items" not in status_text(app)


# ---------------------------------------------------------------------------
# operations: delete
# ---------------------------------------------------------------------------


async def test_delete_dialog_cancel_changes_nothing(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "d")  # note.txt
        await settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert "note.txt" in app.screen.detail
        assert "permanently" in app.screen.title_text
        await pilot.press("n")
        await settle(pilot)
        assert not isinstance(app.screen, ConfirmScreen)
        assert ids["note"] in fake.files
        assert names(app)[2] == "note.txt"
        assert highlighted(app) == 2
        assert app.focused.id == "current"  # refocused after the modal


async def test_delete_confirm_removes_file_and_keeps_cursor_placed(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "d")
        await settle(pilot)
        await pilot.press("y")
        await settle_op(pilot)
        assert ids["note"] not in fake.files
        assert names(app) == ["Alpha", "Zeta", "report.pdf"]
        assert names(app)[highlighted(app)] == "report.pdf"  # next survivor
        assert app.focused.id == "current"


async def test_delete_marks_skips_nonempty_folders(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+a")  # all four root entries
        await pilot.press("d")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.title_text == "Delete 4 items permanently?"
        assert "Alpha/" in app.screen.detail
        assert "(not empty — skipped)" in app.screen.detail
        await pilot.press("y")
        await settle_op(pilot)
        # Alpha survives (it has Sub), the rest of the selection is gone
        assert ids["alpha"] in fake.folders
        assert ids["sub"] in fake.folders
        assert ids["zeta"] not in fake.folders
        assert ids["note"] not in fake.files
        assert ids["report"] not in fake.files
        assert names(app) == ["Alpha"]
        assert app.marks == {}  # marks consumed by the delete


async def test_modal_screen_blocks_app_level_keys(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("d")  # confirm on Alpha (non-empty -> warning line)
        await settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        stack = len(app.screen_stack)
        await pilot.press("d", "a", "r", "q")  # none of these may reach the app
        await settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)  # still open
        assert len(app.screen_stack) == stack
        assert app.is_running
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, ConfirmScreen)


# ---------------------------------------------------------------------------
# operations: new folder + rename
# ---------------------------------------------------------------------------


async def test_new_folder_is_created_and_selected(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("a")
        await settle(pilot)
        assert isinstance(app.screen, InputScreen)
        await pilot.press(*list("Invoices"), "enter")
        await settle_op(pilot)
        created = [f for f in fake.folders.values() if f["name"] == "Invoices"]
        assert len(created) == 1
        assert created[0]["parent"] is None
        assert names(app) == ["Alpha", "Invoices", "Zeta", "note.txt", "report.pdf"]
        assert names(app)[highlighted(app)] == "Invoices"
        assert app.focused.id == "current"


async def test_new_folder_duplicate_name_reports_an_error(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("a")
        await settle(pilot)
        await pilot.press(*list("Alpha"), "enter")
        await settle_op(pilot)
        assert len([f for f in fake.folders.values() if f["name"] == "Alpha"]) == 1
        assert names(app).count("Alpha") == 1
        assert app.focused.id == "current"


async def test_new_folder_cancel_creates_nothing(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("a")
        await settle(pilot)
        await pilot.press(*list("Ghost"), "escape")
        await settle(pilot)
        assert not any(f["name"] == "Ghost" for f in fake.folders.values())
        assert names(app) == ["Alpha", "Zeta", "note.txt", "report.pdf"]


async def test_rename_opens_with_cursor_before_the_extension(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "r")  # note.txt
        await settle(pilot)
        assert isinstance(app.screen, InputScreen)
        field = app.screen.query_one("Input")
        assert field.value == "note.txt"
        assert field.cursor_position == 4  # between "note" and ".txt"
        await pilot.press("s")  # -> "notes.txt"
        await pilot.press("enter")
        await settle_op(pilot)
        assert fake.files[ids["note"]]["name"] == "notes.txt"
        assert "notes.txt" in names(app)
        assert "note.txt" not in names(app)
        assert names(app)[highlighted(app)] == "notes.txt"
        assert app.focused.id == "current"


async def test_rename_to_an_existing_name_reports_an_error(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "r")  # Zeta -> try to rename it to Alpha
        await settle(pilot)
        field = app.screen.query_one("Input")
        assert field.value == "Zeta"
        for _ in range(4):
            await pilot.press("backspace")
        await pilot.press(*list("Alpha"), "enter")
        await settle_op(pilot)
        assert fake.folders[ids["zeta"]]["name"] == "Zeta"
        assert names(app).count("Alpha") == 1
        assert "Zeta" in names(app)


async def test_rename_with_an_unchanged_name_makes_no_api_call(cfg, fake):
    seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "r")
        await settle(pilot)
        await pilot.press("enter")  # submit the original value
        await settle(pilot)
        assert fake.count("update_file") == 0
        assert fake.count("update_folder") == 0


async def test_rename_to_an_empty_name_reports_an_error(cfg, fake):
    ids = seed_tree(fake)
    app = make_app(cfg, fake)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("j", "j", "r")
        await settle(pilot)
        await pilot.press("end")  # cursor sits mid-name; go to the end first
        for _ in range(8):
            await pilot.press("backspace")
        await pilot.press("enter")
        await settle_op(pilot)
        assert fake.files[ids["note"]]["name"] == "note.txt"  # unchanged
        assert fake.count("update_file") == 0
