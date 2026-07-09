import json
import urllib.error
import urllib.parse
import urllib.request

from .progress import utc_iso


def sanitize_secret_text(value, api_key=""):
    text = str(value or "")
    secret = str(api_key or "")
    if not secret:
        return text
    encoded = urllib.parse.quote_plus(secret)
    return text.replace(secret, "[redacted]").replace(encoded, "[redacted]")


def public_result(result):
    result = result or {}
    return {
        "status": str(result.get("status") or ""),
        "message": str(result.get("message") or ""),
        "checked_at": result.get("checked_at"),
        "http_status": result.get("http_status"),
        "server_name": str(result.get("server_name") or ""),
        "version": str(result.get("version") or ""),
    }


def result(status, message, *, api_key="", http_status=None, server_name="", version=""):
    return public_result(
        {
            "status": status,
            "message": sanitize_secret_text(message, api_key),
            "checked_at": utc_iso(),
            "http_status": http_status,
            "server_name": server_name,
            "version": version,
        }
    )


def endpoint(settings, api_path):
    base = str((settings or {}).get("emby_url") or "").strip().rstrip("/")
    api_key = str((settings or {}).get("emby_api_key") or "").strip()
    if not base or not api_key:
        return ""
    api_path = "/" + str(api_path or "").strip("/")
    if base.lower().endswith("/emby"):
        url = f"{base}{api_path}"
    else:
        url = f"{base}/emby{api_path}"
    return f"{url}?{urllib.parse.urlencode({'api_key': api_key})}"


def read_response_json(response):
    if not hasattr(response, "read"):
        return None
    raw = response.read()
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def request_json(
    settings,
    api_path,
    *,
    method="GET",
    body=None,
    opener=None,
    timeout=15,
    accept="application/json",
):
    api_key = str((settings or {}).get("emby_api_key") or "")
    url = endpoint(settings, api_path)
    if not url:
        return None, result(
            "skipped",
            "Emby URL and API key are required",
            api_key=api_key,
        )
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"accept": accept},
    )
    try:
        with opener(request, timeout=timeout) as response:
            code = getattr(response, "status", None) or getattr(response, "code", 0)
            data = read_response_json(response)
    except urllib.error.HTTPError as exc:
        return None, result(
            "failed",
            f"Emby rejected the request ({getattr(exc, 'code', 'unknown')})",
            api_key=api_key,
            http_status=getattr(exc, "code", None),
        )
    except Exception as exc:
        return None, result(
            "failed",
            f"Emby connection failed: {sanitize_secret_text(exc, api_key)}",
            api_key=api_key,
        )
    return data, result("success", f"Emby request completed ({code})", api_key=api_key, http_status=code)
