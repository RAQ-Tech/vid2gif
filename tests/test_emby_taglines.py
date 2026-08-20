"""The tagline-titles workflow: cleaning, classification, apply, and undo.

This is the automation of a job the operator used to do by stopping the Emby
container and editing its database by hand. The stakes are that a bad regex
writes a wrong title into a real library, so the cleaning table is deliberately
long -- and the apply may only ever touch a title whose original is already
safe in the original-title field.
"""

import pytest

from app import emby_client, emby_taglines


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    emby_taglines.tagline_scans.clear()
    emby_taglines.tagline_plans.clear()
    emby_taglines.tagline_runs.clear()
    monkeypatch.setattr(emby_taglines, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(emby_taglines, "LOG_INDEX", str(tmp_path / "logs" / "index.json"))


def _settings(**overrides):
    base = {
        "emby_url": "http://emby:8096",
        "emby_api_key": "key",
        "emby_user_id": "u1",
        "emby_tagline_lock_items": True,
    }
    base.update(overrides)
    return base


# --- the cleaning table ------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # The operator's own examples.
        ("The Wire S03E12", "The Wire"),
        ("Show s4e01", "Show"),
        ("Show s03,e9 Finale", "Show Finale"),
        # Marker positions: leading, middle, bracketed.
        ("S03E12 - The Title", "The Title"),
        ("Show - S03E12 - The End", "Show - The End"),
        ("Show (S03E12)", "Show"),
        ("Show [s01e02]", "Show"),
        # Separator variants inside the marker.
        ("Show s03 e09", "Show"),
        ("Show s03.e09", "Show"),
        ("Show s03-e09", "Show"),
        ("Show s03, e9", "Show"),
        # Double episodes.
        ("Show S01E01E02", "Show"),
        ("Show S01E01-E02", "Show"),
        # The spelled-out pair.
        ("Season 3 Episode 12 Show", "Show"),
        ("Show Season 12, Episode 3", "Show"),
        # Titles that must NOT be touched.
        ("Ocean's 11", "Ocean's 11"),
        ("Se7en", "Se7en"),
        ("Superman 2015", "Superman 2015"),
        ("Season of the Witch", "Season of the Witch"),
        ("Espionage 101", "Espionage 101"),
        # Whitespace is tidied even without markers.
        ("Two   Spaces", "Two Spaces"),
        # A title that is nothing but a marker cleans to nothing.
        ("s03e12", ""),
        ("", ""),
    ],
)
def test_clean_title(title, expected):
    assert emby_taglines.clean_title(title) == expected


# --- classification ----------------------------------------------------------


def _raw(name, tagline=None, original=None, lock=False, item_id="id-1"):
    raw = {"Id": item_id, "Name": name, "Type": "Video", "Path": f"/media/{item_id}.mkv"}
    if tagline is not None:
        raw["Taglines"] = [tagline]
    if original is not None:
        raw["OriginalTitle"] = original
    if lock:
        raw["LockData"] = True
    return raw


def test_a_marker_title_with_empty_original_writes_title_and_tagline():
    entry = emby_taglines.classify_item(_raw("Show S01E02"))

    assert entry["status"] == "ready"
    assert entry["proposed_tagline"] == "Show"
    assert entry["writes_title"] is True
    assert entry["original_backup"] == "will-copy"
    assert entry["detail"] == "Title and tagline will be written"


def test_an_original_matching_the_title_is_the_safety_copy():
    entry = emby_taglines.classify_item(_raw("Show S01E02", original="Show S01E02"))

    assert entry["writes_title"] is True
    assert entry["original_backup"] == "existing-copy"


def test_an_occupied_original_holds_the_title_and_still_writes_the_tagline():
    """The operator's own condition: no second copy, no title edit."""
    entry = emby_taglines.classify_item(_raw("Amelie S01E02", original="Le Fabuleux Destin"))

    assert entry["status"] == "ready"
    assert entry["writes_title"] is False
    assert entry["original_backup"] == "occupied"
    assert "original-title field is in use" in entry["detail"]


