"""The refusals that protect a library during duplicate cleanup.

apply_duplicate_cleanup_plan guards the files twice over, and the two layers are
worth understanding separately:

* A **group preflight** re-checks every planned file's identity before touching
  anything in that group. If one has changed, moved, or gone, the whole group is
  skipped untouched. This is the layer that fires in practice.
* A **per-file gate** then re-checks each file individually: still inside the
  library, not a symlink, still present, still the file the scan measured, with
  a destination inside quarantine that is not already occupied.

There turn out to be four group-level guards, not one: a planned file's identity
changing, the folder's contents changing, a quarantine destination becoming
unavailable, and the group being edited after the preview. Between them they
shadow most of the per-file gate, which is why that gate was the largest
uncovered block in the module -- it is redundant defence in depth rather than
neglected code.

Both layers are tested. The group guards are driven the way they actually fire.
The per-file checks that nothing upstream covers -- a source outside the
library, a symlink, a destination outside quarantine -- are exercised with the
identity preflight satisfied, the way they would run if a file changed in the
window between the two checks.
"""

import os

import pytest

from app import app_settings, maintenance


def _reset(monkeypatch, settings_overrides=None):
    maintenance.duplicate_scans.clear()
    maintenance.cleanup_plans.clear()
    maintenance.duplicate_apply_runs.clear()
    settings = app_settings.default_settings()
    settings.update(settings_overrides or {})
    monkeypatch.setattr(maintenance, "probe_video_metadata", lambda path: {})
    monkeypatch.setattr(maintenance.app_settings, "load_settings", lambda: dict(settings))


def _library_with_duplicate(tmp_path, name="Movie"):
    lib = tmp_path / "library"
    folder = lib / name
    folder.mkdir(parents=True)
    keep = folder / f"{name}.1080p.mkv"
    remove = folder / f"{name}.720p.mkv"
    keep.write_bytes(b"a" * 400)
    remove.write_bytes(b"b" * 100)
    return lib, keep, remove


def _plan(lib, monkeypatch, action="move", settings_overrides=None):
    _reset(monkeypatch, settings_overrides)
    scan, err = maintenance.start_duplicate_scan(str(lib), lib_root=str(lib), synchronous=True)
    assert err is None and scan["status"] == "success", err
    plan, err = maintenance.build_duplicate_cleanup_plan(
        {
            "scan_id": scan["id"],
            "action": action,
            "groups": [],
            "visible_group_ids": [group["id"] for group in scan.get("groups") or []],
        },
        lib_root=str(lib),
    )
    assert err is None, err
    assert plan.get("files"), "the fixture should produce at least one file to act on"
    return plan


def _stored_files(plan):
    """The live plan the apply reads, so a test can put it into a bad state."""
    return maintenance.cleanup_plans[plan["id"]]["files"]


def _pass_preflight(monkeypatch):
    """Satisfy the group check so the per-file gate behind it can be reached.

    This models the window between the two checks: the preflight saw a healthy
    group, and the file changed before the per-file gate ran.
    """
    monkeypatch.setattr(maintenance, "_identity_matches", lambda path, expected: True)


def _reasons(result):
    return [item.get("reason", "") for item in result.get("refused") or []]


# --- the layer that fires in practice --------------------------------------


def test_a_group_whose_file_changed_is_skipped_whole(tmp_path, monkeypatch):
    """One altered file protects every file in its group, not just itself."""
    lib, keep, remove = _library_with_duplicate(tmp_path)
    plan = _plan(lib, monkeypatch)

    target = _stored_files(plan)[0]["source_path"]
    with open(target, "wb") as handle:
        handle.write(b"c" * 5000)

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result.get("skipped_changed_group_count") == 1
    assert "changed after the cleanup preview" in result["skipped_changed_groups"][0]["reason"]
    assert result.get("applied_count", 0) == 0
    assert os.path.exists(target), "nothing in a changed group may be touched"
    assert keep.exists()


def test_a_group_whose_file_vanished_is_skipped_whole(tmp_path, monkeypatch):
    lib, keep, remove = _library_with_duplicate(tmp_path)
    plan = _plan(lib, monkeypatch)

    os.remove(_stored_files(plan)[0]["source_path"])

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result.get("skipped_changed_group_count") == 1
    assert keep.exists(), "the keeper is left alone when its group is skipped"


