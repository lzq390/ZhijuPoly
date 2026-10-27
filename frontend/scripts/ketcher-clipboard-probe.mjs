// Browser probe only: both supported SDKs dispatch this event after their
// debounced paste merge candidates have been initialized in the editor window.
export async function clearForClipboardPaste(page) {
  await page.evaluate(() => window.__probeEditor().setMolecule(''));
  // SDK 3.7 resolves before its worker import updates the canvas. Overlapping
  // that clear with a paste can delete the atoms used by its delayed merge.
  await page.waitForFunction(() => window.__probeEditor().editor.struct().atoms.size === 0);
}

export async function pasteClipboardAt(page, point) {
  // Ensure the later move has a different coordinate even when the canvas was
  // just clicked at point. A click's implicit move must not race paste commit.
  await page.mouse.move(point.x + 1, point.y);
  await page.evaluate(() => {
    const editor = window.__probeEditor().editor;
    const win = document.querySelector('[data-structure-editor] iframe')?.contentWindow || window;
    const state = { editor, win, tool: null, readyAction: null, events: 0 };
    state.onReady = () => {
      const tool = editor._tool;
      if (!tool?.struct?.atoms || !tool.action) return;
      if (!state.tool) state.tool = tool;
      if (state.tool === tool) {
        state.readyAction = tool.action;
        state.events += 1;
      }
    };
    win.addEventListener('itemsToMergeInitializationComplete', state.onReady);
    window.__probePaste = state;
  });
  try {
    await page.keyboard.press('Control+v');
    await page.waitForFunction(() => {
      const state = window.__probePaste;
      return state.tool && state.editor._tool === state.tool && state.readyAction === state.tool.action;
    });
    await page.evaluate(() => {
      const state = window.__probePaste;
      state.initialAction = state.tool.action;
      state.readyAction = null;
    });
    await page.mouse.move(point.x, point.y);
    await page.waitForFunction(() => {
      const state = window.__probePaste;
      return state.editor._tool === state.tool && state.tool.action !== state.initialAction &&
        state.readyAction === state.tool.action;
    });
    // No further move: mousemove clears mergeItems and starts another debounce.
    // null mergeItems is a valid initialized result when nothing can be fused.
    await page.mouse.down();
    await page.mouse.up();
    // The preview already contains atoms. Tool exit proves the paste committed.
    await page.waitForFunction(() => window.__probePaste.editor._tool !== window.__probePaste.tool);
    return await page.evaluate(() => ({ readyEvents: window.__probePaste.events, committed: true }));
  } finally {
    await page.evaluate(() => {
      const state = window.__probePaste;
      state.win.removeEventListener('itemsToMergeInitializationComplete', state.onReady);
      delete window.__probePaste;
    });
  }
}