def test_a_clean_title_only_needs_its_tagline():
    entry = emby_taglines.classify_item(_raw("Plain Movie"))

    assert entry["status"] == "ready"
    assert entry["writes_title"] is False
    assert entry["original_backup"] == "not-needed"
    assert entry["detail"] == "Tagline will be written"


def test_a_finished_item_is_done():
    entry = emby_taglines.classify_item(_raw("Show", tagline="Show", lock=True))

    assert entry["status"] == "done"


def test_a_matching_tagline_without_the_lock_is_still_ready():
    entry = emby_taglines.classify_item(_raw("Show", tagline="Show", lock=False))

    assert entry["status"] == "ready"
    assert "metadata lock" in entry["detail"]
    # And with locking disabled the same item is done.
    assert emby_taglines.classify_item(_raw("Show", tagline="Show"), lock_items=False)["status"] == "done"


def test_a_pure_marker_title_is_unusable():
    entry = emby_taglines.classify_item(_raw("S01E02"))

    assert entry["status"] == "unusable"


# --- scan, paging, plan ------------------------------------------------------


def _run_scan(monkeypatch, raws):
    monkeypatch.setattr(
        emby_taglines.emby_client,
        "request_paged_json",
        lambda *a, **k: (raws, emby_client.result("success", "ok")),
    )
    scan, err = emby_taglines.start_scan(_settings(), synchronous=True)
    assert err is None and scan["status"] == "success", err
    return scan


def test_scan_classifies_and_counts(monkeypatch):
    scan = _run_scan(
        monkeypatch,
        [
            _raw("Show S01E01", item_id="a"),
            _raw("Done", tagline="Done", lock=True, item_id="b"),
            _raw("S01E01", item_id="c"),
        ],
    )

    assert scan["counts"] == {"ready": 1, "done": 1, "unusable": 1}
    payload, err = emby_taglines.items_payload(scan["id"], status="ready")
    assert err is None
    assert [item["id"] for item in payload["items"]] == ["a"]
    everything, _err = emby_taglines.items_payload(scan["id"], status="all", limit=2)
    assert everything["total"] == 3
    assert everything["has_next"] is True


def test_a_failed_sweep_reports_the_reason(monkeypatch):
    monkeypatch.setattr(
        emby_taglines.emby_client,
        "request_paged_json",
        lambda *a, **k: (None, emby_client.result("failed", "Emby is unreachable")),
    )

    scan, err = emby_taglines.start_scan(_settings(), synchronous=True)

    assert err is None
    assert scan["status"] == "failed"
    assert "unreachable" in scan["error"]


def test_scan_refuses_without_emby_configured():
    scan, err = emby_taglines.start_scan({"emby_url": "", "emby_api_key": ""}, synchronous=True)

    assert scan is None
    assert "not configured" in err


def test_plan_selection_covers_all_eligible_minus_exclusions(monkeypatch):
    scan = _run_scan(monkeypatch, [_raw("A S01E01", item_id="a"), _raw("B S01E01", item_id="b")])

    plan, err = emby_taglines.build_plan(
        {"scan_id": scan["id"], "selection": {"mode": "all_eligible", "excluded_item_ids": ["b"]}}
    )

    assert err is None
    assert [item["id"] for item in plan["items"]] == ["a"]
    nothing, err = emby_taglines.build_plan(
        {"scan_id": scan["id"], "selection": {"mode": "all_eligible", "excluded_item_ids": ["a", "b"]}}
    )
    assert nothing is None and err == "Nothing is selected"


# --- apply -------------------------------------------------------------------


def _fake_server(monkeypatch, items_by_id, fail_write_for=()):
    """A stand-in Emby: GET returns the stored item, POST records the write."""
    writes = []

    def fake_get(settings, api_path, **kwargs):
        item_id = api_path.rstrip("/").split("/")[-1]
        raw = items_by_id.get(item_id)
        if raw is None:
            return None, emby_client.result("failed", "Item not found")
        return dict(raw), emby_client.result("success", "ok")

    def fake_write(settings, api_path, *, json_body=None, **kwargs):
        item_id = api_path.rstrip("/").split("/")[-1]
        if item_id in fail_write_for:
            return emby_client.result("failed", "Emby rejected the update")
        writes.append((item_id, json_body))
        items_by_id[item_id] = dict(json_body)
        return emby_client.result("success", "ok")

    monkeypatch.setattr(emby_taglines.emby_client, "request_json", fake_get)
    monkeypatch.setattr(emby_taglines.emby_client, "request_no_content", fake_write)
    return writes


