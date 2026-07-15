import os
import shutil
import stat
import uuid

from .utils import path_is_under


IDENTITY_KEYS = (
    "real_path",
    "size",
    "mtime_ns",
    "ctime_ns",
    "inode",
    "device",
)


class FileSafetyError(RuntimeError):
    pass


def path_has_symlink_component(path, *, root=None):
    """Return True if an existing path component at or below root is a symlink."""
    current = os.path.abspath(str(path or ""))
    if not current:
        return True
    boundary = os.path.abspath(root) if root else None
    boundary_key = os.path.normcase(boundary) if boundary else None
    while True:
        if os.path.islink(current):
            return True
        current_key = os.path.normcase(current)
        if boundary_key and current_key == boundary_key:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return bool(boundary_key)
        current = parent


def regular_file_identity(path, *, root=None, allowed_extensions=None):
    """Capture a regular, non-symlink file identity suitable for race checks."""
    path = str(path or "")
    if not path or path_has_symlink_component(path, root=root):
        return None
    if root and not path_is_under(path, root):
        return None
    if allowed_extensions is not None:
        extension = os.path.splitext(path)[1].lower()
        if extension not in allowed_extensions:
            return None
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(value.st_mode):
        return None
    return {
        "real_path": os.path.normcase(os.path.realpath(path)),
        "size": value.st_size,
        "mtime_ns": getattr(
            value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000)
        ),
        "ctime_ns": getattr(
            value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000)
        ),
        "inode": getattr(value, "st_ino", 0),
        "device": getattr(value, "st_dev", 0),
    }


def identity_matches(path, expected, *, root=None, allowed_extensions=None):
    current = regular_file_identity(
        path, root=root, allowed_extensions=allowed_extensions
    )
    keys = [key for key in IDENTITY_KEYS if isinstance(expected, dict) and key in expected]
    return bool(
        current
        and expected
        and len(keys) >= 2
        and all(current.get(key) == expected.get(key) for key in keys)
    )


def target_state(path, *, root=None):
    """Capture whether a destination exists without following symlinks."""
    path = str(path or "")
    if not path:
        raise FileSafetyError("Destination path is missing")
    parent = os.path.dirname(path)
    if (
        not parent
        or not os.path.isdir(parent)
        or path_has_symlink_component(parent, root=root)
    ):
        raise FileSafetyError("Destination directory is unsafe")
    if root and (not path_is_under(parent, root) or not path_is_under(path, root)):
        raise FileSafetyError("Destination is outside the allowed root")
    if not os.path.lexists(path):
        return {"exists": False, "identity": None}
    identity = regular_file_identity(path, root=root)
    if not identity:
        raise FileSafetyError("Destination is not a regular file")
    return {"exists": True, "identity": identity}


def target_state_matches(path, expected, *, root=None):
    if not isinstance(expected, dict) or "exists" not in expected:
        return False
    if not expected.get("exists"):
        return not os.path.lexists(path)
    return identity_matches(path, expected.get("identity"), root=root)


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_install_file(
    source,
    target,
    *,
    root=None,
    expected_source=None,
    expected_target=None,
    default_mode=0o664,
):
    """Durably install a complete file with an atomic same-directory replace.

    The destination is never streamed to directly. A complete temporary file is
    written and fsynced beside it, then installed with os.replace(). Optional
    identities make normal concurrent writers fail closed instead of being
    silently overwritten.
    """
    source_identity = regular_file_identity(source)
    if not source_identity:
        raise FileSafetyError("Staged output is missing or unsafe")
    if expected_source and not identity_matches(source, expected_source):
        raise FileSafetyError("Staged output changed before installation")

    if expected_target is None:
        expected_target = target_state(target, root=root)
    elif not target_state_matches(target, expected_target, root=root):
        raise FileSafetyError("Destination changed while work was queued")

    parent = os.path.dirname(target)
    basename = os.path.basename(target)
    tmp_path = os.path.join(
        parent, f".{basename}.vid2gif-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    )
    mode = default_mode
    if expected_target.get("exists"):
        try:
            mode = stat.S_IMODE(os.stat(target, follow_symlinks=False).st_mode)
        except OSError as exc:
            raise FileSafetyError("Destination changed before installation") from exc

    fd = None
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as output, open(source, "rb") as staged:
            fd = None
            shutil.copyfileobj(staged, output, length=1024 * 1024)
            output.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        if not hasattr(os, "fchmod"):
            os.chmod(tmp_path, mode)

        if expected_source and not identity_matches(source, expected_source):
            raise FileSafetyError("Staged output changed during installation")
        if not target_state_matches(target, expected_target, root=root):
            raise FileSafetyError("Destination changed during installation")

        if expected_target.get("exists"):
            os.replace(tmp_path, target)
        else:
            # link() is an atomic no-overwrite install. If another container
            # creates the destination after our last check, this fails closed.
            os.link(tmp_path, target, follow_symlinks=False)
        _fsync_directory(parent)

        installed = regular_file_identity(target, root=root)
        if not installed or installed.get("size") != source_identity.get("size"):
            raise FileSafetyError("Installed file could not be verified")
        return installed
    finally:
        if fd is not None:
            os.close(fd)
        try:
            if os.path.lexists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def atomic_quarantine_file(
    source,
    destination,
    *,
    root=None,
    expected_source=None,
):
    """Move a file without overwrite or cross-filesystem copy/delete fallback.

    A hard link is made durable before the source name is removed. Interruption
    between those operations can leave two complete names, but not a partial or
    missing file.
    """
    source_identity = regular_file_identity(source, root=root)
    if not source_identity:
        raise FileSafetyError("Source is missing or unsafe")
    if expected_source and not identity_matches(source, expected_source, root=root):
        raise FileSafetyError("Source changed before quarantine")
    if os.path.lexists(destination):
        raise FileExistsError("Quarantine destination already exists")

    parent = os.path.dirname(destination)
    if (
        not parent
        or not os.path.isdir(parent)
        or path_has_symlink_component(parent, root=root)
        or (root and not path_is_under(destination, root))
    ):
        raise FileSafetyError("Quarantine destination is unsafe")

    linked = False
    try:
        os.link(source, destination, follow_symlinks=False)
        linked = True
        _fsync_directory(parent)

        try:
            same_file = os.path.samefile(source, destination)
        except OSError:
            same_file = False
        current = regular_file_identity(source, root=root)
        stable_keys = ("real_path", "size", "mtime_ns", "inode", "device")
        if not same_file or not current or any(
            current.get(key) != source_identity.get(key) for key in stable_keys
        ):
            raise FileSafetyError("Source changed during quarantine")

        os.unlink(source)
        _fsync_directory(os.path.dirname(source))
        return regular_file_identity(destination, root=root)
    except Exception:
        if linked and os.path.lexists(destination):
            try:
                if os.path.exists(source) and os.path.samefile(source, destination):
                    os.unlink(destination)
                    _fsync_directory(parent)
            except OSError:
                pass
        raise


def atomic_move_file_no_overwrite(
    source,
    destination,
    *,
    root=None,
    expected_source=None,
):
    """Rename a regular file without replacing an existing destination.

    This shares the durable hard-link/unlink implementation used for
    quarantine moves, but describes ordinary in-folder renames accurately at
    call sites such as poster backup preservation.
    """
    return atomic_quarantine_file(
        source,
        destination,
        root=root,
        expected_source=expected_source,
    )
