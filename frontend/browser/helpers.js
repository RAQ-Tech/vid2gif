/**
 * Wait until every CSS transition/animation inside a container has settled.
 *
 * Bootstrap's `.fade` panes animate opacity 0 -> 1 and add the `show` class at
 * the *start* of that transition, so waiting on the class is not enough. axe
 * composites text and background colours through whatever opacity is in effect
 * when it runs, and a half-faded pane reports colours that are washed out
 * toward the page background -- producing colour-contrast violations that do
 * not exist once the pane is fully visible.
 */
export async function waitForOpaque(page, selector) {
  await page.waitForFunction(
    target => {
      const element = document.querySelector(target);
      if (!element) return false;
      if (Number(getComputedStyle(element).opacity) < 1) return false;
      return element
        .getAnimations({ subtree: true })
        .every(animation => animation.playState !== 'running');
    },
    selector,
    { timeout: 5000 },
  );
}
