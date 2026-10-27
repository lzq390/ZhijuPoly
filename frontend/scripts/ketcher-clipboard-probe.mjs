// Browser probe only: both supported SDKs dispatch this event after their
// debounced paste merge candidates have been initialized in the editor window.
export async function clearForClipboardPaste(page) {
  // Copy deselects the graph. Select it again and delete through the native
  // keyboard path, without another asynchronous import before the paste.
  await page.keyboard.press('Control+a');
  await page.waitForFunction(() => {
    const editor = window.__probeEditor().editor;
    const win = document.querySelector('[data-structure-editor] iframe')?.contentWindow || window;
    const count = editor.struct().atoms.size;
    const cut = win.document.querySelector('[data-testid="cut-button"]');
    return count > 0 && editor.selection()?.atoms?.length === count && cut && !cut.disabled;
  });
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => window.__probeEditor().editor.struct().atoms.size === 0);
}

export async function pasteClipboardAt(page, point, { exerciseCancel = false } = {}) {
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
    await page.waitForFunction(() => Boolean(window.__probePaste.editor._tool?.struct?.atoms));
    await page.evaluate(() => {
      const state = window.__probePaste;
      state.tool = state.editor._tool;
    });
    if (exerciseCancel) {
      // A paste remains the active tool after leaving the canvas, but its
      // preview is rolled back. Exercise real pointer cancellation, including
      // when that legal cancellation happened before the initial ready event.
      await page.mouse.move(1, 1);
      await page.waitForFunction(() => {
        const state = window.__probePaste;
        return state.editor._tool === state.tool && !state.tool.action &&
          state.editor.struct().atoms.size === 0;
      });
      await page.evaluate(() => { window.__probePaste.readyAction = null; });
      await page.mouse.move(point.x + 1, point.y);
    }
    await page.waitForFunction(() => {
      const state = window.__probePaste;
      return state.tool.action && state.editor._tool === state.tool && state.readyAction === state.tool.action;
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
    const result = await page.evaluate(() => ({ readyEvents: window.__probePaste.events, committed: true }));
    if (exerciseCancel) result.canceledPreviewResumed = true;
    return result;
  } catch (error) {
    const state = await page.evaluate(() => {
      const state = window.__probePaste, tool = state.editor._tool;
      return { readyEvents: state.events, atomIds: [...state.editor.struct().atoms.keys()],
        pasteTool: Boolean(tool?.struct?.atoms), action: Boolean(tool?.action) };
    });
    error.stack += `\nPaste state: ${JSON.stringify(state)}`;
    throw error;
  } finally {
    await page.evaluate(() => {
      const state = window.__probePaste;
      state.win.removeEventListener('itemsToMergeInitializationComplete', state.onReady);
      delete window.__probePaste;
    });
  }
}
