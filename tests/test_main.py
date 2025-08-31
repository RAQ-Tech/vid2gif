import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from app.main import parse_int_list, build_segments, DEFAULTS


def test_parse_int_list_filters_non_ints():
    assert parse_int_list("1,2, x , 3") == [1, 2, 3]


def test_build_segments_generates_ranges():
    cfg = dict(DEFAULTS)
    cfg["percent_points"] = parse_int_list(DEFAULTS["percent_points"])
    segs = build_segments(100.0, cfg)
    assert isinstance(segs, list) and segs
    first = segs[0]
    assert first["end"] > first["start"]