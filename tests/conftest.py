"""Shared test doubles and fixtures.

The whole suite runs against :class:`FakeClient` — an in-memory NoCFO with
no network anywhere.  It mirrors ``NoCfoClient``'s surface and reproduces
the server quirks the app depends on (non-empty folder delete -> 500,
locked file delete -> 403).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nocfo_explorer.config import Config
from nocfo_explorer.nocfo import NoCfoError, NocfoFile, NocfoFolder

_UNSET = object()


class FakeClient:
    """In-memory NoCFO.  Pass as ``client_factory`` to ExplorerApp."""

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        business_slug: str = "acme",
        businesses: list[dict] | None = None,
        **_ignored,
    ):
        self.base_url = base_url
        self.token = token
        self.business_slug = business_slug
        self.businesses = (
            businesses
            if businesses is not None
            else [{"slug": "acme", "name": "Acme"}, {"slug": "beta", "name": "Beta Oy"}]
        )
        self.notices: list[str] = []
        self.calls: list[tuple] = []  # (method, args, kwargs)
        self.fail_token = False  # current_user raises 401
        self.fail_next: Exception | None = None  # raised once by the next call

        self._next_id = 100
        self.folders: dict[int, dict] = {}  # id -> {id, name, parent}
        self.files: dict[int, dict] = {}  # id -> stored file record
        self.contents: dict[int, bytes] = {}  # id -> download bytes
        self.attached: set[int] = set()  # "used" by documents
        self.locked: set[int] = set()  # is_deletable = False

    # -- seeding helpers --------------------------------------------------

    def _next(self) -> int:
        self._next_id += 1
        return self._next_id

    def add_folder(self, name: str, parent: int | None = None) -> int:
        fid = self._next()
        self.folders[fid] = {"id": fid, "name": name, "parent": parent}
        return fid

    def add_file(
        self,
        name: str,
        folder: int | None = None,
        mime: str = "text/plain",
        content: bytes = b"",
        notes: str = "",
    ) -> int:
        fid = self._next()
        self.files[fid] = {
            "id": fid,
            "name": name,
            "mime": mime,
            "folder_id": folder,
            "notes": notes,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "url": f"https://api.test/media/{fid}",
            "preview_url": None,
        }
        self.contents[fid] = content
        return fid

    def _record(self, method: str, *args, **kwargs) -> None:
        self.calls.append((method, args, kwargs))

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def count(self, method: str) -> int:
        return sum(1 for c in self.calls if c[0] == method)

    # -- token / business -------------------------------------------------

    def current_user(self) -> dict:
        self._record("current_user")
        if self.fail_token:
            raise NoCfoError("401 unauthorized", 401)
        return {"id": 1, "username": "tester"}

    def list_businesses(self) -> list[dict]:
        self._record("list_businesses")
        return list(self.businesses)

    def check(self) -> str:
        self._record("check")
        return "Acme"

    def close(self) -> None:
        self._record("close")

    # -- folders ----------------------------------------------------------

    def _subfolders(self, parent: int | None) -> list[dict]:
        return sorted(
            (f for f in self.folders.values() if f["parent"] == parent),
            key=lambda f: f["name"].lower(),
        )

    def _files_in(self, folder: int | None) -> list[NocfoFile]:
        return sorted(
            (
                self._file(f["id"])
                for f in self.files.values()
                if f["folder_id"] == folder
            ),
            key=lambda f: f.name.lower(),
        )

    def _breadcrumbs(self, folder_id: int) -> list[dict]:
        out: list[dict] = []
        cur: int | None = folder_id
        seen: set[int] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            f = self.folders[cur]
            out.append({"id": f["id"], "name": f["name"]})
            cur = f["parent"]
        return list(reversed(out))

    def _folder_has_content(self, folder_id: int) -> bool:
        return bool(self._subfolders(folder_id)) or any(
            f["folder_id"] == folder_id for f in self.files.values()
        )

    def list_root_folders(self) -> list[NocfoFolder]:
        self._record("list_root_folders")
        self._maybe_fail()
        return [
            NocfoFolder(id=f["id"], name=f["name"], parent=f["parent"])
            for f in self._subfolders(None)
        ]

    def get_folder(self, folder_id: int) -> NocfoFolder:
        self._record("get_folder", folder_id)
        self._maybe_fail()
        if folder_id not in self.folders:
            raise NoCfoError(f"404 folder {folder_id}", 404)
        f = self.folders[folder_id]
        return NocfoFolder(
            id=f["id"],
            name=f["name"],
            parent=f["parent"],
            breadcrumbs=self._breadcrumbs(folder_id),
            children=[{"id": c["id"], "name": c["name"]} for c in self._subfolders(folder_id)],
            has_content=self._folder_has_content(folder_id),
        )

    def create_folder(self, name: str, parent: int | None) -> NocfoFolder:
        self._record("create_folder", name, parent)
        self._maybe_fail()
        fid = self.add_folder(name, parent)
        return NocfoFolder(id=fid, name=name, parent=parent)

    def update_folder(
        self, folder_id: int, *, name=_UNSET, parent=_UNSET
    ) -> NocfoFolder:
        self._record(
            "update_folder",
            folder_id,
            **{k: v for k, v in {"name": name, "parent": parent}.items() if v is not _UNSET},
        )
        self._maybe_fail()
        f = self.folders[folder_id]
        if name is not _UNSET:
            f["name"] = name
        if parent is not _UNSET:
            f["parent"] = parent
        return NocfoFolder(id=folder_id, name=f["name"], parent=f["parent"])

    def delete_folder(self, folder_id: int) -> None:
        self._record("delete_folder", folder_id)
        self._maybe_fail()
        if self._folder_has_content(folder_id):
            # the real server answers an HTML 500 page for non-empty folders
            raise NoCfoError(f"500 folder {folder_id} not empty", 500)
        del self.folders[folder_id]

    def ancestor_ids(self, folder_id: int) -> set[int]:
        self._record("ancestor_ids", folder_id)
        return {c["id"] for c in self._breadcrumbs(folder_id)}

    # -- files ------------------------------------------------------------

    def _file(self, file_id: int) -> NocfoFile:
        if file_id not in self.files:
            raise NoCfoError(f"404 file {file_id}", 404)
        r = self.files[file_id]
        return NocfoFile(
            id=r["id"],
            name=r["name"],
            mime=r["mime"],
            folder_id=r["folder_id"],
            notes=r["notes"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            url=r["url"],
            preview_url=r["preview_url"],
            is_deletable=file_id not in self.locked,
            raw=dict(r),
        )

    def list_files(self, folder=None, root=None, is_used=None, search=None):
        self._record(
            "list_files",
            **{k: v for k, v in {"folder": folder, "root": root, "is_used": is_used, "search": search}.items() if v is not None},
        )
        self._maybe_fail()
        out = [self._file(f["id"]) for f in self.files.values()]
        if folder is not None:
            out = [f for f in out if f.folder_id == folder]
        if root is True:
            out = [f for f in out if f.folder_id is None]
        elif root is False:
            out = [f for f in out if f.folder_id is not None]
        if is_used is True:
            out = [f for f in out if f.id in self.attached]
        elif is_used is False:
            out = [f for f in out if f.id not in self.attached]
        if search is not None:
            needle = search.lower()
            out = [f for f in out if needle in f.name.lower()]
        yield from sorted(out, key=lambda f: f.name.lower())

    def get_file(self, file_id: int) -> NocfoFile:
        self._record("get_file", file_id)
        self._maybe_fail()
        return self._file(file_id)

    def search_files(self, term: str, limit: int = 200) -> list[NocfoFile]:
        self._record("search_files", term)
        return list(self.list_files(search=term))[:limit]

    def attached_file_ids(self) -> set[int]:
        self._record("attached_file_ids")
        return set(self.attached)

    def update_file(self, file_id: int, *, name=_UNSET, folder_id=_UNSET, notes=_UNSET):
        self._record(
            "update_file",
            file_id,
            **{k: v for k, v in {"name": name, "folder_id": folder_id, "notes": notes}.items() if v is not _UNSET},
        )
        self._maybe_fail()
        if file_id not in self.files:
            raise NoCfoError(f"404 file {file_id}", 404)
        r = self.files[file_id]
        if name is not _UNSET:
            r["name"] = name
        if folder_id is not _UNSET:
            r["folder_id"] = folder_id
        if notes is not _UNSET:
            r["notes"] = notes
        return self._file(file_id)

    def delete_file(self, file_id: int) -> None:
        self._record("delete_file", file_id)
        self._maybe_fail()
        if file_id in self.locked:
            raise NoCfoError(f"403 file {file_id} not deletable", 403)
        if file_id not in self.files:
            raise NoCfoError(f"404 file {file_id}", 404)
        del self.files[file_id]

    def upload_file(
        self,
        name: str,
        content: bytes,
        folder_id: int | None = None,
        mime: str | None = None,
        notes: str | None = None,
        timeout: float | None = None,
    ) -> NocfoFile:
        self._record("upload_file", name, **{"folder_id": folder_id, "mime": mime})
        self._maybe_fail()
        fid = self.add_file(name, folder=folder_id, mime=mime or "text/plain", content=content, notes=notes or "")
        return self._file(fid)

    def download_file(self, file: NocfoFile, *, max_bytes=None, timeout=None) -> bytes:
        self._record("download_file", file.id)
        self._maybe_fail()
        data = self.contents.get(file.id, b"")
        return data[:max_bytes] if max_bytes is not None else data


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No ambient config/env leaks into a test (cwd + HOME point at tmp)."""
    for var in ("NOCFO_EXPLORER_CONFIG", "NOCFO_TOKEN", "NOCFO_BUSINESS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def cfg() -> Config:
    """A fully configured app config (the wizard is not exercised)."""
    return Config(base_url="https://api.test", token="tok", business_slug="acme")


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()
