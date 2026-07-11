import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Literal, Mapping, TypedDict, cast

from .progress import utc_iso


EmbyStatus = Literal["success", "failed", "skipped", "disabled"]
EmbyErrorCode = Literal[
    "missing_config",
    "http_error",
    "timeout",
    "connection_error",
    "invalid_response",
]


class EmbyResult(TypedDict):
    status: EmbyStatus
    message: str
    checked_at: str | None
    http_status: int | None
    error_code: EmbyErrorCode | None
    server_name: str
    version: str


_UNSET = object()


def sanitize_secret_text(value, api_key=""):
    text = str(value or "")
    secret = str(api_key or "")
    if not secret:
        return text
    encoded = urllib.parse.quote_plus(secret)
    return text.replace(secret, "[redacted]").replace(encoded, "[redacted]")


def public_result(result) -> EmbyResult:
    result = result or {}
    return {
        "status": cast(EmbyStatus, str(result.get("status") or "")),
        "message": str(result.get("message") or ""),
        "checked_at": result.get("checked_at"),
        "http_status": result.get("http_status"),
        "error_code": cast(EmbyErrorCode | None, result.get("error_code")),
        "server_name": str(result.get("server_name") or ""),
        "version": str(result.get("version") or ""),
    }


def result(
    status: EmbyStatus,
    message,
    *,
    api_key="",
    http_status=None,
    error_code: EmbyErrorCode | None = None,
    server_name="",
    version="",
) -> EmbyResult:
    return public_result(
        {
            "status": status,
            "message": sanitize_secret_text(message, api_key),
            "checked_at": utc_iso(),
            "http_status": http_status,
            "error_code": error_code,
            "server_name": server_name,
            "version": version,
        }
    )


def endpoint(settings, api_path, params: Mapping[str, Any] | None = None):
    base = str((settings or {}).get("emby_url") or "").strip().rstrip("/")
    api_key = str((settings or {}).get("emby_api_key") or "").strip()
    if not base or not api_key:
        return ""
    api_path = "/" + str(api_path or "").strip("/")
    if base.lower().endswith("/emby"):
        url = f"{base}{api_path}"
    else:
        url = f"{base}/emby{api_path}"
    query = [(str(key), value) for key, value in (params or {}).items() if value is not None]
    if query:
        url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
    return url


def read_response_json(response):
    valid, data = _read_response_json(response)
    return data if valid else None


def _read_response_json(response):
    if not hasattr(response, "read"):
        return False, None
    raw = response.read()
    if not raw:
        return False, None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return True, json.loads(raw)
    except (TypeError, ValueError):
        return False, None


def _request_error(exc, api_key="") -> EmbyResult:
    if isinstance(exc, urllib.error.HTTPError):
        return result(
            "failed",
            f"Emby rejected the request ({getattr(exc, 'code', 'unknown')})",
            api_key=api_key,
            http_status=getattr(exc, "code", None),
            error_code="http_error",
        )
    if isinstance(exc, (TimeoutError, socket.timeout)) or (
        isinstance(exc, urllib.error.URLError)
        and isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout))
    ):
        return result(
            "failed",
            "Emby request timed out",
            api_key=api_key,
            error_code="timeout",
        )
    return result(
        "failed",
        f"Emby connection failed: {sanitize_secret_text(exc, api_key)}",
        api_key=api_key,
        error_code="connection_error",
    )


