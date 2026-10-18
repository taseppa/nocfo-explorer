"""NoCFO REST client.

Covers the documented files API (https://developers.nocfo.io) plus the
*undocumented* folders API that nocfo-sync discovered by probing
(``/v1/business/{slug}/folders/``).  The folders endpoints are not in the
public OpenAPI schema and may change without notice; all calls fail loudly
on unexpected shapes.

Notes that shaped this module:

* ``PATCH /files/{id}/`` renames (``name``) and moves (``folder_id``) a file;
  ``PATCH /folders/{id}/`` renames (``name``) and moves (``parent``) a folder.
* Deleting a folder only succeeds when it is empty — the server answers a
  non-empty folder with an HTML 500 page.  ``ops.delete_entries`` works
  around it by deleting the tree bottom-up (children first) instead of
  firing the request at a non-empty folder.
* ``DELETE /files/{id}/`` is refused for files the server considers unsafe to
  delete (``is_deletable: false`` on the detail endpoint), typically files
  attached to documents.
* There is no native copy: :meth:`NoCfoClient.download_file` +
  :meth:`NoCfoClient.upload_file` implement it.
"""

from __future__ import annotations

import mimetypes
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

# Extensions that ``mimetypes`` does not reliably know (varies by Python
# build/platform).  These feed NoCFO's ``type`` field, so be explicit.
_EXTRA_MIME_TYPES = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".webp": "image/webp",
    ".avif": "image/avif",
}


def guess_mime(name: str) -> str:
    """Best-effort MIME type for a filename (NoCFO's ``type`` field)."""
    lower = name.lower()
    for ext, mime in _EXTRA_MIME_TYPES.items():
        if lower.endswith(ext):
            return mime
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class NoCfoError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# Sentinel meaning "argument not provided" — distinct from an explicit
# ``None``, which is a real value (move to root / clear the parent).
_UNSET: Any = object()


@dataclass
class NocfoFolder:
    id: int
    name: str
    parent: int | None
    breadcrumbs: list[dict] | None = None  # only from the detail endpoint
    children: list[dict] | None = None  # only from the detail endpoint
    has_content: bool | None = None  # files or subfolders present


@dataclass
class NocfoFile:
    id: int
    name: str
    mime: str  # the ``type`` field; may be "" for pre-fix uploads
    folder_id: int | None
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    url: str = ""  # download URL (the ``file`` field)
    preview_url: str | None = None
    is_deletable: bool | None = None  # only from the detail endpoint
    raw: dict = field(default_factory=dict, repr=False)


def _file_from_record(r: dict) -> NocfoFile:
    preview = r.get("preview") or {}
    return NocfoFile(
        id=r["id"],
        name=r.get("name", ""),
        mime=r.get("type") or "",
        folder_id=r.get("folder_id"),
        notes=r.get("notes") or "",
        created_at=r.get("created_at") or "",
        updated_at=r.get("updated_at") or "",
        url=r.get("file") or "",
        preview_url=preview.get("image") or None,
        is_deletable=r.get("is_deletable"),
        raw=r,
    )


class NoCfoClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        base_url: str,
        token: str,
        business_slug: str,
        timeout: float = 30.0,
        max_retries: int = 4,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.business_slug = business_slug
        self.max_retries = max_retries
        self.idempotency = True  # may be disabled at runtime (see upload_file)
        self.notices: list[str] = []
        common: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            common["transport"] = transport
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Token {token}"},
            follow_redirects=True,
            **common,
        )
        # Same transport, no auth header — for off-origin download URLs
        # (a signed bucket URL rejects a foreign Authorization header).
        self._bare = httpx.Client(follow_redirects=True, **common)

    def close(self) -> None:
        self._http.close()
        self._bare.close()

    def __enter__(self) -> NoCfoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level -------------------------------------------------------

    def _request(
        self, method: str, path: str, retry_5xx: bool = True, **kwargs
    ) -> Any:
        retry_statuses = set(self.RETRY_STATUSES)
        if not retry_5xx:
            retry_statuses -= {500, 502, 503, 504}
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.HTTPError as e:
                last = e
                if attempt == self.max_retries:
                    raise NoCfoError(f"{method} {path}: network error: {e}") from e
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code >= 400:
                if resp.status_code in retry_statuses and attempt < self.max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise NoCfoError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:500]}",
                    resp.status_code,
                )
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise NoCfoError(f"{method} {path}: exhausted retries: {last}")

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = 1
        params = dict(params or {})
        params.setdefault("page_size", 100)
        while True:
            params["page"] = page
            data = self._request("GET", path, params=params)
            yield from data.get("results", [])
            if not data.get("next"):
                return
            page = data["next"]

    # -- token / business discovery --------------------------------------

    def current_user(self) -> dict:
        """``GET /v1/user/`` — validates the token without a business slug."""
        return self._request("GET", "/v1/user/")

    def list_businesses(self) -> list[dict]:
        """``GET /v1/business/`` — businesses the token can access."""
        return list(self._paginate("/v1/business/"))

    def check(self) -> str:
        """Verify auth + business slug; returns the business name."""
        data = self._request("GET", f"/v1/business/{self.business_slug}/")
        return data.get("name", self.business_slug)

    # -- folders (undocumented!) -----------------------------------------

    def list_root_folders(self) -> list[NocfoFolder]:
        return [
            NocfoFolder(id=r["id"], name=r["name"], parent=r.get("parent"))
            for r in self._paginate(f"/v1/business/{self.business_slug}/folders/")
        ]

    def get_folder(self, folder_id: int) -> NocfoFolder:
        r = self._request("GET", f"/v1/business/{self.business_slug}/folders/{folder_id}/")
        return NocfoFolder(
            id=r["id"],
            name=r["name"],
            parent=r.get("parent"),
            breadcrumbs=r.get("breadcrumbs"),
            children=r.get("children"),
            has_content=r.get("has_content"),
        )

    def create_folder(self, name: str, parent: int | None) -> NocfoFolder:
        body: dict[str, Any] = {"name": name}
        if parent is not None:
            body["parent"] = parent
        r = self._request("POST", f"/v1/business/{self.business_slug}/folders/", json=body)
        return NocfoFolder(id=r["id"], name=r["name"], parent=r.get("parent"))

    def update_folder(
        self,
        folder_id: int,
        *,
        name: Any = _UNSET,
        parent: Any = _UNSET,
    ) -> NocfoFolder:
        """Rename / move a folder (undocumented PATCH; see README)."""
        body: dict[str, Any] = {}
        if name is not _UNSET:
            body["name"] = name
        if parent is not _UNSET:
            body["parent"] = parent
        r = self._request(
            "PATCH", f"/v1/business/{self.business_slug}/folders/{folder_id}/", json=body
        )
        return NocfoFolder(
            id=r.get("id", folder_id),
            name=r.get("name", name if name is not _UNSET else ""),
            parent=r.get("parent", parent if parent is not _UNSET else None),
        )

    def delete_folder(self, folder_id: int) -> None:
        # Only empty folders: a non-empty one returns an HTML 500 page.
        self._request("DELETE", f"/v1/business/{self.business_slug}/folders/{folder_id}/")

    def ancestor_ids(self, folder_id: int) -> set[int]:
        """``{folder_id}`` plus every ancestor id, for descendant checks.

        Uses the detail endpoint's ``breadcrumbs`` when present and falls
        back to walking ``parent`` links (bounded) when they are not.
        """
        ids: set[int] = {folder_id}
        detail = self.get_folder(folder_id)
        crumbs = detail.breadcrumbs
        if crumbs is not None:
            for c in crumbs:
                if isinstance(c, dict) and c.get("id") is not None:
                    ids.add(c["id"])
            return ids
        # fallback: walk up via parent links (never loop forever)
        current = detail.parent
        for _ in range(64):
            if current is None:
                break
            if current in ids:
                break
            ids.add(current)
            current = self.get_folder(current).parent
        return ids

    # -- files -----------------------------------------------------------

    def list_files(
        self,
        folder: int | None = None,
        root: bool | None = None,
        is_used: bool | None = None,
        search: str | None = None,
    ) -> Iterator[NocfoFile]:
        params: dict[str, Any] = {}
        if folder is not None:
            params["folder"] = folder
        if root is not None:
            params["root"] = str(root).lower()
        if is_used is not None:
            params["is_used"] = str(is_used).lower()
        if search is not None:
            params["search"] = search
        for r in self._paginate(f"/v1/business/{self.business_slug}/files/", params):
            yield _file_from_record(r)

    def get_file(self, file_id: int) -> NocfoFile:
        r = self._request("GET", f"/v1/business/{self.business_slug}/files/{file_id}/")
        return _file_from_record(r)

    def search_files(self, term: str, limit: int = 200) -> list[NocfoFile]:
        """Files matching ``term``, capped at ``limit`` results."""
        out: list[NocfoFile] = []
        for f in self.list_files(search=term):
            out.append(f)
            if len(out) >= limit:
                break
        return out

    def attached_file_ids(self) -> set[int]:
        """Ids of files attached to at least one document.

        One paginated sweep (cached by the caller) — used to warn before
        deleting and to badge files in the preview pane.
        """
        return {f.id for f in self.list_files(is_used=True)}

    def update_file(
        self,
        file_id: int,
        *,
        name: Any = _UNSET,
        folder_id: Any = _UNSET,
        notes: Any = _UNSET,
    ) -> NocfoFile:
        """Rename / move a file.  ``folder_id=None`` moves it to the root."""
        body: dict[str, Any] = {}
        if name is not _UNSET:
            body["name"] = name
        if folder_id is not _UNSET:
            body["folder_id"] = folder_id
        if notes is not _UNSET:
            body["notes"] = notes
        r = self._request(
            "PATCH", f"/v1/business/{self.business_slug}/files/{file_id}/", json=body
        )
        return _file_from_record(r) if isinstance(r, dict) else self.get_file(file_id)

    def delete_file(self, file_id: int) -> None:
        self._request("DELETE", f"/v1/business/{self.business_slug}/files/{file_id}/")

    def upload_file(
        self,
        name: str,
        content: bytes,
        folder_id: int | None = None,
        mime: str | None = None,
        notes: str | None = None,
        timeout: float = 120.0,
    ) -> NocfoFile:
        """Upload via multipart (also the copy backend).

        Sends ``Idempotency-Key`` so retries inside this client cannot create
        duplicates.  Some environments (observed on api-tst.nocfo.io) answer
        any request carrying that header with 500 — on such a 500 we retry
        once without it and disable idempotency for the rest of the session.

        The ``type`` form field is what NoCFO stores as the attachment type
        and what gates server-side preview generation.
        """
        if len(name) > 256:
            stem, dot, ext = name.rpartition(".")
            if dot and len(ext) < 20:
                name = name[: 256 - len(ext) - 1] + "." + ext
            else:
                name = name[:256]
        mime = mime or guess_mime(name)
        data: dict[str, str] = {"name": name, "type": mime}
        if folder_id is not None:
            data["folder_id"] = str(folder_id)
        if notes:
            data["notes"] = notes
        path = f"/v1/business/{self.business_slug}/file_upload/"
        files = {"file": (name, content, mime)}

        if self.idempotency:
            try:
                r = self._request(
                    "POST", path, retry_5xx=False, data=data, files=files,
                    timeout=timeout,
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                )
            except NoCfoError as e:
                if e.status_code != 500:
                    raise
                self.idempotency = False
                self.notices.append(
                    "NoCFO returned 500 for file_upload with Idempotency-Key; "
                    "retrying without it and disabling idempotency for this run."
                )
            else:
                return _file_from_record(r)
        r = self._request("POST", path, data=data, files=files, timeout=timeout)
        return _file_from_record(r)

    def download_file(
        self,
        file: NocfoFile,
        *,
        max_bytes: int | None = None,
        timeout: float = 60.0,
    ) -> bytes:
        """Fetch a file's content (used by preview and copy).

        Same-origin URLs go through the authenticated client; off-origin
        (signed bucket) URLs are fetched without the Authorization header.
        ``max_bytes`` streams and stops early — previews never pull a
        100 MB attachment just to show the first lines.
        """
        if not file.url:
            raise NoCfoError(f"file {file.id} has no download URL")
        same_origin = file.url.startswith(self.base_url)
        client = self._http if same_origin else self._bare
        try:
            with client.stream("GET", file.url, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    raise NoCfoError(
                        f"GET {file.url} -> {resp.status_code}", resp.status_code
                    )
                if max_bytes is None:
                    return resp.read()
                chunks: list[bytes] = []
                got = 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    got += len(chunk)
                    if got >= max_bytes:
                        break
                return b"".join(chunks)[:max_bytes]
        except httpx.HTTPError as e:
            raise NoCfoError(f"GET {file.url}: network error: {e}") from e
