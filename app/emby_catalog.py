import hashlib
import json
import ntpath
import posixpath
import re
import threading
import time

from . import emby_client
from .progress import utc_iso


SUCCESS_CACHE_SECONDS = 300
FAILURE_CACHE_SECONDS = 30
CATALOG_ITEM_TYPES = "Movie,Episode,Video,Series,Season,BoxSet"
CATALOG_FIELDS = "Path,MediaSources,MediaStreams"

_cache = {}
_cache_lock = threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def normalize_path(value):
    value = str(value or "").strip()
    if not value:
        return ""
    value = value.replace("\\", "/")
    is_unc = value.startswith("//")
    if re.match(r"^[A-Za-z]:/", value):
        value = ntpath.normpath(value).replace("\\", "/")
    else:
        value = posixpath.normpath(value)
    if is_unc and not value.startswith("//"):
        value = "/" + value
    if value != "/":
        value = value.rstrip("/")
    return value.casefold()


def configuration_fingerprint(settings):
    settings = settings or {}
    payload = {
        "url": str(settings.get("emby_url") or "").strip().rstrip("/").casefold(),
        "key": hashlib.sha256(str(settings.get("emby_api_key") or "").encode("utf-8")).hexdigest(),
        "mappings": sorted([
            {
                "emby_prefix": normalize_path(item.get("emby_prefix")),
                "local_prefix": normalize_path(item.get("local_prefix")),
            }
            for item in (settings.get("emby_path_mappings") or [])
            if isinstance(item, dict)
        ], key=lambda item: (item["emby_prefix"], item["local_prefix"])),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _base_summary(status, message, *, fingerprint="", checked_at=None, server_id="", catalog_count=0):
    return {
        "status": status,
        "checked_at": checked_at,
        "server_id": str(server_id or ""),
        "catalog_item_count": int(catalog_count or 0),
        "total_count": 0,
        "matched_count": 0,
        "unmatched_count": 0,
        "ambiguous_count": 0,
        "message": str(message or ""),
        "_configuration_fingerprint": fingerprint,
    }


def not_checked_summary(message="Emby identity was not checked; rescan to add item IDs."):
    return _base_summary("not_checked", message)


def public_summary(summary, settings=None):
    if not isinstance(summary, dict):
        summary = not_checked_summary()
    public = {key: value for key, value in summary.items() if not str(key).startswith("_")}
    stored = summary.get("_configuration_fingerprint")
    if settings is not None and stored and stored != configuration_fingerprint(settings):
        public["status"] = "stale"
        public["message"] = "Emby settings changed after this scan; rescan to refresh item IDs."
    return public


def _catalog_entry(item):
    return {
        "emby_item_id": str(item.get("Id") or item.get("id") or ""),
        "emby_item_type": str(item.get("Type") or item.get("type") or ""),
        "emby_item_name": str(item.get("Name") or item.get("name") or ""),
    }


def _subtitle_stream(stream, media_source_id=""):
    if not isinstance(stream, dict) or str(stream.get("Type") or "").lower() != "subtitle":
        return None
    path = str(stream.get("Path") or "")
    language = str(stream.get("Language") or "").strip().lower().replace("_", "-")
    if language in {"und", "unk", "unknown"}:
        language = ""
    return {
        "media_source_id": str(media_source_id or ""),
        "index": int(stream.get("Index") or 0),
        "language_code": language,
        "display_language": str(stream.get("DisplayLanguage") or ""),
        "codec": str(stream.get("Codec") or ""),
        "display_title": str(stream.get("DisplayTitle") or stream.get("Title") or ""),
        "is_external": bool(stream.get("IsExternal")),
        "is_text": bool(stream.get("IsTextSubtitleStream")),
        "is_default": bool(stream.get("IsDefault")),
        "is_forced": bool(stream.get("IsForced")),
        "is_hearing_impaired": bool(stream.get("IsHearingImpaired")),
        "delivery_method": str(stream.get("DeliveryMethod") or ""),
        "_path": path,
        "_normalized_path": normalize_path(path),
    }


def public_subtitle_stream(stream):
    return {key: value for key, value in (stream or {}).items() if not str(key).startswith("_")}


def _stream_source(path, streams, media_source_id="", present=False):
    sanitized = []
    for raw in streams or []:
        value = _subtitle_stream(raw, media_source_id)
        if value:
            sanitized.append(value)
    return {
        "media_source_id": str(media_source_id or ""),
        "path": str(path or ""),
        "normalized_path": normalize_path(path),
        "streams_present": bool(present),
        "subtitle_streams": sanitized,
    }


def _build_catalog(items, system_info, fingerprint):
    entries = {}
    items_by_id = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        entry = _catalog_entry(item)
        item_id = entry["emby_item_id"]
        if not item_id:
            continue
        sources = []
        if item.get("Path") or "MediaStreams" in item:
            sources.append(
                _stream_source(
                    item.get("Path"),
                    item.get("MediaStreams") or [],
                    present="MediaStreams" in item,
                )
            )
        for source in item.get("MediaSources") or []:
            if isinstance(source, dict):
                sources.append(
                    _stream_source(
                        source.get("Path"),
                        source.get("MediaStreams") or [],
                        source.get("Id"),
                        present="MediaStreams" in source,
                    )
                )
        entry["_stream_sources"] = sources
        items_by_id[item_id] = entry
        paths = [item.get("Path")]
        for source in item.get("MediaSources") or []:
            if isinstance(source, dict):
                paths.append(source.get("Path"))
        for path in paths:
            normalized = normalize_path(path)
            if normalized:
                entries.setdefault(normalized, set()).add(item_id)
    return {
        "entries": entries,
        "items_by_id": items_by_id,
        "server_id": str(
            (system_info or {}).get("Id")
            or (system_info or {}).get("ServerId")
            or (system_info or {}).get("ServerName")
            or ""
        ),
        "item_count": len(items_by_id),
        "configuration_fingerprint": fingerprint,
    }


def subtitle_streams_for_path(catalog, item_id, local_path, mappings=None):
    if not catalog or not item_id:
        return {"status": "not_checked", "streams": []}
    entry = (catalog.get("items_by_id") or {}).get(str(item_id)) or {}
    sources = entry.get("_stream_sources") or []
    local = normalize_path(local_path)
    mapped = mapped_emby_paths(local_path, mappings)
    candidates = {path for path in [local, *mapped] if path}
    matches = [source for source in sources if source.get("normalized_path") in candidates]
    identified = [source for source in matches if source.get("media_source_id")]
    if identified:
        top_level = [source for source in matches if not source.get("media_source_id")]
        matches = (
            identified
            if any(source.get("streams_present") for source in identified) or not any(source.get("streams_present") for source in top_level)
            else top_level
        )
    source_ids = {source.get("media_source_id") or source.get("normalized_path") for source in matches}
    if len(mapped) > 1 or len(source_ids) > 1:
        return {"status": "ambiguous", "streams": []}
    if not matches:
        return {"status": "partial", "streams": []}
    present = any(source.get("streams_present") for source in matches)
    streams = []
    seen = set()
    for source in matches:
        for stream in source.get("subtitle_streams") or []:
            key = (stream.get("media_source_id"), stream.get("index"), stream.get("_normalized_path"))
            if key in seen:
                continue
            seen.add(key)
            streams.append(dict(stream))
    return {"status": "complete" if present else "partial", "streams": streams}


def load_catalog(settings, *, force=False, opener=None, before_page=None, now=None):
    settings = settings or {}
    fingerprint = configuration_fingerprint(settings)
    if not settings.get("emby_url") or not settings.get("emby_api_key"):
        return None, _base_summary(
            "not_configured",
            "Configure an Emby URL and API key to add item IDs.",
            fingerprint=fingerprint,
        )
    now = time.monotonic() if now is None else float(now)
    if not force:
        with _cache_lock:
            cached = _cache.get(fingerprint)
            if cached and cached[0] > now:
                return cached[1], dict(cached[2])

    info, info_result = emby_client.request_json(
        settings, "/System/Info", opener=opener, timeout=15
    )
    if info_result.get("status") != "success" or not isinstance(info, dict):
        message = info_result.get("message") or "Emby system information is unavailable."
        if info_result.get("status") == "success" and not isinstance(info, dict):
            message = "Emby returned invalid system information."
        summary = _base_summary(
            "unavailable",
            message,
            fingerprint=fingerprint,
            checked_at=info_result.get("checked_at") or utc_iso(),
        )
        with _cache_lock:
            _cache[fingerprint] = (now + FAILURE_CACHE_SECONDS, None, summary)
        return None, summary

    items, items_result = emby_client.request_paged_json(
        settings,
        "/Items",
        params={
            "Recursive": "true",
            "IncludeItemTypes": CATALOG_ITEM_TYPES,
            "Fields": CATALOG_FIELDS,
        },
        opener=opener,
        timeout=30,
        before_page=before_page,
    )
    if items_result.get("status") != "success" or items is None:
        summary = _base_summary(
            "unavailable",
            items_result.get("message") or "Emby catalog is unavailable.",
            fingerprint=fingerprint,
            checked_at=items_result.get("checked_at") or utc_iso(),
            server_id=info.get("Id") or info.get("ServerId") or info.get("ServerName"),
        )
        with _cache_lock:
            _cache[fingerprint] = (now + FAILURE_CACHE_SECONDS, None, summary)
        return None, summary

    catalog = _build_catalog(items, info, fingerprint)
    summary = _base_summary(
        "complete",
        "Emby catalog loaded.",
        fingerprint=fingerprint,
        checked_at=items_result.get("checked_at") or utc_iso(),
        server_id=catalog["server_id"],
        catalog_count=catalog["item_count"],
    )
    with _cache_lock:
        _cache[fingerprint] = (now + SUCCESS_CACHE_SECONDS, catalog, summary)
    return catalog, summary


def _path_has_prefix(path, prefix):
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def mapped_emby_paths(local_path, mappings):
    local = normalize_path(local_path)
    candidates = []
    for mapping in mappings or []:
        if not isinstance(mapping, dict):
            continue
        local_prefix = normalize_path(mapping.get("local_prefix"))
        emby_prefix = normalize_path(mapping.get("emby_prefix"))
        if not local_prefix or not emby_prefix or not _path_has_prefix(local, local_prefix):
            continue
        remainder = local[len(local_prefix):].lstrip("/")
        mapped = emby_prefix + (("/" + remainder) if remainder else "")
        candidates.append((len(emby_prefix), len(local_prefix), mapped))
    if not candidates:
        return []
    best = max((local_length, emby_length) for emby_length, local_length, _path in candidates)
    return sorted({path for emby_length, local_length, path in candidates if (local_length, emby_length) == best})


def mapped_local_paths(emby_path, mappings):
    emby = normalize_path(emby_path)
    candidates = []
    for mapping in mappings or []:
        if not isinstance(mapping, dict):
            continue
        emby_prefix = normalize_path(mapping.get("emby_prefix"))
        local_prefix = str(mapping.get("local_prefix") or "").rstrip("/\\")
        if not emby_prefix or not local_prefix or not _path_has_prefix(emby, emby_prefix):
            continue
        remainder = emby[len(emby_prefix):].lstrip("/")
        mapped = local_prefix + (("/" + remainder) if remainder else "")
        candidates.append((len(emby_prefix), mapped))
    if not candidates:
        return []
    best = max(length for length, _path in candidates)
    return sorted({path for length, path in candidates if length == best})


def known_matches_summary(settings, total_count, *, catalog_item_count=0, server_id=""):
    total_count = int(total_count or 0)
    summary = _base_summary(
        "complete",
        f"Matched {total_count} of {total_count} records to Emby.",
        fingerprint=configuration_fingerprint(settings),
        checked_at=utc_iso(),
        server_id=server_id,
        catalog_count=catalog_item_count,
    )
    summary.update(total_count=total_count, matched_count=total_count)
    return summary


def match_path(catalog, local_path, mappings=None):
    if not catalog:
        return {
            "emby_item_id": "",
            "emby_item_type": "",
            "emby_item_name": "",
            "emby_match_status": "unmatched",
        }
    candidate_paths = [normalize_path(local_path)]
    exact_ids = set(catalog.get("entries", {}).get(candidate_paths[0], set())) if candidate_paths[0] else set()
    ids = exact_ids
    if not ids:
        candidate_paths = mapped_emby_paths(local_path, mappings)
        ids = set()
        for candidate in candidate_paths:
            ids.update(catalog.get("entries", {}).get(candidate, set()))
    if len(ids) != 1:
        return {
            "emby_item_id": "",
            "emby_item_type": "",
            "emby_item_name": "",
            "emby_match_status": "ambiguous" if len(ids) > 1 else "unmatched",
        }
    item_id = next(iter(ids))
    entry = {
        key: value
        for key, value in dict((catalog.get("items_by_id") or {}).get(item_id) or {}).items()
        if not str(key).startswith("_")
    }
    return {**entry, "emby_match_status": "matched"}


def match_paths(catalog, paths, mappings=None):
    matches = [match_path(catalog, path, mappings) for path in paths if path]
    matched_ids = {item["emby_item_id"] for item in matches if item.get("emby_match_status") == "matched"}
    if any(item.get("emby_match_status") == "ambiguous" for item in matches) or len(matched_ids) > 1:
        return {
            "emby_item_id": "",
            "emby_item_type": "",
            "emby_item_name": "",
            "emby_match_status": "ambiguous",
        }
    if len(matched_ids) == 1:
        return next(item for item in matches if item.get("emby_item_id") in matched_ids)
    return {
        "emby_item_id": "",
        "emby_item_type": "",
        "emby_item_name": "",
        "emby_match_status": "unmatched",
    }


def enrich_records(records, settings, path_getter, *, opener=None, before_page=None, force=False):
    catalog, base = load_catalog(
        settings,
        force=force,
        opener=opener,
        before_page=before_page,
    )
    mappings = (settings or {}).get("emby_path_mappings") or []
    counts = {"matched": 0, "unmatched": 0, "ambiguous": 0}
    for record in records or []:
        raw_paths = path_getter(record)
        paths = raw_paths if isinstance(raw_paths, (list, tuple, set)) else [raw_paths]
        match = match_paths(catalog, paths, mappings)
        record.update(match)
        counts[match["emby_match_status"]] += 1
    summary = dict(base)
    summary.update(
        {
            "total_count": sum(counts.values()),
            "matched_count": counts["matched"],
            "unmatched_count": counts["unmatched"],
            "ambiguous_count": counts["ambiguous"],
        }
    )
    if summary["status"] == "complete" and summary["total_count"]:
        summary["status"] = "complete" if counts["matched"] == summary["total_count"] else "partial"
        summary["message"] = (
            f"Matched {counts['matched']} of {summary['total_count']} records to Emby"
            + (f"; {counts['ambiguous']} ambiguous" if counts["ambiguous"] else "")
            + "."
        )
    return summary