def _plan_for(monkeypatch, raws):
    scan = _run_scan(monkeypatch, [dict(raw) for raw in raws])
    plan, err = emby_taglines.build_plan({"scan_id": scan["id"], "selection": {"mode": "all_eligible"}})
    assert err is None, err
    return plan


def test_apply_writes_title_tagline_backup_and_lock(monkeypatch):
    raw = _raw("Show S01E02", item_id="a")
    raw["Genres"] = ["Action"]  # round-tripped untouched
    plan = _plan_for(monkeypatch, [raw])
    writes = _fake_server(monkeypatch, {"a": dict(raw)})

    run, err = emby_taglines.start_apply(plan["id"], _settings(), synchronous=True)

    assert err is None
    assert run["status"] == "success" and run["applied_count"] == 1
    item_id, body = writes[0]
    assert item_id == "a"
    assert body["Name"] == "Show"
    assert body["OriginalTitle"] == "Show S01E02", "the original must be preserved before the title changes"
    assert body["Taglines"] == ["Show"]
    assert body["LockData"] is True
    assert body["Genres"] == ["Action"], "everything the GET returned rides along unchanged"


def test_apply_leaves_the_title_alone_when_the_original_is_occupied(monkeypatch):
    raw = _raw("Amelie S01E02", original="Le Fabuleux Destin", item_id="a")
    plan = _plan_for(monkeypatch, [raw])
    writes = _fake_server(monkeypatch, {"a": dict(raw)})

    run, err = emby_taglines.start_apply(plan["id"], _settings(), synchronous=True)

    assert err is None and run["applied_count"] == 1
    _item_id, body = writes[0]
    assert body["Name"] == "Amelie S01E02", "no second copy means no title edit"
    assert body["OriginalTitle"] == "Le Fabuleux Destin", "real original-title data is never displaced"
    assert body["Taglines"] == ["Amelie"]


def test_apply_respects_the_lock_setting(monkeypatch):
    raw = _raw("Show S01E02", item_id="a")
    monkeypatch.setattr(
        emby_taglines.emby_client,
        "request_paged_json",
        lambda *a, **k: ([dict(raw)], emby_client.result("success", "ok")),
    )
    scan, _ = emby_taglines.start_scan(_settings(emby_tagline_lock_items=False), synchronous=True)
    plan, _ = emby_taglines.build_plan({"scan_id": scan["id"], "selection": {"mode": "all_eligible"}})
    writes = _fake_server(monkeypatch, {"a": dict(raw)})

    emby_taglines.start_apply(plan["id"], _settings(emby_tagline_lock_items=False), synchronous=True)

    _item_id, body = writes[0]
    assert "LockData" not in body or body["LockData"] is False


def test_apply_refuses_an_item_whose_title_changed_since_the_scan(monkeypatch):
    raw = _raw("Show S01E02", item_id="a")
    plan = _plan_for(monkeypatch, [raw])
    changed = dict(raw, Name="Renamed Since The Scan S05E05")
    writes = _fake_server(monkeypatch, {"a": changed})

    run, err = emby_taglines.start_apply(plan["id"], _settings(), synchronous=True)

    assert err is None
    assert run["refused_count"] == 1 and run["applied_count"] == 0
    assert writes == [], "a stale plan must not write anything"


def test_apply_refuses_an_incomplete_item_rather_than_blanking_it(monkeypatch):
    raw = _raw("Show S01E02", item_id="a")
    plan = _plan_for(monkeypatch, [raw])
    writes = _fake_server(monkeypatch, {"a": {"Id": "a"}})  # no Name: partial GET

    run, err = emby_taglines.start_apply(plan["id"], _settings(), synchronous=True)

    assert err is None
    assert run["refused_count"] == 1
    assert writes == []


