// Browser probe only: both supported SDKs dispatch this event after their
// debounced paste merge candidates have been initialized in the editor window.
export async function clearForClipboardPaste(page) {
  // Delete is context sensitive: over an atom it erases that atom even when
  // the whole graph is selected. Use the SDK's explicit native clear action.
  const iframe = await page.locator('[data-structure-editor] iframe').count();
  const ui = iframe ? page.frameLocator('[data-structure-editor] iframe') : page;
  await ui.locator('[data-testid="clear-canvas"]:visible:enabled').click();
  await page.waitForFunction(() => {
    const editor = window.__probeEditor().editor;
    const draft = document.querySelector('[aria-label="SMILES 输入，自动同步到画板"]');
    // The raw graph clears synchronously; the host publishes that change later.
    return editor.struct().atoms.size === 0 && draft?.value === '' && !draft.disabled;
  });
}

export async function clipboardProbeState(page) {
  return page.evaluate(() => {
    const editor = window.__probeEditor().editor;
    const win = document.querySelector('[data-structure-editor] iframe')?.contentWindow || window;
    const draft = document.querySelector('[aria-label="SMILES 输入，自动同步到画板"]');
    const hovered = {};
    for (const [kind, items] of Object.entries(editor.render?.ctab || {})) {
      if (items instanceof win.Map) hovered[kind] = [...items].filter(([, item]) => item.hover).map(([id]) => id);
    }
    return { atomIds: [...editor.struct().atoms.keys()], selection: editor.selection(), hovered,
      tool: editor._tool?.constructor?.name, active: win.document.activeElement?.outerHTML?.slice(0, 300),
      focused: win.document.hasFocus(), macro: win.isPolymerEditorTurnedOn,
      keys: window.__probeClipboardKeys || [], draft: draft && { value: draft.value, disabled: draft.disabled } };
  });
}

export async function pasteClipboardAt(page, point, { exerciseCancel = false } = {}) {
  // Preserve canvas keyboard focus while establishing an outside pointer.
  // Async clipboard UI can change which element is under an unmoved pointer.
  await page.mouse.move(1, 1);
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
    await page.evaluate(() => { window.__probePaste.beforeEntry = window.__probePaste.tool.action; });
    await waitCanvasHit(page, { x: point.x + 1, y: point.y });
    await page.mouse.move(point.x + 1, point.y);
    await page.waitForFunction(() => {
      const state = window.__probePaste;
      return state.editor._tool === state.tool && state.tool.action && state.tool.action !== state.beforeEntry &&
        state.readyAction === state.tool.action;
    });
    if (exerciseCancel) {
      // The SDK releases its mousemove guard on its next animation frame.
      await page.evaluate(() => new Promise(resolve => window.__probePaste.win.requestAnimationFrame(resolve)));
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
      await waitCanvasHit(page, { x: point.x + 1, y: point.y });
      await page.mouse.move(point.x + 1, point.y);
    }
    await page.waitForFunction(() => {
      const state = window.__probePaste;
      return state.tool.action && state.editor._tool === state.tool && state.readyAction === state.tool.action;
    });
    await page.evaluate(() => new Promise(resolve => window.__probePaste.win.requestAnimationFrame(resolve)));
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

async function waitCanvasHit(page, point) {
  await page.waitForFunction(point => {
    const frame = document.querySelector('[data-structure-editor] iframe');
    const win = frame?.contentWindow || window;
    const rect = frame?.getBoundingClientRect();
    const scale = rect ? rect.width / win.innerWidth : 1;
    const hit = win.document.elementFromPoint((point.x - (rect?.x || 0)) / scale, (point.y - (rect?.y || 0)) / scale);
    return window.__probePaste.editor.render.clientArea.contains(hit) && hit?.nodeName !== 'DIV';
  }, point);
}
