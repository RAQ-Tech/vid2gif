
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


# ---------------------------------------------------------------------------
# Error paths.
#
# The tests above prove the module installs and moves files correctly. These
# cover what it does when something is wrong: a missing file, a destination
# that is not what was promised, a syscall that fails part way through. Those
# branches decide whether a user's library survives a bad day, and they were
# the least-covered lines in the codebase.
# ---------------------------------------------------------------------------


def _raise_oserror(message):
    def fail(*args, **kwargs):
        raise OSError(message)
    return fail


def test_identity_refuses_paths_it_cannot_vouch_for(tmp_path):
    """Anything that is not a plain regular file inside the root gets no identity."""
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "inside.mkv"
    inside.write_bytes(b"data")
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"data")

    assert file_safety.regular_file_identity("") is None
    assert file_safety.regular_file_identity(str(inside), root=str(root))
    # Outside the root entirely.
    assert file_safety.regular_file_identity(str(outside), root=str(root)) is None
    # Right place, wrong kind of thing.
    assert file_safety.regular_file_identity(str(root), root=str(root)) is None
    assert file_safety.regular_file_identity(str(root / "missing.mkv"), root=str(root)) is None
    # Right place, disallowed extension.
    assert file_safety.regular_file_identity(str(inside), allowed_extensions={".gif"}) is None
    assert file_safety.regular_file_identity(str(inside), allowed_extensions={".mkv"})


def test_path_has_symlink_component_edges(tmp_path):
    """An empty path is treated as unsafe; a clean path below the root is not."""
    assert file_safety.path_has_symlink_component("") is True
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert file_safety.path_has_symlink_component(str(nested), root=str(tmp_path)) is False
    assert file_safety.path_has_symlink_component(str(nested / "c.mkv"), root=str(tmp_path)) is False


