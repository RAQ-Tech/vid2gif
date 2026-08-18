"""The library index: what it asks Emby for, and what it does with the answer."""

import json

import pytest

from app import emby_client, emby_library_index, system_status


def _settings(**overrides):
    base = {"emby_url": "http://emby:8096", "emby_api_key": "key", "emby_user_id": "user-1"}
    base.update(overrides)
    return base


def _raw_item(name, **overrides):
    item = {
        "Id": f"id-{name}",
        "Name": name,
        "Type": "Movie",
        "Path": f"/media/{name}.mkv",
        "ProductionYear": 2021,
        "CommunityRating": 7.5,
        "OfficialRating": "R",
        "RunTimeTicks": 36_000_000_000,  # one hour
        "Genres": ["Action", "Thriller"],
        "Tags": ["4k", "hdr"],
        "Studios": [{"Name": "Some Studio"}],
        "People": [{"Name": "Ada Lovelace", "Type": "Actor", "Role": "Herself"}],
        "UserData": {"Played": True, "PlayCount": 3, "IsFavorite": True, "LastPlayedDate": "2026-01-01T00:00:00Z"},
    }
    item.update(overrides)
    return item


@pytest.fixture
def index_root(tmp_path, monkeypatch):
    root = tmp_path / "emby-index"
    monkeypatch.setattr(emby_library_index, "INDEX_ROOT", str(root))
    monkeypatch.setattr(emby_library_index, "INDEX_PATH", str(root / "library-index.json.gz"))
    # The path mapping is exercised separately; keep the sweep self-contained.
    monkeypatch.setattr(emby_library_index, "_path_lookup", lambda *a, **k: {})
    return root


def _stub_fetch(monkeypatch, items, status="success"):
    outcome = emby_client.result(status, "ok" if status == "success" else "nope")
    monkeypatch.setattr(
        emby_library_index,
        "fetch_items",
        lambda *a, **k: (items if status == "success" else None, outcome),
    )


# --- what it asks Emby for --------------------------------------------------


def test_the_sweep_requests_the_fields_the_catalog_does_not(monkeypatch):
    """The whole point: genres, tags, studios, people and watch state."""
    captured = {}

    def fake_paged(settings, api_path, *, params=None, **kwargs):
        captured["path"] = api_path
        captured["params"] = params
        return [], emby_client.result("success", "ok")

    monkeypatch.setattr(emby_client, "request_paged_json", fake_paged)

    emby_library_index.fetch_items(_settings())

    fields = captured["params"]["Fields"]
    for field in ("Genres", "Tags", "Studios", "People", "UserData"):
        assert field in fields, f"{field} must be requested or it is never available"
    assert captured["params"]["Recursive"] == "true"


def test_watch_state_requires_a_user_context(monkeypatch):
    """UserData only comes back when the request is made as a user.

    Without one the sweep still works; it just cannot report played/favourite.
    """
    seen = []

    def fake_paged(settings, api_path, **kwargs):
        seen.append(api_path)
        return [], emby_client.result("success", "ok")

    monkeypatch.setattr(emby_client, "request_paged_json", fake_paged)

    emby_library_index.fetch_items(_settings(emby_user_id="user-42"))
    emby_library_index.fetch_items(_settings(emby_user_id=""))

    assert seen[0] == "/Users/user-42/Items"
    assert seen[1] == "/Items", "without a user it falls back rather than failing"


def test_a_library_swept_without_a_user_says_it_has_no_watch_state(index_root, monkeypatch):
    _stub_fetch(monkeypatch, [_raw_item("Solo", UserData=None)])

    payload, outcome = emby_library_index.refresh(_settings(emby_user_id=""))

    assert outcome["status"] == "success"
    assert payload["has_watch_state"] is False
    # Absent, not False -- nothing was watched as far as we can tell, and
    # claiming otherwise would make an empty index look like a real answer.
    assert payload["items"][0]["played"] is None
    assert payload["items"][0]["is_favorite"] is None


def test_refusing_to_build_without_emby_configured(index_root):
    payload, outcome = emby_library_index.refresh({"emby_url": "", "emby_api_key": ""})

    assert payload is None
    assert outcome["status"] == "not_configured"


# --- shaping ----------------------------------------------------------------


def test_an_item_is_reduced_to_the_columns_that_matter(index_root):
    item = emby_library_index.public_item(_raw_item("Movie"))

    assert item["name"] == "Movie"
    assert item["genres"] == ["Action", "Thriller"]
    assert item["tags"] == ["4k", "hdr"]
    assert item["studios"] == ["Some Studio"], "studios arrive as objects, not strings"
    assert item["people"][0]["name"] == "Ada Lovelace"
    assert item["people"][0]["type"] == "Actor"
    assert item["runtime_seconds"] == 3600
    assert item["played"] is True
    assert item["play_count"] == 3
    assert item["is_favorite"] is True


