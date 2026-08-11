import os

from app import duplicate_slots


def _video(video_id, stem, *, duration=3600.0, folder="/library/Movie", accessories=None):
    return {
        "id": video_id,
        "kind": "video",
        "stem": stem,
        "name": f"{stem}.mkv",
        "path": os.path.join(folder, f"{stem}.mkv"),
        "size_bytes": 1000,
        "metadata": {"duration_seconds": duration},
        "accessories": accessories or [],
    }


def _accessory(file_id, parent_stem, suffix, role, *, size=100, folder="/library/Movie"):
    return {
        "id": file_id,
        "kind": "accessory",
        "role": role,
        "suffix": suffix,
        "equivalence_key": f"{role}:{suffix.lower()}",
        "name": f"{parent_stem}{suffix}",
        "path": os.path.join(folder, f"{parent_stem}{suffix}"),
        "size_bytes": size,
    }


def _slot(slots, key):
    return next(slot for slot in slots if slot["slot_key"] == key)


def _quality(coverage, status="complete", last=None):
    return {
        "status": status,
        "coverage_percent": coverage,
        "coverage_ratio": None if coverage is None else coverage / 100.0,
        "last_timestamp_seconds": last,
        "label": f"{coverage}% coverage",
    }


def test_best_subtitle_is_borrowed_from_a_losing_copy(monkeypatch):
    keeper = _video("v1", "Movie [WEBDL-2160p]", accessories=[
        _accessory("s1", "Movie [WEBDL-2160p]", ".eng.srt", "subtitle", size=100),
    ])
    other = _video("v2", "Movie [BluRay-1080p]", accessories=[
        _accessory("s2", "Movie [BluRay-1080p]", ".eng.srt", "subtitle", size=200),
    ])
    coverage = {"s1": _quality(61.0, last=2200.0), "s2": _quality(98.0, last=3550.0)}

    def analyze(path, duration):
        return coverage["s1" if "2160p" in path else "s2"]

    slots = duplicate_slots.resolve_group_slots(
        [keeper, other], keeper, analyze_subtitle=analyze
    )
    slot = _slot(slots, "subtitle:.eng.srt")

    assert slot["winner_file_id"] == "s2"
    assert slot["winner_video_id"] == "v2"
    assert slot["borrowed"] is True
    # The borrowed file is renamed onto the keeper's stem.
    assert slot["destination_path"].endswith("Movie [WEBDL-2160p].eng.srt")
    assert slot["loser_file_ids"] == ["s1"]
    assert "98" in slot["reason"]


def test_subtitle_running_past_the_keeper_is_passed_over_and_flagged(monkeypatch):
    """A subtitle from a longer cut must not win just because it covers more."""
    keeper = _video("v1", "Movie short", duration=3000.0, accessories=[
        _accessory("s1", "Movie short", ".eng.srt", "subtitle", size=100),
    ])
    longer = _video("v2", "Movie long", duration=3600.0, accessories=[
        _accessory("s2", "Movie long", ".eng.srt", "subtitle", size=200),
    ])
    # s2's timestamps run 500s past the keeper's runtime.
    coverage = {
        "s1": _quality(97.0, status="complete", last=2950.0),
        "s2": _quality(116.0, status="timing_review", last=3500.0),
    }

    def analyze(path, duration):
        assert duration == 3000.0, "candidates must be judged against the keeper"
        return coverage["s1" if "short" in path else "s2"]

    slots = duplicate_slots.resolve_group_slots(
        [keeper, longer], keeper, analyze_subtitle=analyze
    )
    slot = _slot(slots, "subtitle:.eng.srt")

    assert slot["winner_file_id"] == "s1"
    assert slot["borrowed"] is False
    assert slot["needs_review"] is True
    assert any(flag["kind"] == "runtime_mismatch_avoided" for flag in slot["flags"])


def test_only_subtitle_available_flags_when_it_overruns_the_keeper():
    keeper = _video("v1", "Movie short", duration=3000.0)
    longer = _video("v2", "Movie long", duration=3600.0, accessories=[
        _accessory("s2", "Movie long", ".eng.srt", "subtitle"),
    ])

    slots = duplicate_slots.resolve_group_slots(
        [keeper, longer],
        keeper,
        analyze_subtitle=lambda path, duration: _quality(
            116.0, status="timing_review", last=3500.0
        ),
    )
    slot = _slot(slots, "subtitle:.eng.srt")

    assert slot["winner_file_id"] == "s2"
    assert slot["needs_review"] is True
    assert any(flag["kind"] == "runtime_mismatch" for flag in slot["flags"])


