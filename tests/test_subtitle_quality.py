from app import subtitle_quality


def _write_srt(path, final_start, final_end):
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nOpening line\n\n"
        f"2\n{final_start} --> {final_end}\nFinal line\n",
        encoding="utf-8",
    )
    return path


def test_srt_timestamp_coverage_distinguishes_truncated_and_complete_files(tmp_path):
    incomplete = _write_srt(
        tmp_path / "incomplete.srt",
        "00:27:14,220",
        "00:27:14,540",
    )
    complete = _write_srt(
        tmp_path / "complete.srt",
        "00:41:18,160",
        "00:41:20,560",
    )

    incomplete_quality = subtitle_quality.analyze_srt(incomplete, 41.5 * 60)
    complete_quality = subtitle_quality.analyze_srt(complete, 41.5 * 60)

    assert incomplete_quality["status"] == "likely_incomplete"
    assert incomplete_quality["last_timestamp_label"] == "27:15"
    assert incomplete_quality["coverage_percent"] == 65.6
    assert complete_quality["status"] == "complete"
    assert complete_quality["last_timestamp_label"] == "41:21"
    assert complete_quality["coverage_percent"] == 99.6


def test_short_dialogue_gap_is_not_automatically_marked_incomplete(tmp_path):
    subtitle = _write_srt(
        tmp_path / "credits-gap.srt",
        "01:27:57,000",
        "01:28:00,000",
    )

    quality = subtitle_quality.analyze_srt(subtitle, 90 * 60)

    assert quality["status"] == "complete"
    assert quality["tail_gap_seconds"] == 120
