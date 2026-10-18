"""Operations layer: guards, collision naming, partial failures.

Runs against :class:`tests.conftest.FakeClient` — no network, no Textual.
"""

from __future__ import annotations

import pytest

from nocfo_explorer import ops
from nocfo_explorer.ops import (
    OpsError,
    Target,
    copy_entries,
    copy_name,
    create_folder,
    delete_entries,
    listing_names,
    move_entries,
    rename_entry,
)
from nocfo_explorer.nocfo import NoCfoError
from tests.conftest import FakeClient


def test_copy_name_collisions():
    assert copy_name("report.pdf", set()) == "report.pdf"
    assert copy_name("report.pdf", {"report.pdf"}) == "report (copy).pdf"
    assert (
        copy_name("report.pdf", {"report.pdf", "report (copy).pdf"})
        == "report (copy 2).pdf"
    )
    assert copy_name("README", {"README"}) == "README (copy)"
    assert copy_name("archive.tar.gz", {"archive.tar.gz"}) == (
        "archive.tar (copy).gz"
    )


def test_listing_names_covers_folders_and_files():
    fake = FakeClient()
    docs = fake.add_folder("Docs")
    fake.add_file("top.txt")
    fake.add_file("in.txt", folder=docs)
    assert listing_names(fake, None) == {"Docs", "top.txt"}
    assert listing_names(fake, docs) == {"in.txt"}


# ---------------------------------------------------------------------------
# create / rename
# ---------------------------------------------------------------------------


def test_create_folder_success_and_guards():
    fake = FakeClient()
    fid = create_folder(fake, "Invoices", None)
    assert fake.folders[fid]["name"] == "Invoices"
    assert fake.folders[fid]["parent"] is None

    with pytest.raises(OpsError, match="already exists"):
        create_folder(fake, "Invoices", None)
    with pytest.raises(OpsError, match="empty"):
        create_folder(fake, "   ", None)
    with pytest.raises(OpsError, match="slashes"):
        create_folder(fake, "a/b", None)
    assert fake.count("create_folder") == 1


def test_rename_file_and_folder():
    fake = FakeClient()
    alpha = fake.add_folder("Alpha")
    note = fake.add_file("note.txt", folder=alpha)

    out = rename_entry(
        fake, Target("file", note, "note.txt"), "notes.txt", parent_id=alpha
    )
    assert out == "notes.txt"
    assert fake.files[note]["name"] == "notes.txt"

    out = rename_entry(
        fake, Target("folder", alpha, "Alpha"), "Beta", parent_id=None
    )
    assert out == "Beta"
    assert fake.folders[alpha]["name"] == "Beta"


def test_rename_same_name_is_a_no_op():
    fake = FakeClient()
    note = fake.add_file("note.txt")
    rename_entry(fake, Target("file", note, "note.txt"), "note.txt", parent_id=None)
    assert fake.count("update_file") == 0


def test_rename_refuses_empty_slash_and_duplicates():
    fake = FakeClient()
    a = fake.add_folder("Alpha")
    z = fake.add_folder("Zeta")
    with pytest.raises(OpsError, match="empty"):
        rename_entry(fake, Target("folder", z, "Zeta"), "  ", parent_id=None)
    with pytest.raises(OpsError, match="slashes"):
        rename_entry(fake, Target("folder", z, "Zeta"), "x/y", parent_id=None)
    with pytest.raises(OpsError, match="already exists"):
        rename_entry(fake, Target("folder", z, "Zeta"), "Alpha", parent_id=None)
    assert fake.folders[z]["name"] == "Zeta"
    assert fake.folders[a]["name"] == "Alpha"
    assert fake.count("update_folder") == 0


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


def test_move_file_into_folder_and_folder_to_root():
    fake = FakeClient()
    dest = fake.add_folder("Dest")
    moving = fake.add_folder("Moving")
    note = fake.add_file("note.txt")

    assert move_entries(fake, [Target("file", note, "note.txt")], dest) == []
    assert fake.files[note]["folder_id"] == dest

    assert move_entries(fake, [Target("folder", moving, "Moving")], None) == []
    assert fake.folders[moving]["parent"] is None


def test_move_refuses_folder_into_its_own_descendant():
    fake = FakeClient()
    alpha = fake.add_folder("Alpha")
    sub = fake.add_folder("Sub", parent=alpha)

    failures = move_entries(fake, [Target("folder", alpha, "Alpha")], sub)
    assert len(failures) == 1
    assert "into itself" in failures[0]
    assert fake.folders[alpha]["parent"] is None
    assert fake.count("update_folder") == 0


def test_move_refuses_folder_into_itself():
    fake = FakeClient()
    alpha = fake.add_folder("Alpha")
    failures = move_entries(fake, [Target("folder", alpha, "Alpha")], alpha)
    assert failures and "into itself" in failures[0]
    assert fake.count("update_folder") == 0


