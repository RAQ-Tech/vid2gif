import { test, expect } from '@playwright/test';

// Every maintenance tab wrote the shared scan source when it scanned, and
// nothing read it back, so choosing a folder on one tab left the other five
// showing the library root. Working through the tabs meant picking the same
// folder five times.

const PATH_INPUTS = ['#maintenancePath', '#previewPath', '#subtitlePath', '#actorPath', '#posterPath'];
const CHOSEN = '/library/Chosen Folder';

async function withStoredSource(page, entries) {
  await page.addInitScript(stored => {
    for (const [key, value] of Object.entries(stored)) {
      localStorage.setItem(key, value);
    }
  }, entries);
}

test('the folder chosen on one tab is where every other tab starts', async ({ page }) => {
  await withStoredSource(page, { vid2gif_maintenance_scan_source: CHOSEN });

  await page.goto('/maintenance');

  for (const selector of PATH_INPUTS) {
    await expect(page.locator(selector), `${selector} should follow the shared folder`).toHaveValue(CHOSEN);
  }
});

test('a tab pointed somewhere specific keeps its own folder', async ({ page }) => {
  // The video preview path is a saved setting, not a scratch value, so a
  // deliberate choice there must not be overwritten by the shared one.
  await withStoredSource(page, {
    vid2gif_maintenance_scan_source: CHOSEN,
    vid2gif_preview_scan_source: '/library/Only For Previews',
  });

  await page.goto('/maintenance');

  await expect(page.locator('#previewPath')).toHaveValue('/library/Only For Previews');
  await expect(page.locator('#subtitlePath')).toHaveValue(CHOSEN);
});

test('with nothing stored the tabs fall back to the library root', async ({ page }) => {
  await page.goto('/maintenance');

  const root = await page.locator('#maintenancePath').inputValue();
  expect(root, 'the server-rendered default should survive an empty store').toBeTruthy();
  for (const selector of PATH_INPUTS) {
    await expect(page.locator(selector)).not.toHaveValue('');
  }
});

test('the current page is announced, not only coloured', async ({ page }) => {
  // DESIGN.md: colour is never the only signal. Without aria-current a screen
  // reader cannot tell which of the five pages is open.
  for (const [path, label] of [
    ['/maintenance', 'Maintenance'],
    ['/gifs', 'GIFs'],
    ['/settings', 'Settings'],
    ['/system', 'System'],
  ]) {
    await page.goto(path);
    const current = page.locator('.app-navbar .nav-link[aria-current="page"]');
    await expect(current).toHaveCount(1);
    await expect(current).toHaveText(new RegExp(label));
  }
});

test('every maintenance tab is reachable by its own address', async ({ page }) => {
  // Deep links are what make a seven-tab workbench navigable at all.
  for (const hash of ['duplicates', 'subtitles', 'actor-images', 'video-previews', 'posters']) {
    await page.goto(`/maintenance#${hash}`);
    await expect(page.locator(`#pane-${hash}`)).toHaveClass(/active/);
  }
});