def test_one_failed_write_does_not_stop_the_rest(monkeypatch):
    raws = [_raw("A S01E01", item_id="a"), _raw("B S01E01", item_id="b")]
    plan = _plan_for(monkeypatch, raws)
    writes = _fake_server(monkeypatch, {r["Id"]: dict(r) for r in raws}, fail_write_for={"a"})

    run, err = emby_taglines.start_apply(plan["id"], _settings(), synchronous=True)

    assert err is None
    assert run["failed_count"] == 1 and run["applied_count"] == 1
    assert [item_id for item_id, _body in writes] == ["b"]


def test_apply_writes_an_undoable_log(monkeypatch):
    raw = _raw("Show S01E02", tagline="Old tagline", item_id="a")
    plan = _plan_for(monkeypatch, [raw])
    _fake_server(monkeypatch, {"a": dict(raw)})

    run, _err = emby_taglines.start_apply(plan["id"], _settings(), synchronous=True)

    assert run["log_id"]
    header, records, err = emby_taglines.read_log(run["log_id"])
    assert err is None
    assert header["applied_count"] == 1
    before = records[0]["before"]
    assert before["name"] == "Show S01E02"
    assert before["taglines"] == ["Old tagline"]
    assert before["lock_data"] is False


# --- undo --------------------------------------------------------------------


def test_undo_restores_all_four_fields_onto_the_current_item(monkeypatch):
    raw = _raw("Show S01E02", tagline="Old tagline", item_id="a")
    plan = _plan_for(monkeypatch, [raw])
    server_items = {"a": dict(raw)}
    writes = _fake_server(monkeypatch, server_items)
    run, _err = emby_taglines.start_apply(plan["id"], _settings(), synchronous=True)

    # The operator edits something unrelated in Emby after the apply.
    server_items["a"]["Overview"] = "Written after the apply"
    writes.clear()

    undo, err = emby_taglines.start_undo(run["log_id"], _settings(), synchronous=True)

    assert err is None and undo["applied_count"] == 1
    _item_id, body = writes[0]
    assert body["Name"] == "Show S01E02"
    assert body["OriginalTitle"] == ""
    assert body["Taglines"] == ["Old tagline"]
    assert body["LockData"] is False
    assert body["Overview"] == "Written after the apply", "undo must not clobber later edits"


def test_undo_of_a_log_with_nothing_applied_is_refused(monkeypatch):
    raw = _raw("Show S01E02", item_id="a")
    plan = _plan_for(monkeypatch, [raw])
    _fake_server(monkeypatch, {"a": {"Id": "a"}})  # everything refused
    run, _err = emby_taglines.start_apply(plan["id"], _settings(), synchronous=True)

    undo, err = emby_taglines.start_undo(run["log_id"], _settings(), synchronous=True)

    assert undo is None
    assert "nothing to undo" in err


def test_undo_of_a_missing_log_is_an_error():
    undo, err = emby_taglines.start_undo("no-such-log", _settings(), synchronous=True)

    assert undo is None
    assert err == "Log not found"


# --- endpoints under user context --------------------------------------------


def test_item_reads_use_the_user_endpoint_when_an_account_is_chosen(monkeypatch):
    seen = []

    def fake_get(settings, api_path, **kwargs):
        seen.append(api_path)
        return None, emby_client.result("failed", "stop here")

    monkeypatch.setattr(emby_taglines.emby_client, "request_json", fake_get)
    raw = _raw("Show S01E02", item_id="a")
    plan = _plan_for(monkeypatch, [raw])

    emby_taglines.start_apply(plan["id"], _settings(emby_user_id="u1"), synchronous=True)

    assert seen == ["/Users/u1/Items/a"]


def test_log_index_survives_garbage(tmp_path, monkeypatch):
    monkeypatch.setattr(emby_taglines, "LOG_INDEX", str(tmp_path / "logs" / "index.json"))
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "index.json").write_text("[not, an, object]", encoding="utf-8")

    assert emby_taglines.list_logs() == {"logs": []}
    _header, _records, err = emby_taglines.read_log("anything")
    assert err == "Log not found"
