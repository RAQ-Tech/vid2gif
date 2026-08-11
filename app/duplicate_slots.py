"""Resolve the best copy of every file in a duplicate group, slot by slot.

A duplicate group holds several copies of the same title, each with its own
sidecars. Choosing one copy and removing the rest also removes that copy's
sidecars, which can throw away a better subtitle, poster, or preview than the
one the winning copy happens to carry.

This module decides each *slot* independently instead. A slot is one file role
plus its suffix (``subtitle:.eng.srt``, ``poster:-poster.jpg``), so every copy
that contributes a file of that shape competes for it. The winner is renamed
onto the keeper's stem regardless of which copy it came from, and the losers are
handed back for cleanup.

Time-based slots -- subtitles and BIF previews -- are judged against the
*keeper's* runtime rather than their own parent's. A subtitle taken from a
longer cut then scores badly and flags itself, which is what keeps a mismatched
file from being chosen silently.

The resolver is deliberately free of filesystem and ffprobe calls: every
measurement arrives through an injected probe, so the ranking rules can be
tested without media on disk.
"""

import os


TIME_BASED_ROLES = frozenset({"subtitle", "bif"})
IMAGE_ROLES = frozenset({"poster", "background", "thumb", "clearlogo", "performer"})
VIDEO_SLOT_KEY = "video"

# Roles we understand well enough to act on without asking. Anything else is
# surfaced for review rather than guessed at.
KNOWN_ROLES = TIME_BASED_ROLES | IMAGE_ROLES | frozenset({"nfo"})

DEFAULTS = {
    # Coverage points within which two subtitles are treated as a tie.
    "subtitle_close_points": 8.0,
    # Fraction of pixel count within which two images are treated as a tie.
    "image_close_ratio": 0.10,
    # Seconds of runtime difference tolerated before a time-based file is
    # considered a poor match for the keeper.
    "runtime_tolerance_seconds": 60.0,
}


