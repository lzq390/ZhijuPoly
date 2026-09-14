// Playwright 1.63 waitForFunction tests a Promise for truthiness before its
// value settles. Async SDK predicates must be awaited explicitly by the probe.
export async function pollBrowser(page, predicate, arg, { timeout = 15000 } = {}) {
  let stopped = false;
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`Browser condition timed out: ${predicate.toString().slice(0, 200)}`)), timeout);
  });
  const work = async () => {
    while (!stopped) {
      const value = await page.evaluate(predicate, arg);
      if (value) return value;
      await page.waitForTimeout(80);
    }
  };
  try { return await Promise.race([work(), deadline]); }
  finally { stopped = true; clearTimeout(timer); }
}
