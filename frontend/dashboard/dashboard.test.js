import assert from 'node:assert/strict';
import test from 'node:test';


class FakeElement {
  constructor() {
    this.textContent = '';
    this.innerHTML = '';
    this.style = {};
    this.attributes = {};
    this.classList = { toggle() {} };
  }

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
