import assert from 'node:assert/strict';

// Exercise the host's portal ownership as well as the transformed official CSS.
// The standalone SDK compatibility page does not mount this host lifecycle.
export async function verifyKetcherNativeMenu(page) {
  let result;
  try {
    await page.locator('[data-testid="settings-button"]:visible').click();
    await page.locator('[role="combobox"]:visible').first().click();
    await page.locator('.MuiModal-root .MuiMenuItem-root').first().waitFor();
    await page.mouse.move(0, 0);
    await page.evaluate(async () => {
      // Portal ownership can start CSS transitions after MUI autofocus. Even
      // reduced motion keeps a nonzero duration; read the settled menu rather
      // than its initial MUI values before the first animation frame.
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
      const option = document.querySelector('.MuiModal-root .MuiMenuItem-root');
      let timeout;
      try {
        await Promise.race([
          Promise.all(option.getAnimations().filter(animation => animation instanceof CSSTransition)
            .map(animation => animation.finished.catch(() => {}))),
          new Promise((_, reject) => {
            timeout = setTimeout(() => reject(new Error('Native menu CSS transitions did not settle')), 2000);
          })
        ]);
      } finally {
        clearTimeout(timeout);
      }
    });
    result = await page.evaluate(() => {
      const root = document.querySelector('[data-structure-editor]');
      const option = document.querySelector('.MuiModal-root .MuiMenuItem-root');
      const portal = option.closest('.MuiModal-root');
      const itemStyle = getComputedStyle(option);
      const listStyle = getComputedStyle(portal.querySelector('.MuiList-root.MuiMenu-list'));
      return {
        session: root.dataset.ketcherSession,
        owner: portal.dataset.ketcherOwner,
        hostHiddenByModal: !!root.closest('[aria-hidden="true"]'),
        item: { fontSize: itemStyle.fontSize, minHeight: itemStyle.minHeight, padding: itemStyle.padding },
        list: { maxHeight: listStyle.maxHeight, overflow: listStyle.overflow }
      };
    });
    assert.ok(result.session, 'Editor session must be available');
    assert.equal(result.owner, result.session, 'Settings menu must keep its editor owner after MUI autofocus');
    assert.equal(result.hostHiddenByModal, true, 'Exercise MUI hiding the host behind its modal');
    assert.deepEqual(result.item, { fontSize: '14px', minHeight: '28px', padding: '0px 8px' });
    assert.deepEqual(result.list, { maxHeight: '200px', overflow: 'auto' });
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.locator('[role="combobox"]:visible').first().waitFor({ state: 'hidden' });
    return result;
  } catch (error) {
    // Capture the cascade only after a failure; successful probes keep the
    // same interactions and strict expectations, with no style polling.
    const diagnostics = await page.evaluate(() => {
      const root = document.querySelector('[data-structure-editor]');
      const option = document.querySelector('.MuiModal-root .MuiMenuItem-root');
      const portal = option?.closest('.MuiModal-root');
      const list = portal?.querySelector('.MuiList-root.MuiMenu-list');
      const targets = { option, list, portal };
      const properties = ['font', 'font-size', 'min-height', 'padding', 'max-height',
        'overflow', 'display', 'visibility', 'transition', 'animation'];
      const computed = Object.fromEntries(Object.entries(targets).map(([name, node]) =>
        [name, node ? Object.fromEntries(properties.map(property =>
          [property, getComputedStyle(node).getPropertyValue(property)])) : null]));
      const stylesheets = [...document.styleSheets].map((sheet, index) => {
        const entry = { index, href: sheet.href, disabled: sheet.disabled, media: sheet.media?.mediaText ?? '',
          owner: sheet.ownerNode ? sheet.ownerNode.outerHTML.split('>')[0] + '>' : null, matchedRules: [] };
        const visit = (rules, groups = []) => {
          for (const rule of rules) {
            if (rule.selectorText) {
              for (const [target, node] of Object.entries(targets)) {
                if (!node) continue;
                try {
                  if (node.matches(rule.selectorText)) entry.matchedRules.push({
                    target, selector: rule.selectorText, css: rule.style.cssText, groups
                  });
                } catch { /* Pseudo-elements and unsupported selectors cannot match this element. */ }
              }
            }
            if (rule.cssRules) visit(rule.cssRules, [...groups, rule.cssText.split('{')[0].trim()]);
          }
        };
        try { visit(sheet.cssRules); } catch (failure) { entry.accessError = String(failure); }
        return entry;
      });
      return { session: root?.dataset.ketcherSession, owner: portal?.dataset.ketcherOwner,
        targetHtml: option?.outerHTML ?? null, portalHtml: portal?.outerHTML ?? null,
        activeElementHtml: document.activeElement?.outerHTML ?? null, computed, stylesheets };
    }).catch(failure => ({ captureError: String(failure) }));
    const details = '\nKetcher native menu diagnostics:\n' + JSON.stringify({ observed: result, ...diagnostics }, null, 2);
    if (error instanceof Error) {
      const originalStack = error.stack;
      error.message += details;
      error.stack = originalStack ? originalStack + details : error.message;
    } else console.error(details);
    throw error;
  }
}