def test_single_subtitle_matching_the_keeper_is_adopted_without_a_flag():
    """The runtime check must not flag a lone subtitle that actually fits."""
    keeper = _video("v1", "movie", duration=3000.0)
    copy_one = _video("v2", "movie(1)", duration=3000.0, accessories=[
        _accessory("s2", "movie(1)", ".eng.srt", "subtitle"),
    ])

    slots = duplicate_slots.resolve_group_slots(
        [keeper, copy_one],
        keeper,
        analyze_subtitle=lambda path, duration: _quality(97.0, last=2950.0),
    )
    slot = _slot(slots, "subtitle:.eng.srt")

    assert slot["winner_file_id"] == "s2"
    assert slot["borrowed"] is True
    assert slot["needs_review"] is False
    assert slot["flags"] == []
    assert os.path.basename(slot["destination_path"]) == "movie.eng.srt"


def test_orphan_sidecar_is_kept_and_renamed_onto_the_keeper_stem():
    """movie.mkv has no background; a copy's background is adopted and renamed."""
    keeper = _video("v1", "movie")
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("b1", "movie(1)", "-background.png", "background", size=500),
    ])

    slots = duplicate_slots.resolve_group_slots(
        [keeper, copy_one], keeper, probe_image=lambda path: {"width": 1920, "height": 1080}
    )
    slot = _slot(slots, "background:-background.png")

    assert slot["winner_file_id"] == "b1"
    assert slot["borrowed"] is True
    assert slot["reason"] == "Only copy in the set"
    assert os.path.basename(slot["destination_path"]) == "movie-background.png"
    assert slot["needs_review"] is False


def test_identical_sidecars_prefer_the_keeper_without_flagging():
    keeper = _video("v1", "movie", accessories=[
        _accessory("n1", "movie", ".nfo", "nfo", size=400),
    ])
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("n2", "movie(1)", ".nfo", "nfo", size=400),
    ])

    slots = duplicate_slots.resolve_group_slots([keeper, copy_one], keeper)
    slot = _slot(slots, "nfo:.nfo")

    assert slot["winner_file_id"] == "n1"
    assert slot["identical"] is True
    assert slot["borrowed"] is False
    assert slot["needs_review"] is False
    assert slot["reason"] == "Identical in every copy"


def test_highest_resolution_image_wins_across_copies():
    keeper = _video("v1", "movie", accessories=[
        _accessory("p1", "movie", "-poster.jpg", "poster", size=100),
    ])
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("p2", "movie(1)", "-poster.jpg", "poster", size=90),
    ])
    sizes = {"p1": (1000, 1500), "p2": (3000, 2000)}

    def probe(path):
        width, height = sizes["p1" if path.endswith("movie-poster.jpg") else "p2"]
        return {"width": width, "height": height}

    slots = duplicate_slots.resolve_group_slots([keeper, copy_one], keeper, probe_image=probe)
    slot = _slot(slots, "poster:-poster.jpg")

    assert slot["winner_file_id"] == "p2"
    assert slot["borrowed"] is True
    assert "3000 x 2000" in slot["reason"]


def test_images_within_the_close_margin_are_flagged_for_review():
    keeper = _video("v1", "movie", accessories=[
        _accessory("p1", "movie", "-poster.jpg", "poster", size=100),
    ])
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("p2", "movie(1)", "-poster.jpg", "poster", size=90),
    ])

    def probe(path):
        return (
            {"width": 1000, "height": 1000}
            if path.endswith("movie-poster.jpg")
            else {"width": 1010, "height": 1000}
        )

    slots = duplicate_slots.resolve_group_slots([keeper, copy_one], keeper, probe_image=probe)
    slot = _slot(slots, "poster:-poster.jpg")

    assert slot["needs_review"] is True
    assert any(flag["kind"] == "close_call" for flag in slot["flags"])


def test_bif_matching_the_keeper_runtime_beats_a_wider_mismatched_one():
    keeper = _video("v1", "movie", duration=3600.0, accessories=[
        _accessory("f1", "movie", ".bif", "bif", size=100),
    ])
    copy_one = _video("v2", "movie(1)", duration=1800.0, accessories=[
        _accessory("f2", "movie(1)", ".bif", "bif", size=200),
    ])
    # f2 has wider frames but only covers half the keeper's runtime.
    info = {
        "f1": {"frame_count": 360, "width": 180, "interval_seconds": 10},
        "f2": {"frame_count": 180, "width": 320, "interval_seconds": 10},
    }

    slots = duplicate_slots.resolve_group_slots(
        [keeper, copy_one],
        keeper,
        bif_info=lambda path: info["f1" if path.endswith("movie.bif") else "f2"],
    )
    slot = _slot(slots, "bif:.bif")

    assert slot["winner_file_id"] == "f1"
    assert slot["borrowed"] is False


