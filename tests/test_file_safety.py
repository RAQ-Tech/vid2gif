import os

import pytest

from app import file_safety


def test_atomic_install_replaces_only_after_complete_same_directory_copy(tmp_path):
    root = tmp_path / "library"
    staged_root = tmp_path / "state"
    root.mkdir()
    staged_root.mkdir()
    target = root / "poster.gif"
    source = staged_root / "poster.gif"
    target.write_bytes(b"old-complete-output")
    source.write_bytes(b"GIF89a-new-complete-output")

    expected_target = file_safety.target_state(str(target), root=str(root))
    expected_source = file_safety.regular_file_identity(str(source))
    installed = file_safety.atomic_install_file(
        str(source),
        str(target),
        root=str(root),
        expected_source=expected_source,
        expected_target=expected_target,
    )

    assert target.read_bytes() == source.read_bytes()
    assert installed["size"] == len(source.read_bytes())
    assert not list(root.glob(".poster.gif.vid2gif-*.tmp"))


def test_atomic_install_preserves_existing_output_when_replace_fails(tmp_path, monkeypatch):
    root = tmp_path / "library"
    staged_root = tmp_path / "state"
    root.mkdir()
    staged_root.mkdir()
    target = root / "poster.gif"
    source = staged_root / "poster.gif"
    target.write_bytes(b"old-output")
    source.write_bytes(b"GIF89a-new-output")
    expected_target = file_safety.target_state(str(target), root=str(root))

    monkeypatch.setattr(
        file_safety.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated crash boundary")),
    )

    with pytest.raises(OSError, match="simulated crash boundary"):
        file_safety.atomic_install_file(
            str(source),
            str(target),
            root=str(root),
            expected_target=expected_target,
        )

    assert target.read_bytes() == b"old-output"
    assert not list(root.glob(".poster.gif.vid2gif-*.tmp"))


def test_atomic_install_refuses_concurrent_destination_change(tmp_path):
    root = tmp_path / "library"
    staged_root = tmp_path / "state"
    root.mkdir()
    staged_root.mkdir()
    target = root / "poster.gif"
    source = staged_root / "poster.gif"
    target.write_bytes(b"old-output")
    source.write_bytes(b"GIF89a-new-output")
    expected_target = file_safety.target_state(str(target), root=str(root))
    target.write_bytes(b"other-container-output")

    with pytest.raises(file_safety.FileSafetyError, match="Destination changed"):
        file_safety.atomic_install_file(
            str(source),
            str(target),
            root=str(root),
            expected_target=expected_target,
        )

    assert target.read_bytes() == b"other-container-output"


def test_atomic_install_does_not_overwrite_destination_created_at_install(tmp_path, monkeypatch):
    root = tmp_path / "library"
    staged_root = tmp_path / "state"
    root.mkdir()
    staged_root.mkdir()
    target = root / "poster.gif"
    source = staged_root / "poster.gif"
    source.write_bytes(b"GIF89a-new-output")
    expected_target = file_safety.target_state(str(target), root=str(root))
    original_link = file_safety.os.link

    def racing_link(staged, destination, **kwargs):
        target.write_bytes(b"other-container-output")
        return original_link(staged, destination, **kwargs)

    monkeypatch.setattr(file_safety.os, "link", racing_link)

    with pytest.raises(FileExistsError):
        file_safety.atomic_install_file(
            str(source),
            str(target),
            root=str(root),
            expected_target=expected_target,
        )

    assert target.read_bytes() == b"other-container-output"


def test_regular_file_identity_rejects_symlinked_path_component(tmp_path):
    root = tmp_path / "library"
    real = root / "real"
    real.mkdir(parents=True)
    video = real / "movie.mp4"
    video.write_bytes(b"video")
    linked = root / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    assert file_safety.regular_file_identity(
        str(linked / "movie.mp4"), root=str(root), allowed_extensions={".mp4"}
    ) is None


def test_target_state_rejects_symlink_destination(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    real = root / "real.gif"
    real.write_bytes(b"GIF89a")
    linked = root / "poster.gif"
    try:
        linked.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(file_safety.FileSafetyError, match="regular file"):
        file_safety.target_state(str(linked), root=str(root))


def test_atomic_quarantine_moves_complete_file_without_overwrite(tmp_path):
    root = tmp_path / "library"
    source_dir = root / "movies"
    quarantine = root / ".quarantine"
    source_dir.mkdir(parents=True)
    quarantine.mkdir()
    source = source_dir / "movie.mp4"
    destination = quarantine / "movie.mp4"
    source.write_bytes(b"complete-video")
    expected = file_safety.regular_file_identity(str(source), root=str(root))

    installed = file_safety.atomic_quarantine_file(
        str(source),
        str(destination),
        root=str(root),
        expected_source=expected,
    )

    assert not source.exists()
    assert destination.read_bytes() == b"complete-video"
    assert installed["size"] == len(b"complete-video")


def test_atomic_quarantine_refuses_existing_destination(tmp_path):
    root = tmp_path / "library"
    source_dir = root / "movies"
    quarantine = root / ".quarantine"
    source_dir.mkdir(parents=True)
    quarantine.mkdir()
    source = source_dir / "movie.mp4"
    destination = quarantine / "movie.mp4"
    source.write_bytes(b"source-video")
    destination.write_bytes(b"existing-video")

    with pytest.raises(FileExistsError, match="already exists"):
        file_safety.atomic_quarantine_file(
            str(source), str(destination), root=str(root)
        )

    assert source.read_bytes() == b"source-video"
    assert destination.read_bytes() == b"existing-video"
