// Shared UI primitives, so every surface renders the same control the same way.
//
// The operator's complaint was that each page felt like a new learning curve:
// eight hand-rolled pagers with three markups, four different phrasings of the
// selection summary, a master select-all on some tabs and not others. The fix
// is structural rather than cosmetic -- these builders are the only way to
// render a pager or a selection summary, and a source-level conformance test
// (tests/test_ui_conventions.py) fails the build if a page grows its own
// variant again.
(function () {
  // One page-size ladder for every server-paged list.
  const PAGE_SIZE_OPTIONS = [5, 10, 25, 50, 100];

  function rangeText(page) {
    const total = Number(page?.total || 0);
    if (!total) return '0 of 0';
    const offset = Number(page?.offset || 0);
    const count = Number(page?.count ?? (page?.items?.length ?? 0));
    return `${offset + 1}-${offset + count} of ${total}`;
  }

  // The canonical pager: count on the left, Previous/Next with chevrons on the
  // right. `attr` is the data attribute the page's own event delegation
  // listens for, so wiring stays local while the look is shared.
  function pagerHtml(page, attr, options) {
    const suffix = options && options.suffix ? options.suffix : '';
    const prevDisabled = page && page.has_previous ? '' : ' disabled';
    const nextDisabled = page && page.has_next ? '' : ' disabled';
    return (
      `<div class="maintenance-pager">` +
      `<div class="text-muted small">${rangeText(page)}${suffix}</div>` +
      `<div class="toolbar-row mb-0">` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-${attr}="prev"${prevDisabled}>` +
      `<i class="bi bi-chevron-left" aria-hidden="true"></i><span>Previous</span></button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-${attr}="next"${nextDisabled}>` +
      `<span>Next</span><i class="bi bi-chevron-right" aria-hidden="true"></i></button>` +
      `</div>` +
      `</div>`
    );
  }

  // The one phrasing for "how much is selected", everywhere. Pagination is
  // page-scoped but selection is not, and the words carry that promise.
  function selectionSummary(count, noun) {
    const what = noun ? `${noun} ` : '';
    return `${Number(count) || 0} ${what}selected across all result pages`;
  }

  // Master select-all state for the all-eligible/explicit selection model the
  // maintenance tabs share: checked means everything, a dash means some.
  function syncMasterCheckbox(checkbox, selectedCount, totalCount) {
    if (!checkbox) return;
    const total = Number(totalCount) || 0;
    const count = Number(selectedCount) || 0;
    checkbox.disabled = !total;
    checkbox.checked = total > 0 && count === total;
    checkbox.indeterminate = count > 0 && count < total;
  }

  window.vid2gifUI = {
    PAGE_SIZE_OPTIONS,
    pagerHtml,
    rangeText,
    selectionSummary,
    syncMasterCheckbox,
  };
}());