def test_unknown_sidecars_are_left_alone_and_flagged():
    keeper = _video("v1", "movie", accessories=[
        _accessory("u1", "movie", ".weird", "unknown", size=10),
    ])
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("u2", "movie(1)", ".weird", "unknown", size=20),
    ])

    slots = duplicate_slots.resolve_group_slots([keeper, copy_one], keeper)
    slot = _slot(slots, "unknown:.weird")

    assert slot["winner_file_id"] == ""
    assert slot["loser_file_ids"] == []
    assert slot["needs_review"] is True
    assert any(flag["kind"] == "unknown_role" for flag in slot["flags"])


def test_summary_reports_the_mix_of_source_copies():
    keeper = _video("v1", "movie", accessories=[
        _accessory("p1", "movie", "-poster.jpg", "poster", size=100),
    ])
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("p2", "movie(1)", "-poster.jpg", "poster", size=90),
        _accessory("b2", "movie(1)", "-background.png", "background", size=90),
    ])

    def probe(path):
        return (
            {"width": 100, "height": 100}
            if path.endswith("movie-poster.jpg")
            else {"width": 4000, "height": 4000}
        )

    slots = duplicate_slots.resolve_group_slots([keeper, copy_one], keeper, probe_image=probe)
    summary = duplicate_slots.slot_summary(slots)

    assert summary["slot_count"] == 2
    # Both slots are won by the losing copy, so both are borrowed.
    assert summary["borrowed_count"] == 2
    assert summary["source_video_ids"] == ["v2"]


def test_settings_can_widen_the_subtitle_tie_margin():
    keeper = _video("v1", "movie", accessories=[
        _accessory("s1", "movie", ".eng.srt", "subtitle", size=100),
    ])
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("s2", "movie(1)", ".eng.srt", "subtitle", size=200),
    ])
    coverage = {"s1": _quality(80.0, last=100.0), "s2": _quality(90.0, last=100.0)}

    def analyze(path, duration):
        return coverage["s1" if path.endswith("movie.eng.srt") else "s2"]

    tight = duplicate_slots.resolve_group_slots(
        [keeper, copy_one], keeper, analyze_subtitle=analyze
    )
    # 10 points apart: not close by default (8), close once the margin is 20.
    assert _slot(tight, "subtitle:.eng.srt")["needs_review"] is False

    loose = duplicate_slots.resolve_group_slots(
        [keeper, copy_one],
        keeper,
        {"duplicate_subtitle_close_points": 20},
        analyze_subtitle=analyze,
    )
    assert _slot(loose, "subtitle:.eng.srt")["needs_review"] is True


def test_unreadable_subtitles_are_not_borrowed_on_file_size_alone():
    """Borrowing needs evidence; a bigger unparseable file is not evidence."""
    keeper = _video("v1", "movie", accessories=[
        _accessory("s1", "movie", ".eng.srt", "subtitle", size=6),
    ])
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("s2", "movie(1)", ".eng.srt", "subtitle", size=900),
    ])

    slots = duplicate_slots.resolve_group_slots(
        [keeper, copy_one],
        keeper,
        # No usable timestamps in either file.
        analyze_subtitle=lambda path, duration: _quality(None, status="invalid"),
    )
    slot = _slot(slots, "subtitle:.eng.srt")

    assert slot["winner_file_id"] == "s1"
    assert slot["borrowed"] is False
    assert slot["needs_review"] is True
    assert any(flag["kind"] == "no_comparable_signal" for flag in slot["flags"])


def test_unmeasurable_images_are_not_borrowed_on_file_size_alone():
    keeper = _video("v1", "movie", accessories=[
        _accessory("p1", "movie", "-poster.jpg", "poster", size=10),
    ])
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("p2", "movie(1)", "-poster.jpg", "poster", size=5000),
    ])

    slots = duplicate_slots.resolve_group_slots(
        [keeper, copy_one], keeper, probe_image=lambda path: None
    )
    slot = _slot(slots, "poster:-poster.jpg")

    assert slot["winner_file_id"] == "p1"
    assert slot["borrowed"] is False
    assert any(flag["kind"] == "no_comparable_signal" for flag in slot["flags"])


def test_a_measurable_candidate_still_wins_over_an_unreadable_one():
    keeper = _video("v1", "movie", accessories=[
        _accessory("s1", "movie", ".eng.srt", "subtitle", size=100),
    ])
    copy_one = _video("v2", "movie(1)", accessories=[
        _accessory("s2", "movie(1)", ".eng.srt", "subtitle", size=200),
    ])
    results = {
        "s1": _quality(None, status="invalid"),
        "s2": _quality(96.0, status="complete", last=3400.0),
    }

    slots = duplicate_slots.resolve_group_slots(
        [keeper, copy_one],
        keeper,
        analyze_subtitle=lambda path, duration: results[
            "s1" if path.endswith("movie.eng.srt") else "s2"
        ],
    )
    slot = _slot(slots, "subtitle:.eng.srt")

    assert slot["winner_file_id"] == "s2"
    assert slot["borrowed"] is True
