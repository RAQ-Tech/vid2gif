import json
import socket
import urllib.error
import urllib.parse

import pytest

from app import emby_client


class FakeResponse:
    def __init__(self, body=b"", status=200):
        self.body = body
        self.status = status
        self.code = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


def _settings(url="http://emby:8096", key="secret"):
    return {"emby_url": url, "emby_api_key": key}


def _json_response(value, status=200):
    return FakeResponse(json.dumps(value).encode("utf-8"), status=status)


def test_endpoint_normalizes_emby_path_and_encodes_query_without_secret():
    params = {"Fields": "People,Path", "Tag": ["one", "two"], "Ignored": None}

    plain = emby_client.endpoint(_settings(), "/Items", params=params)
    suffixed = emby_client.endpoint(_settings("http://emby:8096/emby/"), "Items", params=params)

    assert plain == suffixed
    assert plain.startswith("http://emby:8096/emby/Items?")
    assert "Fields=People%2CPath" in plain
    assert "Tag=one&Tag=two" in plain
    assert "Ignored" not in plain
    assert "secret" not in plain
    assert "api_key" not in plain


def test_request_json_uses_header_auth_and_reads_json():
    captured = {}

    def opener(request, timeout):
        captured.update(request=request, timeout=timeout)
        return _json_response({"ServerName": "Emby"})

    data, result = emby_client.request_json(
        _settings(key="abc 123"),
        "/System/Info",
        params={"Fields": "Version"},
        opener=opener,
        timeout=9,
    )

    request = captured["request"]
    assert data == {"ServerName": "Emby"}
    assert result["status"] == "success"
    assert result["error_code"] is None
    assert request.full_url == "http://emby:8096/emby/System/Info?Fields=Version"
    assert request.get_header("X-emby-token") == "abc 123"
    assert request.get_header("Accept") == "application/json"
    assert captured["timeout"] == 9
    assert "abc 123" not in request.full_url
    assert "abc 123" not in str(result)


def test_request_json_encodes_json_body_and_rejects_two_body_types():
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        return _json_response({"ok": True})

    data, result = emby_client.request_json(
        _settings(),
        "/Library/Media/Updated",
        method="POST",
        json_body={"Updates": [{"Path": "/library/Movie", "UpdateType": "Updated"}]},
        opener=opener,
    )

    request = captured["request"]
    assert data == {"ok": True}
    assert result["status"] == "success"
    assert json.loads(request.data) == {
        "Updates": [{"Path": "/library/Movie", "UpdateType": "Updated"}]
    }
    assert request.get_header("Content-type") == "application/json"

    with pytest.raises(ValueError, match="cannot both"):
        emby_client.request_json(
            _settings(),
            "/Items",
            json_body={},
            body=b"raw",
            opener=opener,
        )


def test_request_no_content_preserves_raw_body_and_content_type():
    captured = {}

    def opener(request, timeout):
        captured.update(request=request, timeout=timeout)
        return FakeResponse(status=204)

    result = emby_client.request_no_content(
        _settings(),
        "/Items/p1/Images/Primary",
        body=b"image-bytes",
        content_type="image/jpeg",
        accept="application/json",
        opener=opener,
        timeout=30,
    )

    request = captured["request"]
    assert result["status"] == "success"
    assert result["http_status"] == 204
    assert request.data == b"image-bytes"
    assert request.get_header("Content-type") == "image/jpeg"
    assert request.get_header("X-emby-token") == "secret"
    assert captured["timeout"] == 30


def test_request_no_content_supports_json_body():
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        return FakeResponse(status=200)

    result = emby_client.request_no_content(
        _settings(),
        "/Items/one",
        json_body={"Name": "Updated"},
        opener=opener,
    )

    assert result["status"] == "success"
    assert captured["request"].data == b'{"Name":"Updated"}'
    assert captured["request"].get_header("Content-type") == "application/json"


def test_missing_configuration_is_typed_and_does_not_open_request():
    called = False

    def opener(request, timeout):
        nonlocal called
        called = True

    data, result = emby_client.request_json({}, "/Items", opener=opener)

    assert data is None
    assert result["status"] == "skipped"
    assert result["error_code"] == "missing_config"
    assert called is False


def test_http_error_is_typed_and_redacted():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized secret", None, None)

    data, result = emby_client.request_json(_settings(), "/Items", opener=opener)

    assert data is None
    assert result["status"] == "failed"
    assert result["error_code"] == "http_error"
    assert result["http_status"] == 401
    assert "secret" not in captured["url"]
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "error",
    [TimeoutError("slow secret"), socket.timeout("slow secret"), urllib.error.URLError(socket.timeout())],
)
def test_timeout_errors_are_typed_and_redacted(error):
    def opener(request, timeout):
        raise error

    data, result = emby_client.request_json(_settings(), "/Items", opener=opener)

    assert data is None
    assert result["status"] == "failed"
    assert result["error_code"] == "timeout"
    assert "secret" not in str(result)


