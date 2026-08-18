"""A local index of Emby library metadata and this user's watch state.

`emby_catalog` already sweeps Emby, but it asks for the bare minimum needed to
match an Emby item to a file on disk: `Path,MediaSources,MediaStreams,
RunTimeTicks`. Everything descriptive -- genres, tags, studios, people -- and
everything personal -- played, play count, favourite -- is returned by the same
endpoint and simply never requested.

This module asks for the rest and keeps it, because one index answers three
questions that otherwise need three implementations:

* **Filtering** on several facets at once ("these two tags *and* this actor").
* **Watch state**, which is just two more columns on the same rows.
* **What the library owner tends to like**, which is those rows grouped
  differently rather than a separate dataset.

Filtering happens locally rather than being handed to Emby. Emby will happily
combine *different* facets, but requiring several tags *simultaneously* is not
something its item filter is built for -- and once the rows are here, "all of
these" is a set operation rather than a query-string negotiation.

The index is personal in a way the rest of `/state` is not: it records what has
been watched and favourited. `system_status` excludes this directory from the
`/state` backup archive for that reason, and says so in the manifest rather than
dropping it silently.
"""

import gzip
import json
import os
import threading

from . import emby_catalog
from . import emby_client
from .config import STATE_ROOT
from .progress import utc_iso


INDEX_ROOT = os.path.join(STATE_ROOT, "emby-index")
INDEX_PATH = os.path.join(INDEX_ROOT, "library-index.json.gz")
SCHEMA_VERSION = 1

# Everything the catalog asks for, plus the descriptive and personal fields it
# does not. `UserData` is what carries played/favourite, and only arrives when
# the request is made in a user's context.
INDEX_FIELDS = (
    "Path,Genres,Studios,Tags,People,ProviderIds,ProductionYear,"
    "CommunityRating,OfficialRating,RunTimeTicks,DateCreated,UserData"
)
INDEX_ITEM_TYPES = "Movie,Episode,Video"
TICKS_PER_SECOND = 10_000_000

_index_lock = threading.Lock()


class EmbyIndexError(RuntimeError):
    """The index could not be built or read."""


# --- shaping ----------------------------------------------------------------


def _names(values, key="Name"):
    """Emby returns some facets as strings and some as objects."""
    out = []
    for value in values or []:
        if isinstance(value, str):
            name = value.strip()
        elif isinstance(value, dict):
            name = str(value.get(key) or "").strip()
        else:
            continue
        if name:
            out.append(name)
    return out


def _people(values):
    people = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        name = str(value.get("Name") or "").strip()
        if not name:
            continue
        people.append(
            {
                "name": name,
                "type": str(value.get("Type") or "").strip(),
                "role": str(value.get("Role") or "").strip(),
            }
        )
    return people


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def public_item(raw, path_lookup=None):
    """One library row: what it is, and what this user has done with it."""
    user_data = raw.get("UserData") if isinstance(raw.get("UserData"), dict) else {}
    ticks = _number(raw.get("RunTimeTicks"))
    emby_path = str(raw.get("Path") or "").strip()
    return {
        "id": str(raw.get("Id") or ""),
        "name": str(raw.get("Name") or "").strip(),
        "type": str(raw.get("Type") or "").strip(),
        "emby_path": emby_path,
        # The path as this container sees it, via the configured mappings.
        "local_path": (path_lookup or {}).get(emby_path, ""),
        "year": _number(raw.get("ProductionYear")),
        "community_rating": _number(raw.get("CommunityRating")),
        "official_rating": str(raw.get("OfficialRating") or "").strip(),
        "runtime_seconds": round(ticks / TICKS_PER_SECOND, 3) if ticks else None,
        "created_at": raw.get("DateCreated"),
        "genres": _names(raw.get("Genres")),
        "tags": _names(raw.get("Tags")),
        "studios": _names(raw.get("Studios")),
        "people": _people(raw.get("People")),
        # Personal state. Absent rather than guessed when Emby did not send it.
        "played": bool(user_data.get("Played")) if user_data else None,
        "play_count": int(user_data.get("PlayCount") or 0) if user_data else None,
        "is_favorite": bool(user_data.get("IsFavorite")) if user_data else None,
        "last_played_at": user_data.get("LastPlayedDate") if user_data else None,
    }


# --- fetching ---------------------------------------------------------------


def list_users(settings=None, opener=None):
    """The Emby accounts available, so one can be chosen for watch state."""
    settings = settings or {}
    data, outcome = emby_client.request_json(settings, "/Users", opener=opener, timeout=20)
    if outcome.get("status") != "success":
        return None, outcome
    if not isinstance(data, list):
        return None, emby_client.result(
            "failed", "Emby returned an unexpected user list", error_code="invalid_response"
        )
    users = [
        {"id": str(item.get("Id") or ""), "name": str(item.get("Name") or "").strip()}
        for item in data
        if isinstance(item, dict) and item.get("Id")
    ]
    return users, outcome


def _path_lookup(settings, opener=None):
    """Emby path -> local path, reusing the mapping the app already has."""
    try:
        catalog, _summary = emby_catalog.load_catalog(settings, opener=opener)
    except Exception:
        return {}
    lookup = {}
    for entry in (catalog or {}).get("items") or []:
        if not isinstance(entry, dict):
            continue
        emby_path = str(entry.get("path") or entry.get("emby_path") or "").strip()
        local_path = str(entry.get("local_path") or "").strip()
        if emby_path and local_path:
            lookup[emby_path] = local_path
    return lookup


