"""Client-level tests: request shapes, parsing, retries — no network."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from nocfo_explorer.nocfo import (
    NoCfoClient,
    NoCfoError,
    NocfoFile,
    guess_mime,
)

BASE = "https://api.test"


def make_client(handler) -> NoCfoClient:
    return NoCfoClient(BASE, "tok", "biz", transport=httpx.MockTransport(handler))


# -- MIME ------------------------------------------------------------------


def test_guess_mime_known_types():
    assert guess_mime("a.pdf") == "application/pdf"
    assert guess_mime("b.JPG") == "image/jpeg"
    assert guess_mime("photo.heic") == "image/heic"
    assert guess_mime("data.zzzunknown") == "application/octet-stream"
    assert guess_mime("noext") == "application/octet-stream"


# -- PATCH rename/move -----------------------------------------------------


def test_update_file_sends_only_provided_fields():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7, "name": "new", "folder_id": 2})

    c = make_client(handler)
    c.update_file(7, name="new")
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/v1/business/biz/files/7/"
    assert seen["body"] == {"name": "new"}


def test_update_file_folder_id_none_clears_the_folder():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7, "name": "x", "folder_id": None})

    c = make_client(handler)
    c.update_file(7, folder_id=None)
    assert seen["body"] == {"folder_id": None}


def test_update_file_moves_and_returns_parsed_file():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"folder_id": 42}
        return httpx.Response(
            200,
            json={"id": 7, "name": "x.pdf", "type": "application/pdf", "folder_id": 42},
        )

    moved = make_client(handler).update_file(7, folder_id=42)
    assert isinstance(moved, NocfoFile)
    assert (moved.id, moved.name, moved.folder_id) == (7, "x.pdf", 42)
    assert moved.mime == "application/pdf"


def test_update_folder_patches_undocumented_endpoint():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 9, "name": "renamed", "parent": 3})

    c = make_client(handler)
    c.update_folder(9, name="renamed", parent=3)
    assert seen["path"] == "/v1/business/biz/folders/9/"
    assert seen["body"] == {"name": "renamed", "parent": 3}


def test_update_folder_omits_unset_fields():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 9, "name": "only-name"})

    make_client(handler).update_folder(9, name="only-name")
    assert seen["body"] == {"name": "only-name"}


# -- listing / pagination --------------------------------------------------


def test_list_files_params_and_pagination():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        page = int(request.url.params["page"])
        results = [{"id": page * 10, "name": f"f{page}", "type": "text/plain"}]
        return httpx.Response(
            200, json={"results": results, "next": page + 1 if page < 3 else None}
        )

    c = make_client(handler)
    files = list(c.list_files(folder=5, search="x"))
    assert [f.id for f in files] == [10, 20, 30]
    assert [call["page"] for call in calls] == ["1", "2", "3"]
    assert all(call["folder"] == "5" for call in calls)
    assert all(call["search"] == "x" for call in calls)
    assert all(call["page_size"] == "100" for call in calls)


def test_list_files_root_and_is_used_flags():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={"results": []})

    c = make_client(handler)
    list(c.list_files(root=True))
    list(c.list_files(is_used=True))
    assert calls[0]["root"] == "true"
    assert calls[1]["is_used"] == "true"


def test_search_files_respects_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        results = [
            {"id": i, "name": f"f{i}", "type": "text/plain"} for i in range(50)
        ]
        return httpx.Response(200, json={"results": results, "next": None})

    assert len(make_client(handler).search_files("f", limit=7)) == 7


def test_attached_file_ids_across_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        results = [{"id": page * 100 + i, "name": "x"} for i in range(2)]
        return httpx.Response(
            200, json={"results": results, "next": page + 1 if page < 2 else None}
        )

    assert make_client(handler).attached_file_ids() == {100, 101, 200, 201}


# -- detail parsing --------------------------------------------------------


def test_get_file_parses_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 4711,
                "name": "inv.pdf",
                "type": "application/pdf",
                "folder_id": 3,
                "file": "https://api.test/media/inv.pdf",
                "preview": {"image": "https://api.test/prev.png"},
                "notes": "Q1",
                "is_deletable": False,
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    f = make_client(handler).get_file(4711)
    assert f.name == "inv.pdf"
    assert f.url == "https://api.test/media/inv.pdf"
    assert f.preview_url == "https://api.test/prev.png"
    assert f.is_deletable is False
    assert f.notes == "Q1"


def test_get_folder_detail_keeps_breadcrumbs_children_has_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 9,
                "name": "Sub",
                "parent": 5,
                "breadcrumbs": [{"id": 5, "name": "Top"}],
                "children": [{"id": 10, "name": "Deep"}],
                "has_content": True,
            },
        )

    f = make_client(handler).get_folder(9)
    assert f.parent == 5
    assert f.breadcrumbs == [{"id": 5, "name": "Top"}]
    assert f.children == [{"id": 10, "name": "Deep"}]
    assert f.has_content is True


# -- ancestor / descendant checks -----------------------------------------


def test_ancestor_ids_uses_breadcrumbs():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 9,
                "name": "Sub",
                "parent": 5,
                "breadcrumbs": [{"id": 1, "name": "Root"}, {"id": 5, "name": "Top"}],
            },
        )

    assert make_client(handler).ancestor_ids(9) == {9, 5, 1}


def test_ancestor_ids_walks_parents_when_no_breadcrumbs():
    def handler(request: httpx.Request) -> httpx.Response:
        folder_id = int(request.url.path.rstrip("/").split("/")[-1])
        parents = {7: 3, 3: None}
        return httpx.Response(
            200, json={"id": folder_id, "name": f"f{folder_id}", "parent": parents[folder_id]}
        )

    assert make_client(handler).ancestor_ids(7) == {7, 3}


# -- uploads ---------------------------------------------------------------


def test_upload_sends_name_type_and_folder():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(
            200, json={"id": 1, "name": "a.pdf", "type": "application/pdf"}
        )

    c = make_client(handler)
    c.upload_file("a.pdf", b"%PDF-1.4", folder_id=5)
    assert b'name="name"' in seen["body"]
    assert b'name="type"' in seen["body"] and b"application/pdf" in seen["body"]
    assert b'name="folder_id"' in seen["body"] and b"5" in seen["body"]


def test_upload_truncates_overlong_names_to_256():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"id": 1, "name": "x", "type": "t"})

    long = "y" * 300 + ".pdf"
    make_client(handler).upload_file(long, b"x")
    name_field = seen["body"].split(b'name="name"\r\n\r\n')[1].split(b"\r\n")[0]
    assert len(name_field) <= 256
    assert name_field.endswith(b".pdf")


def test_upload_idempotency_header_falls_back_on_500():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("Idempotency-Key"):
            return httpx.Response(500, text="<html>boom</html>")
        return httpx.Response(200, json={"id": 1, "name": "a.pdf"})

    c = make_client(handler)
    f = c.upload_file("a.pdf", b"x")
    assert f.id == 1
    assert len(requests) == 2
    assert requests[0].headers.get("Idempotency-Key")
    assert not requests[1].headers.get("Idempotency-Key")
    assert c.idempotency is False
    assert c.notices  # the user is told what happened


# -- downloads -------------------------------------------------------------


def test_download_same_origin_keeps_auth_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"PDFDATA")

    c = make_client(handler)
    f = NocfoFile(id=1, name="a.pdf", mime="application/pdf", folder_id=None,
                  url=f"{BASE}/media/a.pdf")
    assert c.download_file(f) == b"PDFDATA"
    assert seen["auth"] == "Token tok"


def test_download_off_origin_sends_no_auth_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"IMG")

    c = make_client(handler)
    f = NocfoFile(id=1, name="a.png", mime="image/png", folder_id=None,
                  url="https://bucket.test/signed/a.png")
    assert c.download_file(f) == b"IMG"
    assert seen["auth"] is None


def test_download_max_bytes_caps_stream():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"z" * 10_000)

    c = make_client(handler)
    f = NocfoFile(id=1, name="big.txt", mime="text/plain", folder_id=None,
                  url=f"{BASE}/big.txt")
    assert len(c.download_file(f, max_bytes=256)) == 256


def test_download_without_url_fails_loudly():
    c = make_client(lambda request: httpx.Response(200, json={}))
    f = NocfoFile(id=1, name="x", mime="", folder_id=None, url="")
    with pytest.raises(NoCfoError, match="no download URL"):
        c.download_file(f)


def test_download_http_error_carries_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    c = make_client(handler)
    f = NocfoFile(id=1, name="x", mime="", folder_id=None, url=f"{BASE}/x")
    with pytest.raises(NoCfoError) as e:
        c.download_file(f)
    assert e.value.status_code == 404


# -- errors / retries ------------------------------------------------------


def test_delete_folder_500_raises_with_status(monkeypatch):
    # 500 is a retryable status (transient errors deserve the backoff) —
    # the non-empty-folder 500 never reaches here because ops pre-checks.
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="<html>not empty</html>")

    with pytest.raises(NoCfoError) as e:
        make_client(handler).delete_folder(3)
    assert e.value.status_code == 500
    assert calls["n"] == 5  # initial attempt + 4 retries


def test_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="later")
        return httpx.Response(200, json={"id": 1, "name": "u"})

    c = make_client(handler)
    assert c.check() == "u"
    assert calls["n"] == 2


def test_client_error_does_not_retry(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: (_ for _ in ()).throw(AssertionError()))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad token")

    with pytest.raises(NoCfoError) as e:
        make_client(handler).current_user()
    assert e.value.status_code == 401
    assert calls["n"] == 1