def test_malformed_facets_are_dropped_rather_than_crashing(index_root):
    item = emby_library_index.public_item(
        _raw_item(
            "Odd",
            Genres=["Action", "", None, 42, {"Name": "Drama"}],
            People=["not an object", {"NoName": True}, {"Name": "  "}],
            RunTimeTicks="not a number",
        )
    )

    assert item["genres"] == ["Action", "Drama"]
    assert item["people"] == []
    assert item["runtime_seconds"] is None


# --- filtering --------------------------------------------------------------


@pytest.fixture
def library(index_root, monkeypatch):
    items = [
        _raw_item("Both Tags", Tags=["4k", "hdr"], Genres=["Action"]),
        _raw_item("One Tag", Tags=["4k"], Genres=["Action"]),
        _raw_item(
            "Other Actor",
            Tags=["4k", "hdr"],
            Genres=["Drama"],
            People=[{"Name": "Grace Hopper", "Type": "Actor"}],
        ),
        # Deliberately shares no facet with the rows above, so the filter tests
        # and the watch-state tests cannot pass each other by accident.
        _raw_item(
            "Unwatched",
            Tags=["sd"],
            Genres=["Comedy"],
            People=[{"Name": "Katherine Johnson", "Type": "Actor"}],
            UserData={"Played": False, "PlayCount": 0, "IsFavorite": False},
        ),
    ]
    _stub_fetch(monkeypatch, items)
    payload, _outcome = emby_library_index.refresh(_settings())
    return payload


def test_requiring_several_tags_at_once(library):
    """The case Emby's own filter does not cover, and the reason for the index."""
    both = emby_library_index.search(library, tags=["4k", "hdr"])
    either = emby_library_index.search(library, tags=["4k", "hdr"], match_all=False)

    assert {item["name"] for item in both} == {"Both Tags", "Other Actor"}
    assert len(either) > len(both), "any-of is a looser filter than all-of"


def test_different_facets_combine_with_and(library):
    """Two tags and an actor and a genre -- the query that started this."""
    results = emby_library_index.search(
        library,
        tags=["4k", "hdr"],
        genres=["Action"],
        people=["Ada Lovelace"],
    )

    assert [item["name"] for item in results] == ["Both Tags"]


def test_filters_ignore_case(library):
    assert emby_library_index.search(library, tags=["4K", "HDR"], genres=["action"])


def test_watch_state_can_be_filtered(library):
    played = emby_library_index.search(library, played=True)
    unplayed = emby_library_index.search(library, played=False)
    favorites = emby_library_index.search(library, favorite=True)

    assert "Unwatched" not in {item["name"] for item in played}
    assert [item["name"] for item in unplayed] == ["Unwatched"]
    assert "Unwatched" not in {item["name"] for item in favorites}


def test_an_empty_filter_returns_everything_and_a_limit_truncates(library):
    assert len(emby_library_index.search(library)) == 4
    assert len(emby_library_index.search(library, limit=2)) == 2


def test_searching_a_library_that_was_never_swept_is_empty_not_an_error(index_root):
    assert emby_library_index.search() == []
    assert emby_library_index.facets()["tags"] == []


# --- facets and summary -----------------------------------------------------


def test_facets_list_what_can_be_filtered_on_with_counts(library):
    facets = emby_library_index.facets(library)

    tags = {entry["value"]: entry["count"] for entry in facets["tags"]}
    assert tags["4k"] == 3
    assert tags["hdr"] == 2
    assert tags["sd"] == 1
    # Ranked by frequency, so a filter UI can lead with the useful ones.
    assert facets["tags"][0]["value"] == "4k"
    assert {entry["value"] for entry in facets["people"]} == {
        "Ada Lovelace",
        "Grace Hopper",
        "Katherine Johnson",
    }


def test_summary_reports_what_the_index_holds(library):
    summary = emby_library_index.summary(library)

    assert summary["status"] == "ready"
    assert summary["item_count"] == 4
    assert summary["has_watch_state"] is True
    assert summary["played_count"] == 3
    assert summary["favorite_count"] == 3
    assert summary["item_count"] - summary["played_count"] == 1


def test_summary_of_a_missing_index_is_honest_about_being_missing(index_root):
    assert emby_library_index.summary()["status"] == "missing"


# --- persistence ------------------------------------------------------------


def test_the_index_survives_a_round_trip(index_root, monkeypatch):
    _stub_fetch(monkeypatch, [_raw_item("Persisted")])
    emby_library_index.refresh(_settings())

    reloaded = emby_library_index.load_index()

    assert reloaded["item_count"] == 1
    assert reloaded["items"][0]["name"] == "Persisted"
    assert emby_library_index.search(reloaded, tags=["4k"])


def test_a_corrupt_index_reads_as_missing_rather_than_raising(index_root):
    index_root.mkdir(parents=True, exist_ok=True)
    (index_root / "library-index.json.gz").write_bytes(b"not gzip at all")

    assert emby_library_index.load_index() is None
    assert emby_library_index.summary()["status"] == "missing"


# --- privacy ----------------------------------------------------------------


