from app.utils import (
    parse_int_list,
    choose_numeric,
    resolve_case_insensitive,
    path_is_under,
    find_background_image,
)
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


def test_path_is_under_rejects_prefix_sibling(tmp_path):
    root = tmp_path / "library"
    sibling = tmp_path / "library2"
    root.mkdir()
    sibling.mkdir()

    assert path_is_under(str(root / "video.mp4"), str(root))
    assert not path_is_under(str(sibling / "video.mp4"), str(root))


def test_find_background_image_matches_user_style_video_specific_name(tmp_path):
    stem = "Bratty MILF - 2025-11-06 - Working For Stepmom - S12E9 [WEBDL-2160p]"
    video = tmp_path / f"{stem}.mp4"
    background = tmp_path / f"{stem}-background.png"
    video.write_bytes(b"video")
    background.write_bytes(b"image")

    assert find_background_image(str(video)) == str(background)


def test_find_background_image_matches_library_manager_background_names(tmp_path):
    cases = [
        ("movie.mp4", "background.jpg"),
        ("movie.mp4", "backdrop.webp"),
        ("movie.mp4", "fanart-1.png"),
        ("movie.mp4", "art2.tbn"),
        ("movie.mp4", "movie-fanart.jpg"),
        ("Movie.mkv", "Movie-BackDrop-2.JPEG"),
    ]

    for index, (video_name, image_name) in enumerate(cases):
        directory = tmp_path / str(index)
        directory.mkdir()
        video = directory / video_name
        image = directory / image_name
        video.write_bytes(b"video")
        image.write_bytes(b"image")

        assert find_background_image(str(video)) == str(image)


def test_find_background_image_prefers_video_specific_over_folder_level(tmp_path):
    video = tmp_path / "Movie.mkv"
    generic = tmp_path / "background.jpg"
    specific = tmp_path / "Movie-fanart.jpg"
    video.write_bytes(b"video")
    generic.write_bytes(b"generic")
    specific.write_bytes(b"specific")

    assert find_background_image(str(video)) == str(specific)


def test_find_background_image_ignores_non_background_artwork(tmp_path):
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    for name in [
        "Movie-poster.jpg",
        "Movie-cover.png",
        "folder.jpg",
        "thumb.webp",
        "Movie-landscape.jpeg",
        "clearart.png",
    ]:
        (tmp_path / name).write_bytes(b"image")

    assert find_background_image(str(video)) is None
