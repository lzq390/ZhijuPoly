// Reproducible runtime gate for a dependency or patch update. Business probes
// intercept all APIs, so this never creates jobs or contacts production.
import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const cwd = fileURLToPath(new URL('../', import.meta.url));
const output = resolve(process.env.STRUCTURE_GATE_ARTIFACT_DIR || '/tmp/nexpoly-structure-gate');
await mkdir(output, { recursive: true });
const servers = [];
function child(args, env = {}, server = false) {
  const process = spawn('npm', args, { cwd, stdio: 'inherit', env: { ...globalThis.process.env, ...env }, detached: server });
  if (server) servers.push(process);
  return process;
}
function run(args, env) {
  return new Promise((resolve, reject) => {
    const process = child(args, env);
    process.on('error', reject);
    process.on('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`npm ${args.join(' ')} failed (${code ?? signal})`)));
  });
}
async function serve(port, engine, dist, document = '/') {
  const args = ['run', dist ? 'preview' : 'dev', '--', '--host', '127.0.0.1', '--port', String(port), '--strictPort'];
  if (dist) args.push('--outDir', dist);
  else args.push('--force');
  const process = child(args, { VITE_STRUCTURE_EDITOR_ENGINE: engine }, true);
  const url = `http://127.0.0.1:${port}`;
  for (let attempt = 0; attempt < 100; ++attempt) {
    if (process.exitCode !== null) throw new Error(`Browser gate server ${port} exited`);
    try { if ((await fetch(url + document, { signal: AbortSignal.timeout(1000) })).ok) return url; } catch { /* Server starting. */ }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error(`Browser gate server ${port} did not start`);
}
try {
  const native = resolve(output, 'build-react'), iframe = resolve(output, 'build-iframe'), sdk = resolve(output, 'build-sdk');
  await run(['run', 'build', '--', '--outDir', native], { VITE_STRUCTURE_EDITOR_ENGINE: 'react' });
  await run(['run', 'test:structure-resources', '--', native]);
  await run(['run', 'build', '--', '--outDir', iframe], { VITE_STRUCTURE_EDITOR_ENGINE: 'iframe' });
  await run(['run', 'test:structure-resources', '--', iframe]);
  await run(['run', 'build:ketcher-compat', '--', '--outDir', sdk]);
  const nativeUrl = await serve(4280, 'react', native);
  const iframeUrl = await serve(4281, 'iframe', iframe);
  const sdkUrl = await serve(4282, 'react', sdk, '/ketcher-compat.html');
  const devUrl = await serve(5280, 'react');
  for (const [mode, url] of [['development', devUrl], ['production', sdkUrl]]) {
    await run(['run', 'test:ketcher-compat'], { KETCHER_COMPAT_MODE: mode, KETCHER_COMPAT_URL: `${url}/ketcher-compat.html`, KETCHER_COMPAT_ARTIFACTS: resolve(output, `sdk-${mode}`) });
  }
  for (const [mode, url] of [['react-development', devUrl], ['react-production', nativeUrl], ['iframe-production', iframeUrl]]) {
    await run(['run', 'test:structure-browser'], { STRUCTURE_BASE_URL: url, STRUCTURE_ARTIFACT_DIR: resolve(output, mode) });
  }
  await run(['run', 'test:structure-ai'], { STRUCTURE_BASE_URL: nativeUrl, STRUCTURE_ARTIFACT_DIR: resolve(output, 'ai') });
} finally {
  for (const process of servers) {
    if (process.pid) try { globalThis.process.kill(-process.pid, 'SIGTERM'); } catch { /* Already exited. */ }
  }
}
