"""The shared UI contract, enforced at build time.

The operator's standing complaint was that every page felt like a new learning
curve -- pagination on one list but not another, a select-all here but not
there, four phrasings of the same summary. Fixing the instances once is not
enough: the next feature would drift again. These tests pin the conventions at
the source level, so a page that grows its own pager, its own page-size ladder,
or its own wording fails CI instead of shipping.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
STATIC = ROOT / "app" / "static"

CANONICAL_PAGE_SIZES = ["5", "10", "25", "50", "100"]
CANONICAL_SUMMARY_PHRASE = "selected across all result pages"


def _read(path):
    return path.read_text(encoding="utf-8")


def test_every_page_size_selector_offers_the_same_ladder():
    """One page-size ladder everywhere, so learning it once is enough."""
    found = 0
    for template in sorted(TEMPLATES.glob("*.html")):
        text = _read(template)
        for match in re.finditer(r'<select id="(\w*PageLimit)"[^>]*>(.*?)</select>', text, re.S):
            select_id, body = match.groups()
            values = re.findall(r'<option value="(\d+)"', body)
            assert values == CANONICAL_PAGE_SIZES, (
                f"{template.name}#{select_id} offers {values}; every page-size selector must offer "
                f"{CANONICAL_PAGE_SIZES} so no list needs relearning"
            )
            found += 1
    assert found >= 8, f"expected at least 8 page-size selectors, found {found}"


def test_every_pager_is_built_by_the_shared_kit():
    """Pagers render through vid2gifUI.pagerHtml, never by hand."""
    kit = _read(STATIC / "ui-kit.js")
    assert "function pagerHtml" in kit

    maintenance = _read(STATIC / "maintenance.js")
    delegated = maintenance.count("vid2gifUI.pagerHtml(")
    assert delegated >= 8, f"expected all 8 paged areas to delegate to the kit, found {delegated}"
    # No hand-rolled pager markup outside the kit: the literal button labels
    # exist only in ui-kit.js.
    assert ">Previous<" not in maintenance, "a hand-rolled pager crept back into maintenance.js"
    assert "<span>Previous</span>" not in maintenance


def test_selection_summaries_use_one_phrase():
    """Whatever the noun, the scope phrase is always the same promise."""
    maintenance = _read(STATIC / "maintenance.js")
    for line_no, line in enumerate(maintenance.splitlines(), start=1):
        if "selected across" in line:
            assert CANONICAL_SUMMARY_PHRASE in line, (
                f"maintenance.js:{line_no} uses a nonstandard selection phrase; "
                f'the only allowed wording is "... {CANONICAL_SUMMARY_PHRASE}"'
            )


def test_every_reviewable_list_has_a_master_select_all():
    """A select-all on one page means a select-all on every page it fits."""
    maintenance_html = _read(TEMPLATES / "maintenance.html")
    for checkbox_id in (
        "duplicateSelectAllCheckbox",
        "posterSelectAllCheckbox",
        "actorSelectAllCheckbox",
        "subtitleSelectAllCheckbox",
        "taglineSelectAllCheckbox",
    ):
        assert checkbox_id in maintenance_html, f"{checkbox_id} is missing its master select-all"
    # Video previews carry theirs in the results-table header instead.
    assert "previewSelectVisible" in _read(STATIC / "maintenance.js")


def test_the_ui_kit_loads_before_the_scripts_that_use_it():
    base = _read(TEMPLATES / "base.html")
    kit = base.index("ui-kit.js")
    assert kit < base.index("progress-ui.js")
    maintenance_html = _read(TEMPLATES / "maintenance.html")
    assert "maintenance.js" in maintenance_html or "maintenance.js" in base


def test_video_previews_present_scan_results_before_generation():
    """Scan, results, then generation -- in reading order, in separate panels."""
    text = _read(TEMPLATES / "maintenance.html")
    repair = text.index("Video Preview Repair")
    results = text.index('id="previewItems"')
    generation = text.index("Generate Missing Previews")
    quality = text.index("BIF Quality Check")
    assert repair < results < generation < quality, (
        "the video previews tab must read: repair scan, its results, the generation panel, then quality"
    )

    # One primary action per panel, as DESIGN.md requires.
    repair_panel = text[repair:generation]
    generation_panel = text[generation:quality]
    assert repair_panel.count("btn-primary") == 1, "the repair panel must have exactly one primary action"
    assert generation_panel.count("btn-primary") == 1, "the generation panel must have exactly one primary action"


def test_gif_advanced_sampling_is_folded_away_by_default():
    """The basic feature is pick a video and submit; tuning hides until asked for."""
    text = _read(TEMPLATES / "gifs.html")
    collapse = text.index('id="gifAdvancedSampling"')
    collapse_tag = text[text.rindex("<div", 0, collapse) : text.index(">", collapse) + 1]
    assert 'class="collapse"' in collapse_tag, "advanced sampling must start collapsed"
    assert "collapse show" not in collapse_tag
    # The tuning fields live inside the collapse, after its opening tag.
    for field in ("percent_points", "abs_early", "abs_late_from_end", "start_buffer", "end_buffer"):
        assert text.index(f'id="{field}"') > collapse, f"{field} must sit inside the advanced collapse"
    # And the toggle announces itself.
    toggle = text[text.index("gifAdvancedSampling") - 400 : collapse]
    assert 'aria-expanded="false"' in toggle