def _duration_of(video):
    metadata = video.get("metadata") or {}
    try:
        value = float(metadata.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _size_of(item):
    try:
        return int(item.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return 0


def _slot_label(role, suffix):
    pretty = {
        "subtitle": "Subtitle",
        "bif": "BIF preview",
        "poster": "Poster",
        "background": "Background",
        "thumb": "Thumbnail",
        "clearlogo": "Clear logo",
        "performer": "Performer image",
        "nfo": "NFO",
        "unknown": "Unrecognized file",
    }.get(role, role.replace("_", " ").title())
    return f"{pretty} ({suffix})" if suffix else pretty


def _default_probe_image(path):
    from . import poster_maintenance

    return poster_maintenance._probe_image_dimensions(path)


def _default_analyze_subtitle(path, duration_seconds):
    from . import subtitle_quality

    return subtitle_quality.analyze_srt(path, duration_seconds)


def _default_bif_info(path):
    from . import video_preview_maintenance

    parsed = video_preview_maintenance.parse_bif(path)
    if not parsed or not parsed.get("valid"):
        return None
    # parse_bif does not report frame width; the app derives it from the
    # filename first and falls back to decoding one sampled JPEG, so mirror that.
    width = video_preview_maintenance._bif_width_from_name(os.path.basename(path))
    if not width:
        samples = parsed.get("samples") or []
        if samples:
            width, _height = video_preview_maintenance._jpeg_dimensions(
                samples[0].get("bytes")
            )
    interval = max(
        1, int(round((parsed.get("timestamp_multiplier_ms") or 1000) / 1000))
    )
    return {
        "frame_count": parsed.get("image_count") or 0,
        "width": int(width or 0),
        "interval_seconds": interval,
    }


def _candidates_for_slots(videos):
    """Group every accessory across every copy by its slot key."""
    slots = {}
    for video in videos:
        for accessory in video.get("accessories") or []:
            role = str(accessory.get("role") or "unknown")
            suffix = str(accessory.get("suffix") or "")
            key = accessory.get("equivalence_key") or f"{role}:{suffix.lower()}"
            slot = slots.setdefault(
                key,
                {"slot_key": key, "role": role, "suffix": suffix, "candidates": []},
            )
            slot["candidates"].append({"file": accessory, "video": video})
    return slots


def _all_equivalent(candidates):
    """Same-size files are treated as interchangeable, matching the existing
    review-flag convention elsewhere in duplicate cleanup."""
    sizes = {_size_of(entry["file"]) for entry in candidates}
    return len(sizes) < 2


def _prefer_keeper(candidates, keeper):
    for entry in candidates:
        if entry["video"].get("id") == keeper.get("id"):
            return entry
    return candidates[0]


def _rank_subtitles(candidates, keeper, options, analyze_subtitle):
    duration = _duration_of(keeper)
    tolerance = options["runtime_tolerance_seconds"]
    status_rank = {
        "complete": 4,
        "coverage_review": 3,
        "duration_unknown": 2,
        "likely_incomplete": 1,
        "timing_review": 0,
        "invalid": 0,
        "unreadable": 0,
    }
    scored = []
    for entry in candidates:
        quality = analyze_subtitle(entry["file"].get("path"), duration) or {}
        last = quality.get("last_timestamp_seconds")
        overrun = (
            last is not None and duration > 0 and float(last) - duration > tolerance
        )
        scored.append(
            {
                **entry,
                "quality": quality,
                "overruns_keeper": bool(overrun),
                "_rank": (
                    status_rank.get(str(quality.get("status") or ""), 0),
                    float(quality.get("coverage_ratio") or 0),
                    _size_of(entry["file"]),
                ),
            }
        )
    scored.sort(key=lambda item: item["_rank"], reverse=True)

    # Taking a file from another copy is only justified by evidence that it is
    # better. Unreadable or untimed subtitles give no such evidence, so the
    # keeper's own copy stays and the slot is raised for review instead.
    measurable = [
        item for item in scored if item["quality"].get("coverage_ratio") is not None
    ]
    if not measurable:
        return (
            _prefer_keeper(candidates, keeper),
            "Subtitle timings could not be read, so the keeper's copy stays",
            False,
            [
                {
                    "kind": "no_comparable_signal",
                    "label": "No readable timings to compare these subtitles by",
                }
            ],
        )

    best = scored[0]
    quality = best["quality"]
    percent = quality.get("coverage_percent")
    reason = (
        f"{percent}% coverage of the keeper's runtime"
        if percent is not None
        else str(quality.get("label") or "Best available subtitle")
    )
    close = False
    if len(scored) > 1:
        first = scored[0]["quality"].get("coverage_percent")
        second = scored[1]["quality"].get("coverage_percent")
        if first is not None and second is not None:
            close = abs(float(first) - float(second)) < options["subtitle_close_points"]
    flags = []
    if best["overruns_keeper"]:
        flags.append(
            {
                "kind": "runtime_mismatch",
                "label": "Subtitle runs past the keeper's runtime; timings may not line up",
            }
        )
    if any(entry["overruns_keeper"] for entry in scored) and not best["overruns_keeper"]:
        flags.append(
            {
                "kind": "runtime_mismatch_avoided",
                "label": "A longer-running subtitle was passed over to match the keeper",
            }
        )
    return best, reason, close, flags


def _rank_images(candidates, keeper, options, probe_image):
    scored = []
    for entry in candidates:
        dimensions = probe_image(entry["file"].get("path")) or {}
        width = int(dimensions.get("width") or 0)
        height = int(dimensions.get("height") or 0)
        scored.append(
            {
                **entry,
                "dimensions": dimensions,
                "pixels": width * height,
                "_rank": (width * height, _size_of(entry["file"])),
            }
        )
    scored.sort(key=lambda item: item["_rank"], reverse=True)

    # Same rule as subtitles: without a measurement there is no case for
    # preferring another copy's image over the keeper's.
    if not any(item["pixels"] for item in scored):
        return (
            _prefer_keeper(candidates, keeper),
            "Image dimensions could not be read, so the keeper's copy stays",
            False,
            [
                {
                    "kind": "no_comparable_signal",
                    "label": "No readable dimensions to compare these images by",
                }
            ],
        )

    best = scored[0]
    reason = (
        f"{best['dimensions'].get('width')} x {best['dimensions'].get('height')}"
        if best["pixels"]
        else "Largest available image"
    )
    close = False
    if len(scored) > 1 and scored[0]["pixels"] and scored[1]["pixels"]:
        top, runner = scored[0]["pixels"], scored[1]["pixels"]
        close = abs(top - runner) <= top * options["image_close_ratio"]
    return best, reason, close, []


def _rank_bif(candidates, keeper, options, bif_info):
    duration = _duration_of(keeper)
    scored = []
    for entry in candidates:
        info = bif_info(entry["file"].get("path")) or {}
        frames = int(info.get("frame_count") or 0)
        width = int(info.get("width") or 0)
        interval = float(entry["file"].get("interval_seconds") or 0) or 10.0
        implied = frames * interval
        mismatch = duration > 0 and abs(implied - duration) > max(
            options["runtime_tolerance_seconds"], interval * 2
        )
        scored.append(
            {
                **entry,
                "info": info,
                "implied_seconds": implied,
                "mismatches_keeper": bool(mismatch),
                # Matching the keeper's runtime outranks raw frame quality.
                "_rank": (0 if mismatch else 1, width, frames),
            }
        )
    scored.sort(key=lambda item: item["_rank"], reverse=True)
    best = scored[0]
    width = best["info"].get("width") or 0
    reason = f"{width}px frames" if width else "Best available preview"
    close = False
    if len(scored) > 1:
        close = scored[0]["_rank"] == scored[1]["_rank"]
    flags = []
    if best["mismatches_keeper"]:
        flags.append(
            {
                "kind": "runtime_mismatch",
                "label": "Preview length does not match the keeper's runtime",
            }
        )
    return best, reason, close, flags


def _rank_generic(candidates, keeper):
    best = max(
        candidates,
        key=lambda entry: (
            1 if entry["video"].get("id") == keeper.get("id") else 0,
            _size_of(entry["file"]),
        ),
    )
    return best, "Kept from the keeper's copy", False, []


def resolve_group_slots(
    videos,
    keeper,
    settings=None,
    *,
    probe_image=None,
    analyze_subtitle=None,
    bif_info=None,
):
    """Resolve every sidecar slot in a duplicate group.

    ``keeper`` is the copy whose stem the surviving files are renamed onto.
    Returns a list of slot dicts, each naming a winner (or none, when the role
    is not understood) plus the losers to clean up.
    """
    options = dict(DEFAULTS)
    for key in DEFAULTS:
        value = (settings or {}).get(f"duplicate_{key}")
        if value is not None:
            try:
                options[key] = float(value)
            except (TypeError, ValueError):
                pass

    probe_image = probe_image or _default_probe_image
    analyze_subtitle = analyze_subtitle or _default_analyze_subtitle
    bif_info = bif_info or _default_bif_info

    keeper_dir = os.path.dirname(str(keeper.get("path") or ""))
    keeper_stem = str(keeper.get("stem") or "")

    resolved = []
    for key, slot in sorted(_candidates_for_slots(videos).items()):
        role = slot["role"]
        candidates = slot["candidates"]
        entry = {
            "slot_key": key,
            "role": role,
            "suffix": slot["suffix"],
            "label": _slot_label(role, slot["suffix"]),
            "candidate_count": len(candidates),
            "candidate_file_ids": [
                item["file"].get("id", "") for item in candidates
            ],
            "winner_file_id": "",
            "winner_video_id": "",
            "destination_path": "",
            "loser_file_ids": [],
            "reason": "",
            "flags": [],
            "needs_review": False,
            "identical": False,
            "borrowed": False,
        }

        if role not in KNOWN_ROLES:
            entry.update(
                reason="Unrecognized sidecar; left untouched for review",
                needs_review=True,
                flags=[
                    {
                        "kind": "unknown_role",
                        "label": f"{len(candidates)} unrecognized file"
                        f"{'s' if len(candidates) != 1 else ''} in this slot",
                    }
                ],
            )
            resolved.append(entry)
            continue

        equivalent = _all_equivalent(candidates)
        entry["identical"] = equivalent and len(candidates) > 1

        # Interchangeable static files can shortcut, but a time-based file is
        # never safe on size alone: even a single candidate has to be checked
        # against the keeper's runtime before it is adopted.
        if equivalent and role not in TIME_BASED_ROLES:
            best = _prefer_keeper(candidates, keeper)
            reason = (
                "Only copy in the set"
                if len(candidates) == 1
                else "Identical in every copy"
            )
            close, flags = False, []
        elif role == "subtitle":
            best, reason, close, flags = _rank_subtitles(
                candidates, keeper, options, analyze_subtitle
            )
        elif role in IMAGE_ROLES:
            best, reason, close, flags = _rank_images(
                candidates, keeper, options, probe_image
            )
        elif role == "bif":
            best, reason, close, flags = _rank_bif(candidates, keeper, options, bif_info)
        else:
            best, reason, close, flags = _rank_generic(candidates, keeper)

        winner_file = best["file"]
        suffix = str(winner_file.get("suffix") or slot["suffix"] or "")
        destination = (
            os.path.realpath(os.path.join(keeper_dir, f"{keeper_stem}{suffix}"))
            if suffix and keeper_stem
            else ""
        )
        borrowed = best["video"].get("id") != keeper.get("id")
        if close:
            flags = list(flags) + [
                {
                    "kind": "close_call",
                    "label": "Top two candidates are too close to separate automatically",
                }
            ]
        entry.update(
            winner_file_id=winner_file.get("id", ""),
            winner_video_id=best["video"].get("id", ""),
            destination_path=destination,
            loser_file_ids=[
                item["file"].get("id", "")
                for item in candidates
                if item["file"].get("id") != winner_file.get("id")
            ],
            reason=reason,
            flags=flags,
            needs_review=bool(flags),
            borrowed=borrowed,
        )
        resolved.append(entry)

    return resolved


def slot_summary(slots):
    """Counts the review UI needs without re-walking the slot list."""
    return {
        "slot_count": len(slots),
        "borrowed_count": sum(1 for slot in slots if slot.get("borrowed")),
        "review_count": sum(1 for slot in slots if slot.get("needs_review")),
        "identical_count": sum(1 for slot in slots if slot.get("identical")),
        "source_video_ids": sorted(
            {slot.get("winner_video_id") for slot in slots if slot.get("winner_video_id")}
        ),
    }
