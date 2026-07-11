import json
import urllib.parse

import pytest

from app import emby_catalog


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.code = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _settings(**overrides):
    settings = {
        "emby_url": "http://emby:8096",
        "emby_api_key": "secret",
        "emby_path_mappings": [],
    }
    settings.update(overrides)
    return settings


def _catalog_opener(items, captured=None):
    def opener(request, timeout=30):
        if captured is not None:
            captured.append(request)
        if request.full_url.endswith("/System/Info"):
            return FakeResponse({"Id": "server-1", "ServerName": "Emby"})
        return FakeResponse({"Items": items, "TotalRecordCount": len(items)})

    return opener


def test_catalog_requests_documented_fields_and_indexes_media_sources():
    emby_catalog.clear_cache()
    captured = []
    items = [
        {
            "Id": "m1",
            "Name": "Movie",
            "Type": "Movie",
            "Path": "/media/Movie.mkv",
            "MediaSources": [{"Path": "D:\\Movies\\Movie.mkv"}],
        }
    ]

    catalog, summary = emby_catalog.load_catalog(
        _settings(), opener=_catalog_opener(items, captured)
    )

    request = captured[-1]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
    assert query["Recursive"] == ["true"]
    assert query["Fields"] == ["Path,MediaSources"]
    assert query["IncludeItemTypes"] == ["Movie,Episode,Video,Series,Season,BoxSet"]
    assert emby_catalog.match_path(catalog, "/media/Movie.mkv")["emby_item_id"] == "m1"
    assert emby_catalog.match_path(catalog, "d:/movies/movie.mkv")["emby_item_id"] == "m1"
    assert summary["server_id"] == "server-1"
    assert summary["catalog_item_count"] == 1
    assert request.get_header("X-emby-token") == "secret"
    assert "secret" not in request.full_url


def test_explicit_path_mapping_supports_posix_windows_and_longest_prefix(tmp_path):
    catalog = emby_catalog._build_catalog(
        [
            {"Id": "one", "Type": "Movie", "Name": "One", "Path": "/media/One.mkv"},
            {"Id": "two", "Type": "Movie", "Name": "Two", "Path": "Z:\\TV\\Show\\Two.mkv"},
        ],
        {"Id": "server"},
        "fingerprint",
    )
    mappings = [
        {"emby_prefix": "/media", "local_prefix": "/library"},
        {"emby_prefix": "/media/movies", "local_prefix": "/library/movies"},
        {"emby_prefix": "Z:\\TV", "local_prefix": "/library/tv"},
    ]

    assert emby_catalog.match_path(catalog, "/library/One.mkv", mappings)["emby_item_id"] == "one"
    assert emby_catalog.match_path(catalog, "/library/tv/Show/Two.mkv", mappings)["emby_item_id"] == "two"
    assert emby_catalog.mapped_emby_paths("/library/movies/Film.mkv", mappings) == [
        "/media/movies/film.mkv"
    ]


def test_catalog_deduplicates_same_id_and_marks_distinct_id_collisions_ambiguous():
    catalog = emby_catalog._build_catalog(
        [
            {"Id": "same", "Path": "/media/A.mkv", "MediaSources": [{"Path": "/MEDIA/a.mkv"}]},
            {"Id": "other", "Path": "/media/a.mkv"},
        ],
        {},
        "fingerprint",
    )

    match = emby_catalog.match_path(catalog, "/media/A.mkv")

    assert match["emby_match_status"] == "ambiguous"
    assert match["emby_item_id"] == ""


def test_enrichment_is_nonfatal_when_not_configured_or_unavailable():
    records = [{"path": "/library/A.mkv"}]
    summary = emby_catalog.enrich_records(records, {}, lambda item: item["path"])

    assert summary["status"] == "not_configured"
    assert summary["unmatched_count"] == 1
    assert records[0]["emby_match_status"] == "unmatched"

    emby_catalog.clear_cache()
    records = [{"path": "/library/A.mkv"}]
    summary = emby_catalog.enrich_records(
        records,
        _settings(),
        lambda item: item["path"],
        opener=lambda request, timeout: (_ for _ in ()).throw(OSError("offline")),
    )
    assert summary["status"] == "unavailable"
    assert summary["unmatched_count"] == 1


def test_catalog_success_and_failure_cache_and_force_refresh():
    emby_catalog.clear_cache()
    calls = 0

    def opener(request, timeout=30):
        nonlocal calls
        calls += 1
        if request.full_url.endswith("/System/Info"):
            return FakeResponse({"Id": "server"})
        return FakeResponse({"Items": [], "TotalRecordCount": 0})

    emby_catalog.load_catalog(_settings(), opener=opener, now=10)
    emby_catalog.load_catalog(_settings(), opener=opener, now=11)
    assert calls == 2
    emby_catalog.load_catalog(_settings(), opener=opener, now=11, force=True)
    assert calls == 4

    emby_catalog.clear_cache()
    failures = 0

    def failing(request, timeout=30):
        nonlocal failures
        failures += 1
        raise OSError("offline")

    emby_catalog.load_catalog(_settings(), opener=failing, now=20)
    emby_catalog.load_catalog(_settings(), opener=failing, now=21)
    assert failures == 1


def test_catalog_propagates_cancellation_and_public_summary_detects_stale_settings():
    class Cancelled(Exception):
        pass

    emby_catalog.clear_cache()
    checks = 0

    def before_page():
        nonlocal checks
        checks += 1
        raise Cancelled()

    with pytest.raises(Cancelled):
        emby_catalog.load_catalog(
            _settings(),
            opener=_catalog_opener([]),
            before_page=before_page,
        )

    summary = emby_catalog.known_matches_summary(_settings(), 2)
    public = emby_catalog.public_summary(summary, _settings(emby_url="http://other:8096"))
    assert public["status"] == "stale"
    assert "rescan" in public["message"].lower()
    assert "_configuration_fingerprint" not in public


def test_mapped_local_paths_uses_longest_emby_prefix():
    mappings = [
        {"emby_prefix": "/media", "local_prefix": "/library"},
        {"emby_prefix": "/media/tv", "local_prefix": "/library/shows"},
    ]

    assert emby_catalog.mapped_local_paths("/media/tv/Show/Episode.mkv", mappings) == [
        "/library/shows/show/episode.mkv"
    ]
