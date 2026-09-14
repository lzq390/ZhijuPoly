import assert from "node:assert/strict";

// Sample geometry as well as opacity: a drawer can animate perfectly while its
// sibling jumps on the first/last frame. The action must not create real jobs.
export async function checkDrawerSlide(page, { workspace, drawer, action, open, inline, reduced = false }) {
  await page.evaluate(({ workspace, drawer, open }) => {
    const panel = document.querySelector(drawer);
    const content = document.querySelector(workspace);
    const owner = panel.closest(".np-structure-workbench, .ks-panel-layout, .polytao-page");
    const probe = { running: true, frames: [], durations: [], editor: content?.querySelector("[data-structure-editor], iframe") };
    window.__drawerSlideProbe = probe;
    window.__drawerSlideSequence = (window.__drawerSlideSequence || 0) + 1;
    probe.mark = `np-drawer-slide-${window.__drawerSlideSequence}-${open ? "open" : "close"}`;
    performance.mark(`${probe.mark}:start`);
    const start = performance.now();
    const sample = () => {
      if (!probe.running) return;
      const w = content.getBoundingClientRect();
      const d = panel.getBoundingClientRect();
      const nav = document.querySelector(".np-sidebar-desktop")?.getBoundingClientRect();
      for (const animation of panel.getAnimations()) {
        const duration = animation.effect.getTiming().duration;
        if (!probe.durations.includes(duration)) probe.durations.push(duration);
      }
      probe.frames.push({ time: performance.now() - start, x: w.x, width: w.width, drawerX: d.x,
        phase: panel.dataset.motionPhase, ownerScroll: owner?.scrollLeft, sidebarX: nav?.x, sidebarWidth: nav?.width });
      requestAnimationFrame(sample);
    };
    sample();
  }, { workspace, drawer, open });
  await action();
  await page.waitForFunction(({ drawer, open }) => document.querySelector(drawer)?.dataset.motionPhase === (open ? "open" : "closed"), { drawer, open });
  // Include frames *after* completion; that is where the old slot removal jumped.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const result = await page.evaluate(({ workspace }) => {
    const probe = window.__drawerSlideProbe;
    probe.running = false;
    performance.mark(`${probe.mark}:end`);
    return { frames: probe.frames, durations: probe.durations, editorRetained: !probe.editor || probe.editor === document.querySelector(workspace)?.querySelector("[data-structure-editor], iframe") };
  }, { workspace });
  const { frames } = result;
  const first = frames[0], last = frames.at(-1);
  const between = (value, a, b) => value > Math.min(a, b) + 1 && value < Math.max(a, b) - 1;
  const workspaceFrames = frames.filter(frame => between(frame.x, first.x, last.x)).length;
  const drawerFrames = frames.filter(frame => between(frame.drawerX, first.drawerX, last.drawerX)).length;
  assert.ok(result.editorRetained, "Sliding must not remount the workspace editor");
  assert.ok(frames.every(frame => Math.abs(frame.width - first.width) < 1), "Workspace width must remain stable across drawer open/close");
  assert.ok(frames.every(frame => frame.sidebarX === first.sidebarX && frame.sidebarWidth === first.sidebarWidth), "Platform navigation must stay fixed");
  assert.ok(frames.every(frame => frame.ownerScroll === first.ownerScroll), "Focusing an entering drawer must not scroll the outer workspace frame");
  if (!reduced) {
    assert.ok(result.durations.includes(open ? 400 : 320), "The real drawer animation must use the shared enter/exit timing");
    if (inline) {
      assert.ok(workspaceFrames >= 3, "Inline workspace must translate through intermediate positions, including exit completion");
      assert.ok((last.x - first.x) * (open ? -1 : 1) > 10, "Workspace must move in the same direction as the drawer");
      assert.ok(frames.some(frame => between(frame.x, first.x, last.x) && between(frame.drawerX, first.drawerX, last.drawerX)), "Both regions must move at the same time");
      assert.ok(frames.every(frame => Math.abs((frame.x - first.x) / (last.x - first.x) - (frame.drawerX - first.drawerX) / (last.drawerX - first.drawerX)) < .08), "Workspace and drawer must follow the same progress, without a first/last-frame jump");
    } else assert.ok(frames.every(frame => Math.abs(frame.x - first.x) < 1), "Overlay must not displace its background workspace");
    assert.ok(drawerFrames >= 3, "Drawer must slide through intermediate positions");
    assert.ok(Math.abs(last.drawerX - first.drawerX) > 100, "Drawer must travel in/out, not just fade over 20px");
  }
  return { inline, workspaceDelta: Math.round(last.x - first.x), workspaceWidth: Math.round(first.width),
    drawerTravel: Math.round(Math.abs(last.drawerX - first.drawerX)), workspaceFrames, drawerFrames, durations: result.durations, editorRetained: result.editorRetained };
}

/** Reverse an opening transition before completion using the real close action. */
export async function checkDrawerReversal(page, { workspace, drawer, openAction, closeAction }) {
  const before = await page.locator(workspace).boundingBox();
  await openAction();
  await page.waitForFunction(drawer => {
    const panel = document.querySelector(drawer);
    const opacity = Number(getComputedStyle(panel).opacity);
    return panel.dataset.motionPhase === "entering" && opacity > .15 && opacity < .8;
  }, drawer);
  await closeAction();
  await page.waitForFunction(drawer => document.querySelector(drawer)?.dataset.motionPhase === "closed", drawer);
  const after = await page.locator(workspace).boundingBox();
  assert.ok(Math.abs(before.x - after.x) < 1 && Math.abs(before.width - after.width) < 1, "Interrupted entrance must return to the original centered geometry");
  await openAction();
  await page.waitForFunction(drawer => document.querySelector(drawer)?.dataset.motionPhase === "open", drawer);
  await closeAction();
  await page.waitForFunction(drawer => document.querySelector(drawer)?.dataset.motionPhase === "closed", drawer);
  assert.equal(await page.locator(drawer).evaluate(panel => panel.inert), true);
  return true;
}