def test_connection_error_is_typed_and_redacted():
    def opener(request, timeout):
        raise OSError("network secret failed")

    data, result = emby_client.request_json(_settings(), "/Items", opener=opener)

    assert data is None
    assert result["status"] == "failed"
    assert result["error_code"] == "connection_error"
    assert "secret" not in str(result)


@pytest.mark.parametrize("body", [b"", b"not-json"])
def test_invalid_or_empty_json_is_a_typed_failure(body):
    data, result = emby_client.request_json(
        _settings(),
        "/Items",
        opener=lambda request, timeout: FakeResponse(body),
    )

    assert data is None
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_response"


def test_json_null_is_valid_json():
    data, result = emby_client.request_json(
        _settings(),
        "/Items",
        opener=lambda request, timeout: FakeResponse(b"null"),
    )

    assert data is None
    assert result["status"] == "success"


def test_request_paged_json_collects_pages_and_preserves_parameters():
    captured = []
    original = {"Fields": "People,Path", "StartIndex": 999, "Limit": 1}

    def opener(request, timeout):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        captured.append(query)
        start = int(query["StartIndex"][0])
        if start == 0:
            return _json_response({"Items": [{"Id": "1"}, {"Id": "2"}], "TotalRecordCount": 3})
        return _json_response({"Items": [{"Id": "3"}], "TotalRecordCount": 3})

    items, result = emby_client.request_paged_json(
        _settings(),
        "/Items",
        params=original,
        page_size=2,
        opener=opener,
    )

    assert items == [{"Id": "1"}, {"Id": "2"}, {"Id": "3"}]
    assert result["status"] == "success"
    assert [page["StartIndex"] for page in captured] == [["0"], ["2"]]
    assert [page["Limit"] for page in captured] == [["2"], ["2"]]
    assert all(page["Fields"] == ["People,Path"] for page in captured)
    assert original == {"Fields": "People,Path", "StartIndex": 999, "Limit": 1}


@pytest.mark.parametrize(
    "payload",
    [
        {"TotalRecordCount": 0},
        {"Items": [], "TotalRecordCount": "0"},
        {"Items": [], "TotalRecordCount": -1},
        {"Items": [{"Id": "1"}], "TotalRecordCount": 0},
    ],
)
def test_request_paged_json_rejects_malformed_payloads(payload):
    items, result = emby_client.request_paged_json(
        _settings(),
        "/Items",
        opener=lambda request, timeout: _json_response(payload),
    )

    assert items is None
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_response"


def test_request_paged_json_rejects_early_empty_page():
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _json_response({"Items": [{"Id": "1"}], "TotalRecordCount": 2})
        return _json_response({"Items": [], "TotalRecordCount": 2})

    items, result = emby_client.request_paged_json(
        _settings(), "/Items", page_size=1, opener=opener
    )

    assert items is None
    assert result["error_code"] == "invalid_response"


def test_request_paged_json_rejects_total_changes_between_pages():
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        total = 2 if calls == 1 else 3
        return _json_response({"Items": [{"Id": str(calls)}], "TotalRecordCount": total})

    items, result = emby_client.request_paged_json(
        _settings(), "/Items", page_size=1, opener=opener
    )

    assert items is None
    assert result["error_code"] == "invalid_response"


def test_request_paged_json_discards_partial_results_after_failure():
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _json_response({"Items": [{"Id": "1"}], "TotalRecordCount": 2})
        raise urllib.error.HTTPError(request.full_url, 500, "failed", None, None)

    items, result = emby_client.request_paged_json(
        _settings(), "/Items", page_size=1, opener=opener
    )

    assert items is None
    assert result["error_code"] == "http_error"
    assert result["http_status"] == 500


def test_request_paged_json_propagates_cancellation_before_next_page():
    class Cancelled(Exception):
        pass

    checks = 0

    def before_page():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise Cancelled()

    with pytest.raises(Cancelled):
        emby_client.request_paged_json(
            _settings(),
            "/Items",
            page_size=1,
            before_page=before_page,
            opener=lambda request, timeout: _json_response(
                {"Items": [{"Id": "1"}], "TotalRecordCount": 2}
            ),
        )


def test_request_paged_json_rejects_non_positive_page_size():
    with pytest.raises(ValueError, match="positive"):
        emby_client.request_paged_json(_settings(), "/Items", page_size=0)
