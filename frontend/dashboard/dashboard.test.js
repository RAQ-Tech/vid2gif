import assert from 'node:assert/strict';
import test from 'node:test';


class FakeElement {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.textContent = '';
    this.innerHTML = '';
    this.style = {};
    this.attributes = {};
    this.dataset = {};
    this.children = [];
    this.listeners = {};
    this.classes = new Set();
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
      toggle: (name) => {
        if (this.classes.has(name)) {
          this.classes.delete(name);
          return false;
        }
        this.classes.add(name);
        return true;
      },
    };
  }

  setAttribute(key, value) { this.attributes[key] = value; }

  getAttribute(key) { return this.attributes[key]; }

  appendChild(child) { this.children.push(child); return child; }

  addEventListener(name, handler) { this.listeners[name] = handler; }

  click() { if (this.listeners.click) this.listeners.click(); }

  closest() {
    return { setAttribute: (key, value) => { this.attributes[key] = value; } };
  }
}


const elements = new Map();
globalThis.window = {
  vid2gifDashboardConfig: {},
  addEventListener() {},
};
globalThis.document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new FakeElement());
    return elements.get(id);
  },
  querySelector() {
    return new FakeElement();
  },
  createElement(tag) {
    return new FakeElement(tag);
  },
  addEventListener() {},
};

await import('../../app/static/dashboard.js');
const dashboard = globalThis.window.vid2gifDashboardTest;


test('impact renderer handles zero state and large values', () => {
  dashboard.renderImpact({
    impact: {
      status: 'ok',
      tracking_started_at: '2026-07-10T12:00:00Z',
      total_fixes: 1234567,
      resolved_count: 1234567,
      discovered_count: 2000000,
      cleared_elsewhere_count: 2,
      open_count: 4,
      resolution_percent: 61.7,
      operations: {
        quarantined_files: 12,
        quarantined_size_label: '2.0 GB',
        deleted_files: 3,
        deleted_size_label: '1.0 GB',
      },
      categories: [],
      daily: [],
      milestones: { earned: [], next: { label: '2,000,000 Fixes', target: 2000000, current: 1234567, progress_percent: 62 } },
    },
  });

  assert.equal(elements.get('dashboardTotalFixes').textContent, '1,234,567');
  assert.equal(elements.get('dashboardResolutionRate').textContent, '62%');
  assert.equal(elements.get('dashboardImpactOpenCount').textContent, '4');
  assert.equal(elements.get('dashboardImpactProgressBar').style.width, '62%');
});


test('impact category output escapes server-provided labels', () => {
  dashboard.renderImpact({
    impact: {
      status: 'ok',
      categories: [{
        key: 'duplicates',
        title: '<img src=x onerror=alert(1)>',
        href: '/maintenance#duplicates',
        resolved_count: 1,
        discovered_count: 2,
        open_count: 1,
        resolution_percent: 50,
      }],
      daily: [],
      milestones: { earned: [], next: null },
      operations: {},
    },
  });

  const html = elements.get('dashboardImpactCategories').innerHTML;
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img src=x/);
});


test('percentage clamping is stable', () => {
  assert.equal(dashboard.clampPercent(-5), 0);
  assert.equal(dashboard.clampPercent(49.6), 50);
  assert.equal(dashboard.clampPercent(120), 100);
  assert.equal(dashboard.clampPercent('invalid'), 0);
});


test('the backfill note stays hidden when no history was recovered', () => {
  const base = {
    status: 'ok', total_fixes: 0, resolved_count: 0, discovered_count: 0,
    cleared_elsewhere_count: 0, open_count: 0, resolution_percent: 0,
    operations: {}, categories: [], daily: [], milestones: { earned: [] },
  };

  dashboard.renderImpact({ impact: { ...base, backfill: null } });
  assert.equal(document.getElementById('dashboardBackfillNote').classList.contains('d-none'), true);

  // A backfill that recovered nothing is not worth a note either.
  dashboard.renderImpact({ impact: { ...base, backfill: { events_applied: 0, files_recovered: 0 } } });
  assert.equal(document.getElementById('dashboardBackfillNote').classList.contains('d-none'), true);
});


test('recovered history is disclosed with what could not be recovered', () => {
  dashboard.renderImpact({
    impact: {
      status: 'ok', total_fixes: 9, resolved_count: 9, discovered_count: 9,
      cleared_elsewhere_count: 0, open_count: 0, resolution_percent: 100,
      operations: { quarantined_files: 40, deleted_files: 2 },
      categories: [], daily: [], milestones: { earned: [] },
      backfill: {
        events_applied: 3,
        files_recovered: 42,
        not_recoverable: ['Issue history was never logged.', 'Subtitle byte totals are gone.'],
      },
    },
  });

  const note = document.getElementById('dashboardBackfillNote');
  const summary = document.getElementById('dashboardBackfillSummary');
  const detail = document.getElementById('dashboardBackfillDetail');

  assert.equal(note.classList.contains('d-none'), false);
  assert.match(summary.textContent, /3 earlier runs recovered from audit logs/);
  assert.match(summary.textContent, /42 files/);
  // The caveats are listed rather than summarised away.
  assert.equal(detail.children.length, 2);
  assert.equal(detail.children[0].textContent, 'Issue history was never logged.');
});


test('the caveat list expands and collapses', () => {
  dashboard.renderImpact({
    impact: {
      status: 'ok', total_fixes: 1, resolved_count: 1, discovered_count: 1,
      cleared_elsewhere_count: 0, open_count: 0, resolution_percent: 100,
      operations: {}, categories: [], daily: [], milestones: { earned: [] },
      backfill: { events_applied: 1, files_recovered: 5, not_recoverable: ['Only this.'] },
    },
  });

  const toggle = document.getElementById('dashboardBackfillToggle');
  const detail = document.getElementById('dashboardBackfillDetail');

  // The markup ships collapsed; the stub does not read class attributes, so
  // put it in that state explicitly before exercising the toggle.
  detail.classList.add('d-none');

  toggle.click();
  assert.equal(detail.classList.contains('d-none'), false, 'first click expands');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');

  toggle.click();
  assert.equal(detail.classList.contains('d-none'), true, 'second click collapses');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
});