def test_the_watch_history_is_kept_out_of_the_state_backup(tmp_path):
    """/system/backup is unauthenticated, and this index says what was watched.

    There is no field to blank here the way the Emby key is blanked -- the whole
    file is the sensitive part -- so the directory is excluded outright.
    """
    state = tmp_path / "state"
    (state / "emby-index").mkdir(parents=True)
    (state / "logs").mkdir()
    (state / "emby-index" / "library-index.json.gz").write_bytes(b"personal viewing history")
    (state / "logs" / "job.txt").write_text("ordinary log", encoding="utf-8")

    archive_path, backup = system_status.create_state_backup(str(state))

    import zipfile

    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()

    assert not any("emby-index" in name for name in names), "watch history must not be in the archive"
    assert any("logs/job.txt" in name.replace("\\", "/") for name in names), "ordinary state is still archived"
    # Said out loud in the manifest rather than dropped silently.
    assert "emby-index" in backup["excluded"]
    manifest = json.loads(zipfile.ZipFile(archive_path).read("vid2gif-backup.json").decode("utf-8"))
    assert "emby-index" in manifest["excluded"]


# --- choosing an account and locating the file ------------------------------


def test_users_can_be_listed_so_an_account_can_be_chosen(monkeypatch):
    """Watch state belongs to one Emby account; the operator has to pick it."""
    monkeypatch.setattr(
        emby_client,
        "request_json",
        lambda *a, **k: (
            [
                {"Id": "abc", "Name": "Chris"},
                {"Id": "def", "Name": "Guest"},
                {"NoId": True},
            ],
            emby_client.result("success", "ok"),
        ),
    )

    users, outcome = emby_library_index.list_users(_settings())

    assert outcome["status"] == "success"
    assert users == [{"id": "abc", "name": "Chris"}, {"id": "def", "name": "Guest"}]


def test_a_failed_user_lookup_returns_the_reason(monkeypatch):
    monkeypatch.setattr(
        emby_client,
        "request_json",
        lambda *a, **k: (None, emby_client.result("failed", "Emby is unreachable")),
    )

    users, outcome = emby_library_index.list_users(_settings())

    assert users is None
    assert outcome["status"] == "failed"


def test_an_unexpected_user_payload_is_rejected(monkeypatch):
    monkeypatch.setattr(
        emby_client,
        "request_json",
        lambda *a, **k: ({"not": "a list"}, emby_client.result("success", "ok")),
    )

    users, outcome = emby_library_index.list_users(_settings())

    assert users is None
    assert outcome["status"] == "failed"


def test_rows_carry_the_local_path_so_a_result_can_be_acted_on(tmp_path, monkeypatch):
    """A search result is only useful if it names the file on this machine."""
    root = tmp_path / "emby-index"
    monkeypatch.setattr(emby_library_index, "INDEX_ROOT", str(root))
    monkeypatch.setattr(emby_library_index, "INDEX_PATH", str(root / "library-index.json.gz"))
    monkeypatch.setattr(
        emby_library_index.emby_catalog,
        "load_catalog",
        lambda *a, **k: (
            {"items": [{"path": "/media/Mapped.mkv", "local_path": "/library/Mapped.mkv"}]},
            {},
        ),
    )
    _stub_fetch(monkeypatch, [_raw_item("Mapped", Path="/media/Mapped.mkv"), _raw_item("Unmapped")])

    payload, _outcome = emby_library_index.refresh(_settings())

    by_name = {item["name"]: item for item in payload["items"]}
    assert by_name["Mapped"]["local_path"] == "/library/Mapped.mkv"
    # No mapping is an empty string rather than a path that does not exist here.
    assert by_name["Unmapped"]["local_path"] == ""


def test_an_unavailable_catalog_does_not_stop_the_index(tmp_path, monkeypatch):
    """Local paths are a convenience; the metadata is the point."""
    root = tmp_path / "emby-index"
    monkeypatch.setattr(emby_library_index, "INDEX_ROOT", str(root))
    monkeypatch.setattr(emby_library_index, "INDEX_PATH", str(root / "library-index.json.gz"))

    def explode(*_args, **_kwargs):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(emby_library_index.emby_catalog, "load_catalog", explode)
    _stub_fetch(monkeypatch, [_raw_item("Still Indexed")])

    payload, outcome = emby_library_index.refresh(_settings())

    assert outcome["status"] == "success"
    assert payload["item_count"] == 1
    assert payload["items"][0]["local_path"] == ""


def test_a_failed_sweep_leaves_the_previous_index_alone(tmp_path, monkeypatch):
    """A network blip should not wipe a good index."""
    root = tmp_path / "emby-index"
    monkeypatch.setattr(emby_library_index, "INDEX_ROOT", str(root))
    monkeypatch.setattr(emby_library_index, "INDEX_PATH", str(root / "library-index.json.gz"))
    monkeypatch.setattr(emby_library_index, "_path_lookup", lambda *a, **k: {})

    _stub_fetch(monkeypatch, [_raw_item("Original")])
    emby_library_index.refresh(_settings())

    _stub_fetch(monkeypatch, None, status="failed")
    payload, outcome = emby_library_index.refresh(_settings())

    assert payload is None
    assert outcome["status"] == "failed"
    kept = emby_library_index.load_index()
    assert kept["items"][0]["name"] == "Original", "the good index must survive a failed refresh"
