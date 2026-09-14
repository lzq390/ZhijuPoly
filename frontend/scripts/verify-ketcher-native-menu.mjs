import assert from 'node:assert/strict';

// Exercise the host's portal ownership as well as the transformed official CSS.
// The standalone SDK compatibility page does not mount this host lifecycle.
export async function verifyKetcherNativeMenu(page) {
  await page.locator('[data-testid="settings-button"]:visible').click();
  await page.locator('[role="combobox"]:visible').first().click();
  await page.locator('.MuiModal-root .MuiMenuItem-root').first().waitFor();
  await page.mouse.move(0, 0);
  const result = await page.evaluate(() => {
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
}