def test_move_collects_per_item_failures_and_keeps_going():
    note = None

    class HalfBroken(FakeClient):
        def update_file(self, file_id, **kwargs):
            if file_id == note:
                raise NoCfoError("403 not allowed", 403)
            return super().update_file(file_id, **kwargs)

    fake = HalfBroken()
    dest = fake.add_folder("Dest")
    note = fake.add_file("note.txt")
    other = fake.add_file("other.txt")

    failures = move_entries(
        fake,
        [Target("file", note, "note.txt"), Target("file", other, "other.txt")],
        dest,
    )
    assert len(failures) == 1 and "403" in failures[0]
    assert fake.files[note]["folder_id"] is None  # untouched
    assert fake.files[other]["folder_id"] == dest  # the other one moved


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_file_and_empty_folder():
    fake = FakeClient()
    note = fake.add_file("note.txt")
    empty = fake.add_folder("Empty")

    assert delete_entries(fake, [Target("file", note, "note.txt"), Target("folder", empty, "Empty")]) == ([], 2)
    assert note not in fake.files
    assert empty not in fake.folders


def test_delete_folder_recursively():
    fake = FakeClient()
    alpha = fake.add_folder("Alpha")
    sub = fake.add_folder("Sub", parent=alpha)
    fake.add_file("top.txt", folder=alpha)
    fake.add_file("deep.csv", folder=sub)

    assert delete_entries(fake, [Target("folder", alpha, "Alpha")]) == ([], 4)
    assert fake.folders == {}
    assert fake.files == {}


def test_delete_targets_already_gone_with_the_tree_are_not_failures():
    fake = FakeClient()
    alpha = fake.add_folder("Alpha")
    sub = fake.add_folder("Sub", parent=alpha)
    inside = fake.add_file("inside.txt", folder=alpha)

    failures = delete_entries(
        fake,
        [
            Target("folder", alpha, "Alpha"),
            Target("folder", sub, "Sub"),  # deleted via Alpha
            Target("file", inside, "inside.txt"),
        ],
    )
    assert failures == ([], 3)
    assert fake.folders == {}
    assert fake.files == {}


def test_delete_folder_keeps_it_when_a_child_is_locked():
    fake = FakeClient()
    alpha = fake.add_folder("Alpha")
    locked = fake.add_file("locked.txt", folder=alpha)
    fake.add_file("free.txt", folder=alpha)
    fake.locked.add(locked)

    failures = delete_entries(fake, [Target("folder", alpha, "Alpha")])
    assert isinstance(failures, tuple)
    failures_list = failures[0] if isinstance(failures, tuple) else failures
    assert any("403" in f for f in failures_list)  # the locked file…
    assert any("folder kept" in f for f in failures_list)  # …and thus the folder
    assert alpha in fake.folders  # kept whole, never half-deleted
    assert locked in fake.files
    assert not any(f["name"] == "free.txt" for f in fake.files.values())


def test_delete_folder_with_a_parent_cycle_terminates():
    fake = FakeClient()
    a = fake.add_folder("A")
    b = fake.add_folder("B", parent=a)
    fake.folders[a]["parent"] = b  # corrupt data: a cycle

    failures = delete_entries(fake, [Target("folder", a, "A")])
    assert failures  # it cannot succeed, but it must not hang either


def test_delete_locked_file_reports_the_403():
    fake = FakeClient()
    note = fake.add_file("note.txt")
    fake.locked.add(note)

    failures, _ = delete_entries(fake, [Target("file", note, "note.txt")])
    assert len(failures) == 1 and "403" in failures[0]
    assert note in fake.files


def test_delete_missing_item_reports_an_error():
    fake = FakeClient()
    failures, _ = delete_entries(fake, [Target("file", 9999, "gone.txt")])
    assert len(failures) == 1 and "404" in failures[0]


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def test_copy_file_preserves_bytes_mime_and_notes():
    fake = FakeClient()
    dest = fake.add_folder("Dest")
    src = fake.add_file(
        "note.txt",
        mime="text/markdown",
        content=b"hello\n",
        notes="keep me",
    )

    assert copy_entries(fake, [Target("file", src, "note.txt")], dest) == []
    copies = [f for f in fake.files.values() if f["folder_id"] == dest]
    assert len(copies) == 1
    assert copies[0]["mime"] == "text/markdown"
    assert fake.contents[copies[0]["id"]] == b"hello\n"
    assert fake.files[src]["folder_id"] is None  # source untouched


def test_copy_file_collision_gets_copy_suffix():
    fake = FakeClient()
    dest = fake.add_folder("Dest")
    src = fake.add_file("note.txt", content=b"NEW\n")
    fake.add_file("note.txt", folder=dest, content=b"OLD\n")

    assert copy_entries(fake, [Target("file", src, "note.txt")], dest) == []
    names = [f["name"] for f in fake.files.values() if f["folder_id"] == dest]
    # the destination keeps its own note.txt; the copy steps aside
    assert sorted(names) == ["note (copy).txt", "note.txt"]
    copy_id = next(
        f["id"] for f in fake.files.values() if f["name"] == "note (copy).txt"
    )
    assert fake.contents[copy_id] == b"NEW\n"


