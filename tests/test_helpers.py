import pytest

from app.utils import parse_int_list, choose_numeric, resolve_case_insensitive
from app.ffmpeg_utils import build_segments


def test_parse_int_list_basic():
    assert parse_int_list("1,2,3") == [1, 2, 3]


def test_parse_int_list_ignores_invalid_and_spaces():
    assert parse_int_list(" 1 , x, 2,,3 ") == [1, 2, 3]


def test_parse_int_list_empty_string():
    assert parse_int_list("") == []


def test_choose_numeric_preset():
    form = {"height_preset": "360"}
    assert choose_numeric(form, "height_preset", "height_custom", int, 480) == 360


def test_choose_numeric_custom():
    form = {"height_preset": "custom", "height_custom": "720"}
    assert choose_numeric(form, "height_preset", "height_custom", int, 480) == 720


def test_choose_numeric_legacy_field():
    form = {"height": "600"}
    assert choose_numeric(form, "height_preset", "height_custom", int, 480) == 600


def test_choose_numeric_defaults_on_invalid():
    form = {"height_preset": "custom", "height_custom": "abc"}
    assert choose_numeric(form, "height_preset", "height_custom", int, 480) == 480


def test_build_segments_basic():
    cfg = {
        "clip_len": 10.0,
        "start_buffer": 5.0,
        "end_buffer": 5.0,
        "abs_early": 15.0,
        "percent_points": [10, 90],
        "abs_late_from_end": 10.0,
    }
    segs = build_segments(100.0, cfg)
    assert segs == [
        {"start": 10.0, "end": 20.0},
        {"start": 15.0, "end": 25.0},
        {"start": 85.0, "end": 95.0},
    ]


def test_build_segments_fallback_when_no_points():
    cfg = {
        "clip_len": 2.0,
        "start_buffer": 0.0,
        "end_buffer": 0.0,
        "abs_early": 0.0,
        "percent_points": [],
        "abs_late_from_end": 0.0,
    }
    segs = build_segments(5.0, cfg)
    assert segs == [{"start": 0.0, "end": 2.0}]


def test_build_segments_deduplicates_close_points():
    cfg = {
        "clip_len": 10.0,
        "start_buffer": 0.0,
        "end_buffer": 0.0,
        "abs_early": 0.0,
        "percent_points": [10, 12],
        "abs_late_from_end": 0.0,
    }
    segs = build_segments(100.0, cfg)
    assert segs == [{"start": 10.0, "end": 20.0}]


def test_resolve_case_insensitive(tmp_path):
    base = tmp_path / 'Lib'
    (base / 'Sub').mkdir(parents=True)
    (base / 'Sub' / 'Video.MP4').write_text('x')
    path = str(base / 'sub' / 'video.mp4')
    resolved = resolve_case_insensitive(path)
    assert resolved == str(base / 'Sub' / 'Video.MP4')
