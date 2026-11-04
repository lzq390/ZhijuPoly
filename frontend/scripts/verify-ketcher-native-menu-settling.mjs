// Exercise the real browser cascade without an application, backend or SDK.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { verifyKetcherNativeMenu } from './verify-ketcher-native-menu.mjs';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'],
  executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const cases = [];
try {
  for (const mode of ['transition', 'permanent-style-error', 'wrong-owner']) {
    const page = await context.newPage();
    await page.route('**/*', route => route.abort());
    page.setDefaultTimeout(3000);
    await page.setContent(`<!doctype html><meta charset="utf-8"><style>
      [hidden] { display: none !important; }
      .MuiMenuItem-root { font-size: 16px; min-height: 28px; padding: 6px 16px;
        transition: font-size 160ms linear, padding 160ms linear; }
      [data-ketcher-owner] .MuiMenuItem-root { font-size: 14px; padding: 0px 8px; }
      .MuiList-root { max-height: 200px; overflow: auto; }
      ${mode === 'permanent-style-error' ? '.MuiMenuItem-root { font-size: 16px !important; padding: 6px 16px !important; }' : ''}
    </style><main id="host"><div data-structure-editor data-ketcher-session="fixture-session"></div>
      <button data-testid="settings-button">Settings</button></main>
    <section id="settings" hidden><button role="combobox">Menu</button><button id="cancel">Cancel</button></section>
    <div id="portal" class="MuiModal-root" hidden><ul class="MuiList-root MuiMenu-list">
      <li class="MuiMenuItem-root">Option</li></ul></div><script>
      const portal = document.querySelector('#portal'), host = document.querySelector('#host'),
        settings = document.querySelector('#settings');
      document.querySelector('[data-testid="settings-button"]').onclick = () => settings.hidden = false;
      document.querySelector('[role="combobox"]').onclick = () => {
        portal.hidden = false;
        host.setAttribute('aria-hidden', 'true');
        const option = portal.querySelector('li');
        // Force the same initial MUI style flush that autofocus can perform
        // before the host's portal ownership observer assigns its scope.
        getComputedStyle(option).fontSize;
        portal.dataset.ketcherOwner = ${JSON.stringify(mode === 'wrong-owner' ? 'another-session' : 'fixture-session')};
        const style = getComputedStyle(option);
        window.initialMenu = { fontSize: style.fontSize, padding: style.padding };
      };
      addEventListener('keydown', event => {
        if (event.key === 'Escape') { portal.hidden = true; host.removeAttribute('aria-hidden'); }
      });
      document.querySelector('#cancel').onclick = () => settings.hidden = true;
    </script>`);
    if (mode === 'transition') {
      const result = await verifyKetcherNativeMenu(page);
      assert.deepEqual(await page.evaluate(() => window.initialMenu), { fontSize: '16px', padding: '6px 16px' });
      assert.deepEqual(result.item, { fontSize: '14px', minHeight: '28px', padding: '0px 8px' });
      assert.equal(result.owner, 'fixture-session');
    } else {
      await assert.rejects(verifyKetcherNativeMenu(page), error => {
        assert.equal(error.code, 'ERR_ASSERTION');
        assert.match(error.message, mode === 'wrong-owner'
          ? /Settings menu must keep its editor owner/ : /fontSize[\s\S]*16px[\s\S]*14px/);
        return true;
      });
    }
    cases.push(mode);
    await page.close();
  }
  console.log(JSON.stringify({ nativeMenuSettling: 'passed', cases }));
} finally {
  await context.close();
  await browser.close();
}
