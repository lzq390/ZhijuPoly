// Alternate matched before/after builds. Run this while no build or other browser probe is active.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createWriteStream } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const output = resolve(process.env.PROBE_OUTPUT_DIR || '/tmp/nexpoly-loading-performance');
await mkdir(output, { recursive: true });
assert.notEqual(process.env.PROBE_OVERLAP, 'true', 'No browser overlap workaround is permitted in acceptance');
const modes = [
  { name: 'development', baseline: process.env.PROBE_BASELINE_DEV_PORT || '5287', optimized: process.env.PROBE_OPTIMIZED_DEV_PORT || '5288' },
  { name: 'production', baseline: process.env.PROBE_BASELINE_PROD_PORT || '4287', optimized: process.env.PROBE_OPTIMIZED_PROD_PORT || '4288' }
];
const samples = [];
let browser;
function quantile(values, fraction) {
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.ceil(ordered.length * fraction) - 1];
}
for (let round = 1; round <= 3; round++) {
  for (const mode of modes) for (const variant of round % 2 ? ['baseline', 'optimized'] : ['optimized', 'baseline']) {
    const label = `${mode.name}-${variant}-${round}`;
    console.log(`Starting ${label}`);
    const log = createWriteStream(resolve(output, `${label}.log`));
    const code = await new Promise((done, reject) => {
      const child = spawn(process.execPath, [new URL('./analyze-structure-module-loading.mjs', import.meta.url).pathname], {
        env: { ...process.env, PROBE_PORT: mode[variant], PROBE_LABEL: label, PROBE_OUTPUT_DIR: output,
          PROBE_ACCEPTANCE: variant === 'optimized' ? 'true' : 'false', PROBE_OVERLAP: 'false', PROBE_WARMUP_ROUNDS: '1' }
      });
      child.stdout.pipe(log, { end: false });
      child.stderr.pipe(log, { end: false });
      child.on('error', reject);
      child.on('close', exit => { log.end(); done(exit); });
    });
    const data = JSON.parse(await readFile(resolve(output, `${label}.json`), 'utf8'));
    browser = data.browser;
    samples.push({ mode: mode.name, variant, round, label, exitCode: code, data });
    await writeFile(resolve(output, 'progress.json'), JSON.stringify(samples.map(({ data, ...item }) => ({ ...item, summary: data.summary, passed: data.passed })), null, 2));
    assert.equal(code, 0, `${label} failed; see its raw JSON and log`);
    console.log(JSON.stringify({ label, ...data.summary }));
  }
}
const comparisons = modes.map(({ name }) => {
  const variants = Object.fromEntries(['baseline', 'optimized'].map(variant => {
    const group = samples.filter(s => s.mode === name && s.variant === variant);
    const runs = group.flatMap(s => s.data.runs.filter(r => r.workersCreated));
    assert.equal(runs.length, 30);
    const metrics = Object.fromEntries(['afterAnimationMs', 'idleMs', 'interactiveMs'].map(key => [key,
      { p50: quantile(runs.map(r => r[key]), .5), p95: quantile(runs.map(r => r[key]), .95) }]));
    const frames = group.flatMap(s => s.data.trace.frames.filter(f => s.data.runs.some(r =>
      r.workersCreated && f.time >= r.click && f.time <= r.end)));
    const frameDeltas = frames.filter(f => ['exiting', 'entering'].includes(f.phase)).map(f => f.delta);
    const longTasks = runs.flatMap(r => r.events.filter(e => e.type === 'longtask').map(e => e.duration));
    return [variant, { ...metrics, samples: runs.length,
      visualFrameGapMs: { p50: quantile(frameDeltas, .5), p95: quantile(frameDeltas, .95), max: Math.max(...frameDeltas) },
      probeDurationMs: { p50: quantile(frames.map(f => f.probeDurationMs), .5), p95: quantile(frames.map(f => f.probeDurationMs), .95) },
      longTaskMs: { p50: quantile(longTasks, .5), p95: quantile(longTasks, .95), max: Math.max(...longTasks) },
      workersBeforeAnimationEnd: runs.filter(r => r.firstWorkerMs < r.idleMs).length,
      samplesDetail: runs.map(({ from, target, idleMs, readyMs, interactiveMs, afterAnimationMs, firstWorkerMs, workersCreated, exportedSmiles }) =>
        ({ from, target, idleMs, readyMs, interactiveMs, afterAnimationMs, firstWorkerMs, workersCreated, exportedSmiles })) }];
  }));
  const { baseline, optimized } = variants;
  const checks = {
    startsDuringTransition: optimized.workersBeforeAnimationEnd === 30,
    loadingTailMedianZero: optimized.afterAnimationMs.p50 === 0,
    loadingTailP95: optimized.afterAnimationMs.p95 <= 100,
    interactiveP50: optimized.interactiveMs.p50 <= baseline.interactiveMs.p50,
    interactiveP95: optimized.interactiveMs.p95 <= baseline.interactiveMs.p95,
    animationEndMedian: optimized.idleMs.p50 - baseline.idleMs.p50 <= 200
  };
  return { mode: name, ...variants, checks, passed: Object.values(checks).every(Boolean) };
});
const report = { date: new Date().toISOString(), browser, rounds: 3, perVariantPerMode: 30,
  sampling: 'One predeclared six-owner warmup per browser context, retained separately; nearest-rank quantiles; matched mode and document; before/after order reversed in round2; real implementation, no overlap interception',
  comparisons, passed: comparisons.every(c => c.passed) };
await writeFile(resolve(output, 'result.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report.comparisons.map(({ samplesDetail, baseline, optimized, ...item }) => ({ ...item,
  baseline: { afterAnimationMs: baseline.afterAnimationMs, interactiveMs: baseline.interactiveMs, idleMs: baseline.idleMs },
  optimized: { afterAnimationMs: optimized.afterAnimationMs, interactiveMs: optimized.interactiveMs, idleMs: optimized.idleMs } })), null, 2));
if (!report.passed) process.exitCode = 1;
