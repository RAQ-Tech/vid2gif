"""Edge cases in the analysis that decides a subtitle's fate.

`analyze_srt` produces the verdict the subtitles tab acts on: `likely_incomplete`
is what gets offered for quarantine or deletion. Getting it wrong in either
direction costs the operator something -- a good subtitle destroyed, or a
truncated one left in place -- so the inputs that are easy to mishandle are
worth pinning down: an encoding the app does not expect, a file too big to
inspect, a runtime it could not determine.
"""

import json
import subprocess

import pytest

from app import subtitle_quality


def _srt(final_start, final_end):
    return f"1\n00:00:01,000 --> 00:00:02,000\nOpening\n\n2\n{final_start} --> {final_end}\nFinal\n"


# --- reading the file -------------------------------------------------------


def test_a_utf16_subtitle_is_read_rather_than_called_invalid(tmp_path):
    """UTF-16 SRTs are common in the wild.

    If the decode failed, a perfectly good subtitle would be reported as having
    no usable timestamps -- and an "invalid" verdict is one the operator is
    invited to delete.
    """
    path = tmp_path / "utf16.srt"
    path.write_bytes(_srt("00:41:18,160", "00:41:20,560").encode("utf-16"))

    quality = subtitle_quality.analyze_srt(path, 41.5 * 60)

    assert quality["status"] == "complete"
    assert quality["cue_count"] == 2
    assert quality["last_timestamp_label"] == "41:21"


def test_a_utf8_bom_does_not_break_the_first_cue(tmp_path):
    path = tmp_path / "bom.srt"
    path.write_bytes(b"\xef\xbb\xbf" + _srt("00:41:18,160", "00:41:20,560").encode("utf-8"))

    quality = subtitle_quality.analyze_srt(path, 41.5 * 60)

    assert quality["status"] == "complete"
    assert quality["cue_count"] == 2


def test_a_missing_file_is_unreadable_not_invalid(tmp_path):
    """The distinction matters: unreadable is a failure to inspect, not a verdict."""
    quality = subtitle_quality.analyze_srt(tmp_path / "nope.srt", 3600)

    assert quality["status"] == "unreadable"
    assert quality["cue_count"] == 0
    assert quality["coverage_percent"] is None


def test_an_oversized_subtitle_is_refused_rather_than_loaded(tmp_path):
    """A 16 MB ceiling stops a malformed file from being read into memory."""
    path = tmp_path / "huge.srt"
    path.write_bytes(b"x" * (subtitle_quality.MAX_SUBTITLE_BYTES + 10))

    quality = subtitle_quality.analyze_srt(path, 3600)

    assert quality["status"] == "unreadable"
    assert "too large" in quality["label"]


def test_a_file_with_no_timestamps_is_invalid(tmp_path):
    path = tmp_path / "prose.srt"
    path.write_text("this is not a subtitle file at all\n", encoding="utf-8")

    quality = subtitle_quality.analyze_srt(path, 3600)

    assert quality["status"] == "invalid"
    assert quality["cue_count"] == 0


# --- deciding the verdict ---------------------------------------------------


def test_an_unknown_runtime_yields_no_verdict(tmp_path):
    """Without the video's length there is no coverage to judge.

    Reporting a guess here would put a subtitle on the deletion list on the
    strength of a number the app does not have.
    """
    path = tmp_path / "sub.srt"
    path.write_text(_srt("00:27:14,220", "00:27:14,540"), encoding="utf-8")

    for duration in (None, 0, -5, "not a number"):
        quality = subtitle_quality.analyze_srt(path, duration)
        assert quality["status"] == "duration_unknown", duration
        assert quality["coverage_percent"] is None
        assert "runtime unavailable" in quality["label"]


def test_a_subtitle_running_past_the_video_is_flagged_for_review(tmp_path):
    """Almost certainly the wrong file for this video -- but not "incomplete"."""
    path = tmp_path / "overrun.srt"
    path.write_text(_srt("01:30:00,000", "01:30:05,000"), encoding="utf-8")

    quality = subtitle_quality.analyze_srt(path, 40 * 60)

    assert quality["status"] == "timing_review"
    assert "exceeds video runtime" in quality["label"]


def test_a_small_overrun_is_tolerated(tmp_path):
    """Two minutes of slack, so a subtitle a little long is not flagged."""
    path = tmp_path / "slightly-over.srt"
    path.write_text(_srt("01:00:30,000", "01:00:31,000"), encoding="utf-8")

    quality = subtitle_quality.analyze_srt(path, 60 * 60)

    assert quality["status"] == "complete"


