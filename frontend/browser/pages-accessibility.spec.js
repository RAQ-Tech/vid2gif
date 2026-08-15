import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { waitForOpaque } from './helpers.js';

// The remaining surfaces: the two read-only maintenance tabs, and the four
// pages that had no browser coverage at all. These are checked in their real
// default state -- which, for a summary tab or a settings form, is the state
// most users see most of the time.

const WCAG = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'];

async function expectNoViolations(page, scope) {
  const result = await new AxeBuilder({ page }).include(scope).withTags(WCAG).analyze();
  const summary = result.violations.map(v => ({
    id: v.id,
    impact: v.impact,
    nodes: v.nodes.map(n => n.html.slice(0, 120)),
  }));
  expect(summary).toEqual([]);
}

test.describe('maintenance tabs that only report', () => {
  test('the overview tab renders its workstreams and is accessible', async ({ page }) => {
    await page.goto('/maintenance#overview');
    await waitForOpaque(page, '#pane-overview');

    // The tab leads with the library inventory it summarises.
    await expect(page.locator('#pane-overview')).toContainText('Inventory');
    await expectNoViolations(page, '#pane-overview');
  });

  test('the Emby operations tab is accessible when Emby is not configured', async ({ page }) => {
    await page.goto('/maintenance#emby-operations');
    await waitForOpaque(page, '#pane-emby-operations');

    // Not configured is the default state, and it must still explain itself
    // rather than rendering an empty panel.
    await expect(page.locator('#pane-emby-operations')).not.toBeEmpty();
    await expectNoViolations(page, '#pane-emby-operations');
  });
});

test.describe('pages with no previous browser coverage', () => {
  test('the dashboard renders and is accessible', async ({ page }) => {
    await page.goto('/dashboard');
    await expect(page.locator('main, .app-nav-shell').first()).toBeVisible();
    await expectNoViolations(page, 'body');
  });

  test('the settings page renders and is accessible', async ({ page }) => {
    await page.goto('/settings');
    await expect(page.locator('form').first()).toBeVisible();
    await expectNoViolations(page, 'body');
  });

  test('the system page renders and is accessible', async ({ page }) => {
    await page.goto('/system');
    await expect(page.locator('body')).not.toBeEmpty();
    await expectNoViolations(page, 'body');
  });

  test('the test lab is accessible inside the GIFs page', async ({ page }) => {
    await page.goto('/gifs#test');
    await waitForOpaque(page, '#pane-test');
    await expectNoViolations(page, '#pane-test');
  });
});

test('every page keeps its layout inside the viewport at 375px', async ({ page }) => {
  // DESIGN.md: "Never create page-level horizontal overflow." Long paths and
  // wide tables are supposed to scroll inside their own bounded region.
  await page.setViewportSize({ width: 375, height: 812 });

  for (const path of ['/dashboard', '/gifs', '/maintenance', '/settings', '/system']) {
    await page.goto(path);
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(
      overflow.scrollWidth,
      `${path} overflows the viewport horizontally`,
    ).toBeLessThanOrEqual(overflow.clientWidth + 1);
  }
});