def fetch_items(settings=None, opener=None, before_page=None):
    """Sweep the library in the chosen user's context.

    Without a user id Emby returns the items but no `UserData`, so the rows come
    back with their personal columns empty rather than wrong.
    """
    settings = settings or {}
    user_id = str(settings.get("emby_user_id") or "").strip()
    api_path = f"/Users/{user_id}/Items" if user_id else "/Items"
    items, outcome = emby_client.request_paged_json(
        settings,
        api_path,
        params={
            "Recursive": "true",
            "IncludeItemTypes": INDEX_ITEM_TYPES,
            "Fields": INDEX_FIELDS,
        },
        opener=opener,
        timeout=60,
        before_page=before_page,
    )
    return items, outcome


# --- storage ----------------------------------------------------------------


def _write_index(payload):
    os.makedirs(INDEX_ROOT, exist_ok=True)
    tmp_path = f"{INDEX_PATH}.{os.getpid()}.tmp"
    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_path, INDEX_PATH)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def load_index():
    """The stored index, or None when the library has never been swept."""
    try:
        with gzip.open(INDEX_PATH, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if not isinstance(payload.get("items"), list):
        return None
    return payload


def refresh(settings=None, opener=None, before_page=None, now=None):
    """Rebuild the index from Emby and persist it."""
    settings = settings or {}
    if not settings.get("emby_url") or not settings.get("emby_api_key"):
        return None, emby_client.result(
            "not_configured",
            "Emby is not configured, so the library index cannot be built.",
            error_code="not_configured",
        )

    items, outcome = fetch_items(settings, opener=opener, before_page=before_page)
    if outcome.get("status") != "success" or items is None:
        return None, outcome

    lookup = _path_lookup(settings, opener=opener)
    rows = [public_item(raw, lookup) for raw in items if isinstance(raw, dict)]
    user_id = str(settings.get("emby_user_id") or "").strip()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "built_at": now or utc_iso(),
        "user_id": user_id,
        # Stated rather than inferred: without a user, the personal columns are
        # empty because none were requested, not because nothing was watched.
        "has_watch_state": bool(user_id),
        "item_count": len(rows),
        "items": rows,
    }
    with _index_lock:
        _write_index(payload)
    return payload, outcome


# --- querying ---------------------------------------------------------------


def _matches_facet(values, wanted, match_all):
    if not wanted:
        return True
    present = {str(value).casefold() for value in values or []}
    wanted = {str(value).casefold() for value in wanted if str(value).strip()}
    if not wanted:
        return True
    return wanted <= present if match_all else bool(wanted & present)


def _person_names(item):
    return [person.get("name", "") for person in item.get("people") or []]


def search(
    index=None,
    *,
    genres=None,
    tags=None,
    studios=None,
    people=None,
    played=None,
    favorite=None,
    query="",
    match_all=True,
    limit=None,
):
    """Filter the index on any combination of facets.

    `match_all` governs the semantics *within* a facet: True means an item must
    carry every tag asked for, which is the case Emby's own filter does not
    cover and the reason this index exists. Facets are always combined with AND
    -- asking for a genre and an actor means both.
    """
    payload = index if index is not None else load_index()
    if not payload:
        return []
    needle = str(query or "").strip().casefold()
    results = []
    for item in payload.get("items") or []:
        if not _matches_facet(item.get("genres"), genres, match_all):
            continue
        if not _matches_facet(item.get("tags"), tags, match_all):
            continue
        if not _matches_facet(item.get("studios"), studios, match_all):
            continue
        if not _matches_facet(_person_names(item), people, match_all):
            continue
        if played is not None and bool(item.get("played")) != bool(played):
            continue
        if favorite is not None and bool(item.get("is_favorite")) != bool(favorite):
            continue
        if needle and needle not in str(item.get("name") or "").casefold():
            continue
        results.append(item)
        if limit and len(results) >= int(limit):
            break
    return results


def facets(index=None, *, top=None):
    """Every value available to filter on, with counts.

    This is what a filter UI needs to offer choices instead of asking the
    operator to remember exact spellings.
    """
    payload = index if index is not None else load_index()
    if not payload:
        return {"genres": [], "tags": [], "studios": [], "people": []}

    counters = {"genres": {}, "tags": {}, "studios": {}, "people": {}}
    for item in payload.get("items") or []:
        for key in ("genres", "tags", "studios"):
            for value in item.get(key) or []:
                counters[key][value] = counters[key].get(value, 0) + 1
        for name in _person_names(item):
            if name:
                counters["people"][name] = counters["people"].get(name, 0) + 1

    out = {}
    for key, counts in counters.items():
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
        if top:
            ranked = ranked[: int(top)]
        out[key] = [{"value": value, "count": count} for value, count in ranked]
    return out


def summary(index=None):
    """A short description of what the index holds, for status display."""
    payload = index if index is not None else load_index()
    if not payload:
        return {
            "status": "missing",
            "built_at": None,
            "item_count": 0,
            "has_watch_state": False,
            "played_count": 0,
            "favorite_count": 0,
        }
    items = payload.get("items") or []
    return {
        "status": "ready",
        "built_at": payload.get("built_at"),
        "item_count": len(items),
        "has_watch_state": bool(payload.get("has_watch_state")),
        "played_count": sum(1 for item in items if item.get("played")),
        "favorite_count": sum(1 for item in items if item.get("is_favorite")),
    }