def test_copy_file_force_replaces_the_old_copy():
    fake = FakeClient()
    dest = fake.add_folder("Dest")
    src = fake.add_file("note.txt", content=b"NEW\n")
    victim = fake.add_file("note.txt", folder=dest, content=b"OLD\n")

    assert copy_entries(
        fake, [Target("file", src, "note.txt")], dest, force=True
    ) == []
    at_dest = [f for f in fake.files.values() if f["folder_id"] == dest]
    assert len(at_dest) == 1  # exactly one note.txt remains
    assert at_dest[0]["id"] != victim
    assert fake.contents[at_dest[0]["id"]] == b"NEW\n"
    assert victim not in fake.files


def test_copy_file_force_refuses_a_locked_victim():
    fake = FakeClient()
    dest = fake.add_folder("Dest")
    src = fake.add_file("note.txt", content=b"NEW\n")
    victim = fake.add_file("note.txt", folder=dest, content=b"OLD\n")
    fake.locked.add(victim)

    failures = copy_entries(
        fake, [Target("file", src, "note.txt")], dest, force=True
    )
    assert len(failures) == 1 and "locked" in failures[0]
    assert fake.count("upload_file") == 0  # nothing half-done
    assert victim in fake.files


def test_copy_file_into_its_own_folder_needs_a_suffix():
    fake = FakeClient()
    src = fake.add_file("note.txt", content=b"DATA\n")

    assert copy_entries(fake, [Target("file", src, "note.txt")], None) == []
    names = sorted(f["name"] for f in fake.files.values())
    assert names == ["note (copy).txt", "note.txt"]  # the source still counts


def test_copy_folder_recurses_over_the_whole_tree():
    fake = FakeClient()
    alpha = fake.add_folder("Alpha")
    sub = fake.add_folder("Sub", parent=alpha)
    fake.add_file("top.md", folder=alpha, content=b"top\n")
    fake.add_file("deep.csv", folder=sub, content=b"a,b\n")
    dest = fake.add_folder("Dest")

    assert copy_entries(fake, [Target("folder", alpha, "Alpha")], dest) == []
    alpha_copies = [
        f for f in fake.folders.values() if f["name"] == "Alpha" and f["parent"] == dest
    ]
    assert len(alpha_copies) == 1
    new_alpha = alpha_copies[0]["id"]
    sub_copies = [
        f for f in fake.folders.values() if f["name"] == "Sub" and f["parent"] == new_alpha
    ]
    assert len(sub_copies) == 1
    # contents came along
    top = next(f for f in fake.files.values() if f["name"] == "top.md")
    deep = next(f for f in fake.files.values() if f["name"] == "deep.csv")
    assert fake.contents[top["id"]] == b"top\n"
    assert fake.contents[deep["id"]] == b"a,b\n"
    # sources untouched
    assert fake.folders[alpha]["parent"] is None
    assert fake.folders[sub]["parent"] == alpha


def test_copy_folder_collision_then_force_merges():
    fake = FakeClient()
    alpha = fake.add_folder("Alpha")
    fake.add_file("a.txt", folder=alpha, content=b"SRC\n")
    dest = fake.add_folder("Dest")
    existing = fake.add_folder("Alpha", parent=dest)
    fake.add_file("a.txt", folder=existing, content=b"OLD\n")

    # not forcing: the existing folder keeps its name, a sibling appears
    assert copy_entries(fake, [Target("folder", alpha, "Alpha")], dest) == []
    siblings = [f for f in fake.folders.values() if f["parent"] == dest]
    assert sorted(f["name"] for f in siblings) == ["Alpha", "Alpha (copy)"]

    # forcing: reuses the existing folder and overwrites the file
    assert copy_entries(
        fake, [Target("folder", alpha, "Alpha")], dest, force=True
    ) == []
    assert [f for f in fake.folders.values() if f["parent"] == dest] == siblings
    at_existing = [f for f in fake.files.values() if f["folder_id"] == existing]
    assert len(at_existing) == 1
    assert fake.contents[at_existing[0]["id"]] == b"SRC\n"


def test_copy_folder_refuses_a_parent_cycle():
    fake = FakeClient()
    a = fake.add_folder("A")
    b = fake.add_folder("B")
    # make a parent-cycle: A's parent is B, B's parent is A
    fake.folders[a]["parent"] = b
    fake.folders[b]["parent"] = a

    failures = copy_entries(fake, [Target("folder", a, "A")], None)
    assert failures and "loop" in failures[0]


def test_copy_partial_failure_keeps_the_rest_going():
    got = None

    class OneBadFile(FakeClient):
        def get_file(self, file_id):
            if file_id == got:
                raise NoCfoError("404 vanished", 404)
            return super().get_file(file_id)

    fake = OneBadFile()
    dest = fake.add_folder("Dest")
    got = fake.add_file("bad.txt")
    fine = fake.add_file("fine.txt")

    failures = copy_entries(
        fake,
        [Target("file", got, "bad.txt"), Target("file", fine, "fine.txt")],
        dest,
    )
    assert len(failures) == 1 and "404" in failures[0]
    copied = [f for f in fake.files.values() if f["folder_id"] == dest]
    assert [f["name"] for f in copied] == ["fine.txt"]