def _request(
    settings,
    api_path,
    *,
    params=None,
    method="GET",
    json_body=_UNSET,
    body=None,
    content_type=None,
    opener=None,
    timeout=15,
    accept="application/json",
    expect_json=True,
):
    api_key = str((settings or {}).get("emby_api_key") or "").strip()
    url = endpoint(settings, api_path, params=params)
    if not url:
        return None, result(
            "skipped",
            "Emby URL and API key are required",
            api_key=api_key,
            error_code="missing_config",
        )
    if json_body is not _UNSET and body is not None:
        raise ValueError("json_body and body cannot both be provided")
    if json_body is not _UNSET:
        body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        content_type = "application/json"

    headers = {"Accept": accept, "X-Emby-Token": api_key}
    if content_type:
        headers["Content-Type"] = str(content_type)
    request = urllib.request.Request(
        url,
        data=body,
        method=str(method or "GET").upper(),
        headers=headers,
    )
    opener = opener or urllib.request.urlopen
    try:
        with opener(request, timeout=timeout) as response:
            code = getattr(response, "status", None) or getattr(response, "code", 0)
            if expect_json:
                valid, data = _read_response_json(response)
                if not valid:
                    return None, result(
                        "failed",
                        "Emby returned an invalid or empty JSON response",
                        api_key=api_key,
                        http_status=code,
                        error_code="invalid_response",
                    )
            else:
                data = None
    except Exception as exc:
        return None, _request_error(exc, api_key)
    return data, result(
        "success",
        f"Emby request completed ({code})",
        api_key=api_key,
        http_status=code,
    )


def request_json(
    settings,
    api_path,
    *,
    params=None,
    method="GET",
    json_body=_UNSET,
    body=None,
    content_type=None,
    opener=None,
    timeout=15,
    accept="application/json",
) -> tuple[Any, EmbyResult]:
    return _request(
        settings,
        api_path,
        params=params,
        method=method,
        json_body=json_body,
        body=body,
        content_type=content_type,
        opener=opener,
        timeout=timeout,
        accept=accept,
        expect_json=True,
    )


def request_no_content(
    settings,
    api_path,
    *,
    params=None,
    method="POST",
    json_body=_UNSET,
    body=None,
    content_type=None,
    opener=None,
    timeout=15,
    accept="*/*",
) -> EmbyResult:
    _data, request_result = _request(
        settings,
        api_path,
        params=params,
        method=method,
        json_body=json_body,
        body=body,
        content_type=content_type,
        opener=opener,
        timeout=timeout,
        accept=accept,
        expect_json=False,
    )
    return request_result


def request_paged_json(
    settings,
    api_path,
    *,
    params=None,
    page_size=500,
    opener=None,
    timeout=30,
    before_page: Callable[[], None] | None = None,
) -> tuple[list[Any] | None, EmbyResult]:
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = 0
    if page_size < 1:
        raise ValueError("page_size must be a positive integer")

    base_params = {
        key: value
        for key, value in dict(params or {}).items()
        if str(key).lower() not in {"startindex", "limit"}
    }
    collected = []
    start = 0
    expected_total = None
    last_result = result("success", "Emby pagination has no results")
    while True:
        if before_page:
            before_page()
        page_params = {**base_params, "StartIndex": start, "Limit": page_size}
        data, last_result = request_json(
            settings,
            api_path,
            params=page_params,
            opener=opener,
            timeout=timeout,
        )
        if last_result.get("status") != "success":
            return None, last_result
        if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
            return None, result(
                "failed",
                "Emby returned an invalid paged response",
                http_status=last_result.get("http_status"),
                error_code="invalid_response",
            )
        total_value = data.get("TotalRecordCount")
        if isinstance(total_value, bool) or not isinstance(total_value, (int, float)):
            return None, result(
                "failed",
                "Emby returned an invalid total record count",
                http_status=last_result.get("http_status"),
                error_code="invalid_response",
            )
        total = int(total_value)
        if total < 0 or total != total_value:
            return None, result(
                "failed",
                "Emby returned an invalid total record count",
                http_status=last_result.get("http_status"),
                error_code="invalid_response",
            )
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            return None, result(
                "failed",
                "Emby changed the total record count during pagination",
                http_status=last_result.get("http_status"),
                error_code="invalid_response",
            )

        items = data["Items"]
        collected.extend(items)
        if len(collected) == total:
            return collected, last_result
        if len(collected) > total or not items:
            return None, result(
                "failed",
                "Emby returned an incomplete or inconsistent paged response",
                http_status=last_result.get("http_status"),
                error_code="invalid_response",
            )
        start += len(items)