def test_the_middle_band_asks_for_review_instead_of_deciding(tmp_path):
    """Between 80% and 90% coverage is uncertain, and uncertainty is review-only.

    This is the band that must never be auto-deleted: it might be a subtitle
    that simply stops before a long silent credits roll.
    """
    # Ends at 85% of a two-hour video, so the gap is well past the review
    # threshold but the ratio is above the incomplete one.
    path = tmp_path / "middle.srt"
    path.write_text(_srt("01:42:00,000", "01:42:02,000"), encoding="utf-8")

    quality = subtitle_quality.analyze_srt(path, 120 * 60)

    assert quality["status"] == "coverage_review"
    assert "Review coverage" in quality["label"]


def test_format_timestamp_returns_empty_for_nonsense(tmp_path):
    assert subtitle_quality.format_timestamp(None) == ""
    assert subtitle_quality.format_timestamp("abc") == ""
    assert subtitle_quality.format_timestamp(0) == "0:00"
    assert subtitle_quality.format_timestamp(59) == "0:59"
    assert subtitle_quality.format_timestamp(3661) == "1:01:01"
    # Negative values are clamped rather than rendered as nonsense.
    assert subtitle_quality.format_timestamp(-10) == "0:00"


# --- probing the video's runtime --------------------------------------------


def test_probe_returns_nothing_when_the_file_is_absent(tmp_path):
    assert subtitle_quality.probe_media_duration(str(tmp_path / "missing.mkv")) is None
    assert subtitle_quality.probe_media_duration("") is None


def test_probe_reads_the_duration_ffprobe_reports(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"video")

    def fake_run(args, **kwargs):
        assert "ffprobe" in args[0]
        return subprocess.CompletedProcess(args, 0, json.dumps({"format": {"duration": "2493.44"}}), "")

    monkeypatch.setattr(subtitle_quality.subprocess, "run", fake_run)

    assert subtitle_quality.probe_media_duration(str(video)) == pytest.approx(2493.44)


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 1, "", "boom"),  # ffprobe failed
        subprocess.CompletedProcess([], 0, "not json", ""),  # unparseable output
        subprocess.CompletedProcess([], 0, json.dumps({}), ""),  # no format block
        subprocess.CompletedProcess([], 0, json.dumps({"format": {"duration": "0"}}), ""),  # zero
        subprocess.CompletedProcess([], 0, json.dumps({"format": {"duration": "N/A"}}), ""),  # not a number
    ],
)
def test_probe_returns_nothing_rather_than_a_bad_duration(tmp_path, monkeypatch, outcome):
    """Every one of these would otherwise become a coverage percentage."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr(subtitle_quality.subprocess, "run", lambda *a, **k: outcome)

    assert subtitle_quality.probe_media_duration(str(video)) is None


def test_probe_survives_ffprobe_being_absent_or_hanging(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"video")

    for failure in (OSError("ffprobe not found"), subprocess.TimeoutExpired("ffprobe", 30)):

        def raise_it(*_args, _failure=failure, **_kwargs):
            raise _failure

        monkeypatch.setattr(subtitle_quality.subprocess, "run", raise_it)
        assert subtitle_quality.probe_media_duration(str(video)) is None


# --- choosing between two subtitles -----------------------------------------


def _candidate(status, ratio=None, size=0):
    return {"subtitle_quality": {"status": status, "coverage_ratio": ratio}, "size_bytes": size}


def test_a_complete_subtitle_beats_a_broken_one(tmp_path):
    winner = subtitle_quality.clear_quality_winner([_candidate("likely_incomplete", 0.5), _candidate("complete", 0.99)])

    assert winner is not None
    assert winner["subtitle_quality"]["status"] == "complete"


def test_two_similar_subtitles_produce_no_winner(tmp_path):
    """A near-tie is exactly when the app should stop deciding and ask."""
    assert subtitle_quality.clear_quality_winner([_candidate("complete", 0.97), _candidate("complete", 0.95)]) is None


def test_a_clear_coverage_margin_does_produce_a_winner(tmp_path):
    winner = subtitle_quality.clear_quality_winner(
        [_candidate("coverage_review", 0.80), _candidate("coverage_review", 0.95)]
    )

    assert winner is not None
    assert winner["subtitle_quality"]["coverage_ratio"] == 0.95


def test_a_single_candidate_is_returned_and_an_empty_list_is_not(tmp_path):
    only = _candidate("complete", 0.99)
    assert subtitle_quality.clear_quality_winner([only]) is only
    assert subtitle_quality.clear_quality_winner([]) is None
    assert subtitle_quality.clear_quality_winner(None) is None
