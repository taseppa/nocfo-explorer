"""File operations with the guards the API does not give us.

Pure functions over a :class:`~nocfo_explorer.nocfo.NoCfoClient` — no Textual
imports, so the whole module is testable head-less.  Multi-item operations
(``move_entries``, ``delete_entries``, ``copy_entries``) keep going after a
per-item failure and return the list of user-readable failure messages;
wholesale problems (bad names, unusable destination) raise :class:`OpsError`.
"""

from __future__ import annotations

from dataclasses import dataclass

from nocfo_explorer.nocfo import NocfoFile, NocfoFolder, NoCfoError


class OpsError(Exception):
    """Operation refused; ``str(e)`` is safe to show to the user."""


@dataclass(frozen=True)
class Target:
    """One item an operation acts on (the app maps list entries to these)."""

    kind: str  # "folder" | "file"
    id: int
    name: str


def _check_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise OpsError("the name cannot be empty")
    if "/" in name or "\\" in name:
        raise OpsError("names cannot contain slashes")
    return name


def copy_name(name: str, taken: set[str]) -> str:
    """``report.pdf`` -> ``report (copy).pdf``, then ``report (copy 2).pdf``."""
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:  # no usable extension: suffix the whole name
        stem, ext = name, ""
    n = 1
    while True:
        suffix = " (copy)" if n == 1 else f" (copy {n})"
        candidate = f"{stem}{suffix}.{ext}" if ext else f"{stem}{suffix}"
        if candidate not in taken:
            return candidate
        n += 1


def listing_names(client, folder_id: int | None) -> set[str]:
    """Names of everything directly inside ``folder_id`` (None = root)."""
    if folder_id is None:
        folders = list(client.list_root_folders())
        files = list(client.list_files(root=True))
    else:
        detail = client.get_folder(folder_id)
        folders = [
            NocfoFolder(id=c["id"], name=c["name"], parent=folder_id)
            for c in (detail.children or [])
            if isinstance(c, dict) and c.get("id") is not None
        ]
        files = list(client.list_files(folder=folder_id))
    return {f.name for f in folders} | {f.name for f in files}


def _child_folder_id(client, folder_id: int | None, name: str) -> int | None:
    """The id of the child folder called ``name`` (None when there is none)."""
    if folder_id is None:
        children = list(client.list_root_folders())
    else:
        detail = client.get_folder(folder_id)
        children = [
            NocfoFolder(id=c["id"], name=c["name"], parent=folder_id)
            for c in (detail.children or [])
            if isinstance(c, dict) and c.get("id") is not None
        ]
    for child in children:
        if child.name == name:
            return child.id
    return None


def _failures_or_raise(exc: NoCfoError) -> str:
    return str(exc)


# ---------------------------------------------------------------------------
# create / rename
# ---------------------------------------------------------------------------


def create_folder(client, name: str, parent_id: int | None) -> int:
    name = _check_name(name)
    if name in listing_names(client, parent_id):
        raise OpsError(f"'{name}' already exists here")
    try:
        folder = client.create_folder(name, parent_id)
    except NoCfoError as e:
        raise OpsError(str(e)) from e
    return folder.id


def rename_entry(
    client, target: Target, new_name: str, *, parent_id: int | None
) -> str:
    new_name = _check_name(new_name)
    if new_name == target.name:
        return new_name
    others = listing_names(client, parent_id) - {target.name}
    if new_name in others:
        raise OpsError(f"'{new_name}' already exists here")
    try:
        if target.kind == "folder":
            client.update_folder(target.id, name=new_name)
        else:
            client.update_file(target.id, name=new_name)
    except NoCfoError as e:
        raise OpsError(str(e)) from e
    return new_name


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


