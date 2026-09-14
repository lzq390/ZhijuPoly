// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import postcss from 'postcss';
import selectorParser from 'postcss-selector-parser';
import scopeKetcherCss from './scope-ketcher-css.mjs';

const sdkPath = '/fixture/node_modules/ketcher-react/dist/index.css';
const transform = async (css, from = sdkPath) => (await postcss([scopeKetcherCss()]).process(css, { from })).root;
afterEach(() => document.body.replaceChildren());

describe('Ketcher stylesheet isolation', () => {
  it('matches both a scoped portal root and SDK descendants without styling a host menu', async () => {
    document.body.innerHTML = `
      <div class="Select_menu" data-ketcher-owner="session"><li class="MuiMenuItem-root" id="portal-option"></li></div>
      <div data-editor-engine="react"><div class="Select_menu"><li class="MuiMenuItem-root" id="nested-option"></li></div></div>
      <div class="Select_menu" id="host-menu"><li class="MuiMenuItem-root" id="host-option"></li></div>`;
    const css = await transform('.Select_menu { max-height: 200px } .Select_menu .MuiMenuItem-root { font-size: 14px }');
    expect([...document.querySelectorAll(css.nodes[0].selector)]).toEqual([
      document.querySelector('[data-ketcher-owner]'), document.querySelector('[data-editor-engine] .Select_menu')
    ]);
    expect([...document.querySelectorAll(css.nodes[1].selector)].map(node => node.id)).toEqual(['portal-option', 'nested-option']);
    expect(document.querySelector('#host-menu').matches(css.nodes[0].selector)).toBe(false);
  });

  it('keeps functional selector lists and places the scope before pseudo-elements', async () => {
    document.body.innerHTML = '<div data-ketcher-owner="session" class="menu"><li class="option" id="option"></li></div>';
    const css = await transform(':is(.menu, .alternate) > :is(.option, .other) { color: red } .menu::before { content: "x" }');
    expect([...document.querySelectorAll(css.nodes[0].selector)].map(node => node.id)).toEqual(['option']);
    const selectors = selectorParser().astSync(css.nodes[1].selector).nodes;
    expect(selectors).toHaveLength(2);
    expect(selectors[1].last.value).toBe('::before');
    expect(selectors[1].nodes.at(-2).value).toBe(':where');
  });

  it('keeps SDK document variables on editor and portal roots and preserves keyframes', async () => {
    document.body.innerHTML = '<div data-editor-engine="react"></div><div data-ketcher-owner="session"></div>';
    const css = await transform(':root, html body { --sdk-color: red } @keyframes fade { from { opacity: 0 } to { opacity: 1 } } @font-face { font-family: SDK; src: url(sdk.woff2) }');
    expect([...document.querySelectorAll(css.nodes[0].selector)]).toEqual([...document.body.children]);
    expect(css.nodes[1].nodes.map(rule => rule.selector)).toEqual(['from', 'to']);
    expect(css.nodes[2].toString()).toBe('@font-face { font-family: SDK; src: url(sdk.woff2) }');
  });

  it('does not transform host stylesheets', async () => {
    const input = ':root { --host-color: red } .menu { color: blue }';
    expect((await transform(input, '/fixture/src/index.css')).toString()).toBe(input);
  });
});