def test_a_changed_group_does_not_stop_a_healthy_one(tmp_path, monkeypatch):
    """Skipping is per group, so one edited file cannot abandon the whole run."""
    lib = tmp_path / "library"
    for name in ("First", "Second"):
        folder = lib / name
        folder.mkdir(parents=True)
        (folder / f"{name}.1080p.mkv").write_bytes(b"a" * 400)
        (folder / f"{name}.720p.mkv").write_bytes(b"b" * 100)

    plan = _plan(lib, monkeypatch, action="move")
    files = _stored_files(plan)
    assert len(files) >= 2, "fixture should plan more than one group"

    with open(files[0]["source_path"], "wb") as handle:
        handle.write(b"c" * 5000)
    survivor = files[1]["source_path"]

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result.get("skipped_changed_group_count") == 1
    assert result.get("applied_count", 0) >= 1
    assert not os.path.exists(survivor), "the untouched group should still be cleaned"


# --- the layer behind it ----------------------------------------------------


def test_a_source_outside_the_library_is_refused(tmp_path, monkeypatch):
    lib, keep, remove = _library_with_duplicate(tmp_path)
    outsider = tmp_path / "elsewhere.mkv"
    outsider.write_bytes(b"not yours to touch")

    plan = _plan(lib, monkeypatch)
    _pass_preflight(monkeypatch)
    _stored_files(plan)[0]["source_path"] = str(outsider)

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert any("outside the library" in reason for reason in _reasons(result))
    assert outsider.exists(), "a file outside the library must never be touched"
    assert outsider.read_bytes() == b"not yours to touch"


def test_a_symlink_is_never_cleaned(tmp_path, monkeypatch):
    lib, keep, remove = _library_with_duplicate(tmp_path)
    real = lib / "Movie" / "real-target.mkv"
    real.write_bytes(b"the real file")
    link = lib / "Movie" / "link.mkv"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")

    plan = _plan(lib, monkeypatch)
    _pass_preflight(monkeypatch)
    _stored_files(plan)[0]["source_path"] = str(link)

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert any("Symlink" in reason for reason in _reasons(result))
    # Following the link and deleting would destroy a file nobody selected.
    assert link.exists()
    assert real.exists() and real.read_bytes() == b"the real file"


def test_a_vanished_file_is_caught_by_the_folder_snapshot(tmp_path, monkeypatch):
    """A third guard: the folder itself is compared, not just the planned files.

    Even with the identity check satisfied, a file disappearing changes the
    folder, and that alone is enough to leave the group alone.
    """
    lib, keep, remove = _library_with_duplicate(tmp_path)
    plan = _plan(lib, monkeypatch)
    _pass_preflight(monkeypatch)

    os.remove(_stored_files(plan)[0]["source_path"])

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result.get("skipped_changed_group_count") == 1
    assert "folder changed" in result["skipped_changed_groups"][0]["reason"]
    assert keep.exists(), "the keeper is untouched when its folder changed"


def test_a_destination_outside_quarantine_is_refused(tmp_path, monkeypatch):
    lib, keep, remove = _library_with_duplicate(tmp_path)
    plan = _plan(lib, monkeypatch, action="move")
    _pass_preflight(monkeypatch)

    item = _stored_files(plan)[0]
    source = item["source_path"]
    item["destination_path"] = str(lib / "Movie" / "somewhere-else.mkv")

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert any("outside quarantine" in reason for reason in _reasons(result))
    assert os.path.exists(source), "the source stays put when the destination is wrong"


def test_an_occupied_destination_is_never_overwritten(tmp_path, monkeypatch):
    """A fourth guard: quarantine destinations are checked for availability.

    Occupying the destination is caught before the move is attempted, so the
    file already sitting there is never clobbered and the source stays put.
    """
    lib, keep, remove = _library_with_duplicate(tmp_path)
    plan = _plan(lib, monkeypatch, action="move")
    _pass_preflight(monkeypatch)

    item = _stored_files(plan)[0]
    source = item["source_path"]
    destination = item["destination_path"]
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "wb") as handle:
        handle.write(b"something already quarantined here")

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result.get("skipped_changed_group_count") == 1
    assert "no longer available" in result["skipped_changed_groups"][0]["reason"]
    assert os.path.exists(source), "the source is kept when the move cannot happen"
    with open(destination, "rb") as handle:
        assert handle.read() == b"something already quarantined here", "an existing file must not be clobbered"


# --- the happy path, for contrast -------------------------------------------


def test_delete_removes_the_duplicate_and_keeps_the_keeper(tmp_path, monkeypatch):
    lib, keep, remove = _library_with_duplicate(tmp_path)
    plan = _plan(lib, monkeypatch, action="delete")

    planned = {item["source_path"] for item in _stored_files(plan)}
    assert str(keep) not in planned, "the keeper must never be planned for deletion"

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert keep.exists(), "the file being kept must survive"
    assert keep.read_bytes() == b"a" * 400
    assert not remove.exists(), "the duplicate should be gone"