def move_entries(
    client, targets: list[Target], dest_id: int | None
) -> list[str]:
    """Move items into ``dest_id`` (None = the folder root).

    Refuses to move a folder inside itself or its own subtree — the API
    would happily create an unreachable tree.
    """
    failures: list[str] = []
    folder_ids = {t.id for t in targets if t.kind == "folder"}
    blocked: set[int] = set()
    if folder_ids and dest_id is not None:
        try:
            blocked = set(client.ancestor_ids(dest_id))
        except NoCfoError as e:
            raise OpsError(f"cannot inspect the destination: {e}") from e
    for t in targets:
        try:
            if t.kind == "folder":
                if dest_id is not None and (t.id == dest_id or t.id in blocked):
                    failures.append(f"can't move '{t.name}' into itself")
                    continue
                client.update_folder(t.id, parent=dest_id)
            else:
                client.update_file(t.id, folder_id=dest_id)
        except NoCfoError as e:
            failures.append(f"{t.name}: {e}")
    return failures


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def delete_entries(client, targets: list[Target]) -> list[str]:
    """Delete items; non-empty folders are skipped before the API's HTML 500."""
    failures: list[str] = []
    for t in targets:
        try:
            if t.kind == "folder":
                detail = client.get_folder(t.id)
                if detail.has_content:
                    failures.append(f"'{t.name}' is not empty — skipped")
                    continue
                client.delete_folder(t.id)
            else:
                client.delete_file(t.id)
        except NoCfoError as e:
            failures.append(f"{t.name}: {e}")
    return failures


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def _copy_file(client, file_id: int, dest_id: int | None, *, force: bool) -> str:
    src = client.get_file(file_id)
    data = client.download_file(src)
    name = src.name
    taken = listing_names(client, dest_id)
    existing: NocfoFile | None = None
    if name in taken:
        if not force:
            name = copy_name(src.name, taken)
        else:
            candidates = (
                list(client.list_files(root=True))
                if dest_id is None
                else list(client.list_files(folder=dest_id))
            )
            existing = next((f for f in candidates if f.name == name), None)
            if existing is not None and existing.is_deletable is False:
                raise OpsError(f"'{name}' exists here and is locked")
            if existing is None:
                # a folder holds the name — a file cannot replace it
                name = copy_name(src.name, taken)
    # upload first, then drop the old copy: a failed upload keeps the
    # original intact (P = "upload new content, then delete the old copy").
    client.upload_file(name, data, folder_id=dest_id, mime=src.mime, notes=src.notes)
    if existing is not None:
        client.delete_file(existing.id)
    return name


def _copy_folder(
    client, folder_id: int, dest_id: int | None, *, force: bool, seen: set[int]
) -> int:
    if folder_id in seen:
        raise OpsError("this folder tree contains a loop")
    seen.add(folder_id)
    src = client.get_folder(folder_id)
    taken = listing_names(client, dest_id)
    name = src.name
    new_id: int | None = None
    if name in taken:
        if force:
            new_id = _child_folder_id(client, dest_id, name)
        if new_id is None:
            # the name is held by a file, or we are not forcing: step aside
            name = copy_name(src.name, taken)
    if new_id is None:
        new_id = client.create_folder(name, dest_id).id
    for child in src.children or []:
        child_id = child.get("id") if isinstance(child, dict) else None
        if child_id is not None:
            _copy_folder(client, int(child_id), new_id, force=force, seen=seen)
    for f in client.list_files(folder=folder_id):
        _copy_file(client, f.id, new_id, force=force)
    return new_id


def copy_entries(
    client,
    targets: list[Target],
    dest_id: int | None,
    *,
    force: bool = False,
) -> list[str]:
    """Copy items into ``dest_id``; folders are copied with their whole tree.

    Collisions become ``name (copy).ext`` / ``name (copy 2).ext``; with
    ``force=True`` an existing file of the same name is replaced instead.
    """
    failures: list[str] = []
    seen: set[int] = set()
    for t in targets:
        try:
            if t.kind == "folder":
                _copy_folder(client, t.id, dest_id, force=force, seen=seen)
            else:
                _copy_file(client, t.id, dest_id, force=force)
        except OpsError as e:
            failures.append(str(e))
        except NoCfoError as e:
            failures.append(f"{t.name}: {e}")
    return failures