def test_target_state_rejects_every_unusable_destination(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(file_safety.FileSafetyError, match="Destination path is missing"):
        file_safety.target_state("")

    with pytest.raises(file_safety.FileSafetyError, match="Destination directory is unsafe"):
        file_safety.target_state(str(root / "no-such-dir" / "poster.gif"))

    # A destination whose directory sits outside the root is rejected while
    # checking the directory, before containment is even considered.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(file_safety.FileSafetyError, match="Destination directory is unsafe"):
        file_safety.target_state(str(elsewhere / "poster.gif"), root=str(root))

    # Containment is what catches a path whose directory is inside the root but
    # which escapes upward out of it.
    with pytest.raises(file_safety.FileSafetyError, match="outside the allowed root"):
        file_safety.target_state(str(root / ".."), root=str(root))

    # A directory sitting where the file should go is not something to replace.
    (root / "poster.gif").mkdir()
    with pytest.raises(file_safety.FileSafetyError, match="not a regular file"):
        file_safety.target_state(str(root / "poster.gif"), root=str(root))


def test_target_state_matches_rejects_malformed_expectations(tmp_path):
    target = tmp_path / "poster.gif"
    assert file_safety.target_state_matches(str(target), None) is False
    assert file_safety.target_state_matches(str(target), {}) is False
    assert file_safety.target_state_matches(str(target), {"identity": None}) is False
    # "I expect nothing here" holds only while nothing is there.
    assert file_safety.target_state_matches(str(target), {"exists": False}) is True
    target.write_bytes(b"x")
    assert file_safety.target_state_matches(str(target), {"exists": False}) is False


def test_fsync_directory_survives_a_filesystem_that_refuses(tmp_path, monkeypatch):
    """Durability is best effort: a failing fsync must not break the install.

    Directories cannot be opened for fsync on every platform, so this drives
    the branch directly rather than depending on the host.
    """
    probe = tmp_path / "probe"
    probe.write_bytes(b"x")
    closed = []

    real_open = file_safety.os.open
    real_close = file_safety.os.close

    def open_probe(path, flags):
        return real_open(str(probe), file_safety.os.O_RDONLY)

    def record_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(file_safety.os, "open", open_probe)
    monkeypatch.setattr(file_safety.os, "fsync", _raise_oserror("no fsync here"))
    monkeypatch.setattr(file_safety.os, "close", record_close)

    file_safety._fsync_directory(str(tmp_path))

    assert closed, "the descriptor must be closed even when fsync fails"


def test_fsync_directory_ignores_a_directory_it_cannot_open(tmp_path, monkeypatch):
    monkeypatch.setattr(file_safety.os, "open", _raise_oserror("cannot open directory"))
    file_safety._fsync_directory(str(tmp_path))  # must not raise


def test_install_refuses_a_staged_file_that_is_missing_or_changed(tmp_path):
    source = tmp_path / "staged.gif"
    target = tmp_path / "poster.gif"

    with pytest.raises(file_safety.FileSafetyError, match="Staged output is missing or unsafe"):
        file_safety.atomic_install_file(str(source), str(target))

    source.write_bytes(b"first")
    stale = file_safety.regular_file_identity(str(source))
    source.write_bytes(b"second and longer")

    with pytest.raises(file_safety.FileSafetyError, match="Staged output changed before installation"):
        file_safety.atomic_install_file(str(source), str(target), expected_source=stale)

    assert not target.exists(), "nothing may be installed after a refusal"


def test_install_captures_destination_state_when_none_is_supplied(tmp_path):
    source = tmp_path / "staged.gif"
    source.write_bytes(b"payload")
    target = tmp_path / "poster.gif"

    installed = file_safety.atomic_install_file(str(source), str(target))

    assert target.read_bytes() == b"payload"
    assert installed["size"] == len(b"payload")


def test_install_reports_a_destination_that_vanishes_before_the_copy(tmp_path, monkeypatch):
    source = tmp_path / "staged.gif"
    source.write_bytes(b"payload")
    target = tmp_path / "poster.gif"
    target.write_bytes(b"old")
    expected_target = file_safety.target_state(str(target))

    # Let the pre-flight check pass, so the failure lands on the stat() that
    # reads the mode to preserve rather than on an earlier comparison.
    monkeypatch.setattr(file_safety, "target_state_matches", lambda *a, **kw: True)

    real_stat = file_safety.os.stat

    def stat_fails_for_target(path, *args, **kwargs):
        if str(path) == str(target):
            raise OSError("gone")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(file_safety.os, "stat", stat_fails_for_target)

    with pytest.raises(file_safety.FileSafetyError, match="Destination changed before installation"):
        file_safety.atomic_install_file(
            str(source), str(target), expected_target=expected_target
        )


def test_install_uses_chmod_when_the_platform_has_no_fchmod(tmp_path, monkeypatch):
    """Some platforms lack fchmod; the installed file still needs its mode."""
    monkeypatch.delattr(file_safety.os, "fchmod", raising=False)
    source = tmp_path / "staged.gif"
    source.write_bytes(b"payload")
    target = tmp_path / "poster.gif"

    chmodded = []
    real_chmod = file_safety.os.chmod

    def record_chmod(path, mode, **kwargs):
        chmodded.append(mode)
        real_chmod(path, mode)

    monkeypatch.setattr(file_safety.os, "chmod", record_chmod)

    file_safety.atomic_install_file(str(source), str(target))

    assert chmodded, "chmod fallback did not run"
    assert target.read_bytes() == b"payload"


def test_install_aborts_when_the_source_changes_mid_copy(tmp_path, monkeypatch):
    source = tmp_path / "staged.gif"
    source.write_bytes(b"payload")
    target = tmp_path / "poster.gif"
    expected_source = file_safety.regular_file_identity(str(source))

    looks = []
    real_matches = file_safety.identity_matches

    def changed_on_the_second_look(path, expected, **kwargs):
        looks.append(str(path))
        if len(looks) > 1 and str(path) == str(source):
            return False
        return real_matches(path, expected, **kwargs)

    monkeypatch.setattr(file_safety, "identity_matches", changed_on_the_second_look)

    with pytest.raises(file_safety.FileSafetyError, match="Staged output changed during installation"):
        file_safety.atomic_install_file(
            str(source), str(target), expected_source=expected_source
        )

    assert not target.exists()
    leftovers = [p.name for p in tmp_path.iterdir() if ".vid2gif-" in p.name]
    assert leftovers == [], f"temporary file left behind: {leftovers}"


def test_install_aborts_when_the_destination_appears_mid_copy(tmp_path, monkeypatch):
    """The race the whole design exists to lose safely."""
    source = tmp_path / "staged.gif"
    source.write_bytes(b"payload")
    target = tmp_path / "poster.gif"
    expected_target = file_safety.target_state(str(target))

    checks = []

    def appears_after_the_first_check(path, expected, **kwargs):
        checks.append(str(path))
        return len(checks) < 2

    monkeypatch.setattr(file_safety, "target_state_matches", appears_after_the_first_check)

    with pytest.raises(file_safety.FileSafetyError, match="Destination changed during installation"):
        file_safety.atomic_install_file(
            str(source), str(target), expected_target=expected_target
        )

    assert not target.exists()


def test_install_verifies_what_actually_landed(tmp_path, monkeypatch):
    source = tmp_path / "staged.gif"
    source.write_bytes(b"payload")
    target = tmp_path / "poster.gif"

    real_identity = file_safety.regular_file_identity

    def wrong_size_for_installed(path, **kwargs):
        identity = real_identity(path, **kwargs)
        if identity and str(path) == str(target):
            identity = dict(identity, size=identity["size"] + 1)
        return identity

    monkeypatch.setattr(file_safety, "regular_file_identity", wrong_size_for_installed)

    with pytest.raises(file_safety.FileSafetyError, match="could not be verified"):
        file_safety.atomic_install_file(str(source), str(target))


def test_install_closes_its_descriptor_and_clears_its_temp_file(tmp_path, monkeypatch):
    source = tmp_path / "staged.gif"
    source.write_bytes(b"payload")
    target = tmp_path / "poster.gif"

    monkeypatch.setattr(file_safety.os, "fdopen", _raise_oserror("no stream for you"))

    with pytest.raises(OSError, match="no stream for you"):
        file_safety.atomic_install_file(str(source), str(target))

    leftovers = [p.name for p in tmp_path.iterdir() if ".vid2gif-" in p.name]
    assert leftovers == [], f"temporary file left behind: {leftovers}"


def test_install_tolerates_a_temp_file_it_cannot_remove(tmp_path, monkeypatch):
    """Cleanup is best effort; failing to tidy up must not mask a good install."""
    source = tmp_path / "staged.gif"
    source.write_bytes(b"payload")
    target = tmp_path / "poster.gif"

    monkeypatch.setattr(file_safety.os, "remove", _raise_oserror("locked"))

    installed = file_safety.atomic_install_file(str(source), str(target))

    assert installed
    assert target.read_bytes() == b"payload"


def test_quarantine_refuses_a_source_that_is_missing_or_changed(tmp_path):
    source = tmp_path / "video.mkv"
    destination = tmp_path / "quarantine" / "video.mkv"
    destination.parent.mkdir()

    with pytest.raises(file_safety.FileSafetyError, match="Source is missing or unsafe"):
        file_safety.atomic_quarantine_file(str(source), str(destination))

    source.write_bytes(b"first")
    stale = file_safety.regular_file_identity(str(source))
    source.write_bytes(b"second and longer")

    with pytest.raises(file_safety.FileSafetyError, match="Source changed before quarantine"):
        file_safety.atomic_quarantine_file(str(source), str(destination), expected_source=stale)

    assert source.exists(), "the source must survive a refusal"
    assert not destination.exists()


def test_quarantine_refuses_an_unsafe_destination(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    source = root / "video.mkv"
    source.write_bytes(b"data")

    # Parent directory does not exist.
    with pytest.raises(file_safety.FileSafetyError, match="destination is unsafe"):
        file_safety.atomic_quarantine_file(str(source), str(root / "missing" / "video.mkv"))

    # Destination outside the allowed root.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(file_safety.FileSafetyError, match="destination is unsafe"):
        file_safety.atomic_quarantine_file(
            str(source), str(elsewhere / "video.mkv"), root=str(root)
        )

    assert source.exists()


def test_quarantine_rolls_back_its_link_when_the_source_cannot_be_confirmed(tmp_path, monkeypatch):
    """If the link and the source stop agreeing, keep the source.

    This is the interrupted-move case: a link exists but the source is no
    longer provably the file that was checked, so unlinking it would destroy
    something that was never verified.
    """
    source = tmp_path / "video.mkv"
    source.write_bytes(b"data")
    destination = tmp_path / "quarantine" / "video.mkv"
    destination.parent.mkdir()

    monkeypatch.setattr(file_safety.os.path, "samefile", _raise_oserror("cannot compare"))

    with pytest.raises(file_safety.FileSafetyError, match="Source changed during quarantine"):
        file_safety.atomic_quarantine_file(str(source), str(destination))

    assert source.exists(), "the source must never be removed after a failure"


def test_quarantine_keeps_a_destination_rollback_cannot_confirm(tmp_path, monkeypatch):
    """Rollback removes only a destination it can prove is the same file.

    When that cannot be established, leaving two names is the safe outcome;
    deleting the wrong one is not recoverable.
    """
    source = tmp_path / "video.mkv"
    source.write_bytes(b"data")
    destination = tmp_path / "quarantine" / "video.mkv"
    destination.parent.mkdir()

    calls = []

    def samefile(a, b):
        calls.append((str(a), str(b)))
        if len(calls) == 1:
            raise OSError("cannot compare")
        return False

    monkeypatch.setattr(file_safety.os.path, "samefile", samefile)

    with pytest.raises(file_safety.FileSafetyError):
        file_safety.atomic_quarantine_file(str(source), str(destination))

    assert source.exists()
    assert destination.exists(), "an unconfirmed link is kept, not guessed at"


def test_quarantine_removes_the_link_it_made_when_the_move_fails(tmp_path, monkeypatch):
    """The clean rollback: undo the half-done move, leave the library as it was.

    The link is created before the source is unlinked. If the check between
    those two steps fails, the link must be taken back off -- otherwise a failed
    quarantine silently leaves a duplicate behind.
    """
    source = tmp_path / "video.mkv"
    source.write_bytes(b"data")
    destination = tmp_path / "quarantine" / "video.mkv"
    destination.parent.mkdir()

    real_identity = file_safety.regular_file_identity
    looks = []

    def source_looks_different_after_linking(path, **kwargs):
        identity = real_identity(path, **kwargs)
        if str(path) == str(source):
            looks.append(str(path))
            if len(looks) > 1 and identity:
                identity = dict(identity, size=identity["size"] + 1)
        return identity

    monkeypatch.setattr(file_safety, "regular_file_identity", source_looks_different_after_linking)

    with pytest.raises(file_safety.FileSafetyError, match="Source changed during quarantine"):
        file_safety.atomic_quarantine_file(str(source), str(destination))

    assert source.exists(), "the original must survive"
    assert not destination.exists(), "the link created by the failed move must be removed"


def test_move_no_overwrite_is_the_same_durable_operation(tmp_path):
    source = tmp_path / "poster.jpg"
    source.write_bytes(b"image")
    destination = tmp_path / "poster-backup.jpg"

    identity = file_safety.atomic_move_file_no_overwrite(str(source), str(destination))

    assert identity
    assert destination.read_bytes() == b"image"
    assert not source.exists()

    source.write_bytes(b"image again")
    with pytest.raises(FileExistsError):
        file_safety.atomic_move_file_no_overwrite(str(source), str(destination))
    assert source.exists()
