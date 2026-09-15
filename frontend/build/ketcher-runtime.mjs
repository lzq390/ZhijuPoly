import { build } from 'vite';
import react from '@vitejs/plugin-react';
import inject from '@rollup/plugin-inject';
import { createHash, randomUUID } from 'node:crypto';
import { readFile, readdir, mkdir, writeFile, rename, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { gzipSync, brotliCompressSync, constants } from 'node:zlib';
import scopeKetcherCss from './scope-ketcher-css.mjs';
import postcss from 'postcss';

const root = fileURLToPath(new URL('../', import.meta.url));
const digest = value => createHash('sha256').update(value).digest('hex');
const sourcePath = path.join(root, 'node_modules/ketcher-standalone/dist/main.js');
async function inputs() {
  const names = ['package-lock.json', 'postcss.config.js', 'src/styles/ketcher-native.css',
    'build/scope-ketcher-css.mjs', 'build/browser-process.mjs', 'scripts/verify-ketcher-patches.mjs'];
  for (const directory of ['sdk', 'patches']) for (const name of (await readdir(path.join(root, directory))).sort()) {
    if (!name.endsWith('.test.mjs') && !name.endsWith('.md')) names.push(directory + '/' + name);
  }
  names.push('build/ketcher-runtime.mjs');
  const values = await Promise.all(names.map(async name => [name, digest(await readFile(path.join(root, name)))]));
  // Installed bytes are included during development and verified by the patch manifest for release.
  for (const name of ['ketcher-core/dist/index.modern.js', 'ketcher-react/dist/index.js',
    'ketcher-react/dist/index.modern-c0bcdf82.js', 'ketcher-standalone/dist/main.js',
    'ketcher-react/dist/index.css', 'ketcher-macromolecules/dist/index.css']) {
    values.push([name, digest(await readFile(path.join(root, 'node_modules', name)))]);
  }
  return Object.fromEntries(values);
}
let inflight;
export function ensureKetcherRuntime() {
  return inflight ||= buildRuntime().finally(() => { inflight = undefined; });
}
async function buildRuntime() {
  await promisify(execFile)(process.execPath, [path.join(root, 'scripts/verify-ketcher-patches.mjs')], { cwd: root });
  const patchManifest = JSON.parse(await readFile(path.join(root, 'patches/ketcher-manifest.json'), 'utf8'));
  const sources = await inputs();
  const version = digest(JSON.stringify(sources)).slice(0, 20);
  const directory = path.join(root, '.ketcher-runtime', version);
  const manifestFile = path.join(directory, 'manifest.json');
  if (existsSync(manifestFile)) {
    const manifest = JSON.parse(await readFile(manifestFile, 'utf8'));
    for (const asset of manifest.assets) {
      if (digest(await readFile(path.join(directory, asset.file))) !== asset.sha256) throw new Error('Corrupt SDK asset: ' + asset.file);
    }
    return { directory, manifest };
  }
  if (process.argv[1] !== fileURLToPath(import.meta.url)) {
    // Vite serve sets NODE_ENV globally. A separate process prevents development
    // JSX helpers from being paired with the production React runtime.
    const { stdout } = await promisify(execFile)(process.execPath, [fileURLToPath(import.meta.url)], {
      cwd: root, env: { ...process.env, NODE_ENV: 'production' }, maxBuffer: 8 * 1024 * 1024
    });
    const result = JSON.parse(stdout);
    return { directory: result.directory, manifest: JSON.parse(await readFile(path.join(result.directory, 'manifest.json'), 'utf8')) };
  }
  process.env.NODE_ENV = 'production';
  const temporary = directory + '.building-' + process.pid + '-' + randomUUID();
  await mkdir(temporary, { recursive: true });
  const standalone = await readFile(sourcePath, 'utf8');
  const match = standalone.match(/var WorkerFactory = createBase64WorkerFactory\('([A-Za-z0-9+/=]+)', null, false\);/);
  if (!match) throw new Error('Unsupported standalone Worker wrapper');
  const decoded = Buffer.from(match[1], 'base64').toString('utf8');
  // Match createURL: its first line is the loader comment, not part of the Blob.
  const worker = Buffer.from(decoded.slice(decoded.indexOf('\n') + 1));
  if (digest(worker) !== '410080855a8dc69cf25b6854dbd8ac1c96c4ab10c67c17affc2cde1bfc736f8e') throw new Error('Worker extraction changed bytes');
  const micro = path.join(root, 'sdk/micro.tsx');
  const macro = path.join(root, 'sdk/macro.mjs');
  const macroCss = (await readFile(path.join(root, 'node_modules/ketcher-macromolecules/dist/index.css'), 'utf8'))
    .replace(/\s*\/\*# sourceMappingURL=[^*]+\*\/\s*$/, '').trim();
  const css = await readFile(path.join(root, 'node_modules/ketcher-react/dist/index.css'), 'utf8');
  if (!css.startsWith(macroCss)) throw new Error('Macro CSS no longer matches the React package prefix');
  const macroRules = postcss.parse(macroCss);
  const commonVariables = macroRules.nodes.filter(rule => rule.selector === ':root').map(rule => rule.toString()).join('\n');
  macroRules.nodes.filter(rule => rule.selector === ':root').forEach(rule => rule.remove());
  const macroStyle = (await postcss([scopeKetcherCss()]).process(macroRules, { from: undefined })).css;
  let microModules;
  let manifest;
  let workerRef, macroRef, microRef, macroStyleRef;
  const runtimePlugin = {
    name: 'ketcher-independent-runtime',
    enforce: 'pre',
    buildStart() {
      workerRef = this.emitFile({ type: 'asset', name: 'indigo-worker.js', source: worker });
      macroStyleRef = this.emitFile({ type: 'asset', name: 'macro.css', source: macroStyle });
      macroRef = this.emitFile({ type: 'chunk', id: macro, name: 'macro', preserveSignature: 'allow-extension' });
      microRef = this.emitFile({ type: 'chunk', id: micro, name: 'micro', preserveSignature: 'allow-extension' });
    },
    resolveId(id) { if (id === 'virtual:ketcher-assets') return '\0' + id; },
    load(id) {
      if (id === '\0virtual:ketcher-assets') return `export const workerUrl=import.meta.ROLLUP_FILE_URL_${workerRef};export const macroUrl=import.meta.ROLLUP_FILE_URL_${macroRef};export const microUrl=import.meta.ROLLUP_FILE_URL_${microRef};export const macroStyleUrl=import.meta.ROLLUP_FILE_URL_${macroStyleRef};`;
    },
    transform(code, id) {
      if (id === path.join(root, 'node_modules/ketcher-react/dist/index.css')) return { code: commonVariables + code.slice(macroCss.length), map: null };
      if (id === sourcePath) return { code: code.replace(match[0], 'var WorkerFactory = function () { throw new Error("Runtime requires an owned external Worker"); };'), map: null };
      if (id.endsWith('/ketcher-react/dist/index.js')) return { code: code.replace(
        /function nexpolyLoadMacroModule\(\) \{ return import\([^;]+; \}/,
        'function nexpolyLoadMacroModule() { return nexpolyRuntimeLoadMacro(); }') + '\nimport { loadMacro as nexpolyRuntimeLoadMacro } from ' + JSON.stringify(path.join(root, 'sdk/bootstrap.mjs')) + ';', map: null };
    },
    generateBundle(_options, bundle) {
      const chunks = Object.values(bundle).filter(item => item.type === 'chunk');
      if (chunks.some(item => item.code.includes('.jsxDEV('))) throw new Error('Development JSX in production SDK');
      const boot = chunks.find(item => item.facadeModuleId === path.join(root, 'sdk/bootstrap.mjs'));
      const macroChunk = chunks.find(item => item.modules[macro]);
      if (!boot || !macroChunk) throw new Error('Missing runtime entries');
      const closure = (file, visited = new Set()) => {
        if (visited.has(file)) return visited;
        visited.add(file);
        for (const dependency of bundle[file]?.imports || []) closure(dependency, visited);
        return visited;
      };
      const bootFiles = closure(boot.fileName);
      for (const file of bootFiles) if (Object.keys(bundle[file]?.modules || {}).some(id => /node_modules\/(react|react-dom|ketcher-core|ketcher-react)\//.test(id))) throw new Error('Heavy module in bootstrap: ' + JSON.stringify(chunks.map(item => ({file:item.fileName, imports:item.imports, dynamic:item.dynamicImports, ids:Object.keys(item.modules).filter(id => !id.includes('node_modules')).slice(0,25)}))));
      const microChunk = chunks.find(item => item.modules[micro]);
      const microFiles = closure(microChunk.fileName);
      if (microFiles.has(macroChunk.fileName)) throw new Error('Macro in small-molecule startup graph');
      if (macroChunk.imports.some(file => !microFiles.has(file)) || macroChunk.dynamicImports.length) throw new Error('Macro retry has an unloaded child dependency');
      manifest = { schema: 1, version, sources, patchRevision: patchManifest.revision,
        workerSha256: digest(worker), bootstrap: boot.fileName, macro: macroChunk.fileName,
        macroStyle: this.getFileName(macroStyleRef),
        micro: microChunk.fileName, style: 'ketcher.css', graph: chunks.map(item => ({ file: item.fileName, imports: item.imports, dynamicImports: item.dynamicImports })), assets: [] };
    }
  };
  try {
    await build({ root, configFile: false, publicDir: false, base: './', logLevel: 'warn', mode: 'production',
      esbuild: { jsxDev: false },
      define: { global: 'globalThis', 'process.env.NODE_ENV': '"production"' },
      plugins: [react({ jsxRuntime: 'classic' }), runtimePlugin, { ...inject({ include: [/node_modules\/(?:ketcher-[^/]+|assert|util)\//], process: [path.join(root, 'build/browser-process.mjs'), 'process'] }), enforce: 'post' }],
      css: { postcss: { plugins: [scopeKetcherCss()] } },
      build: { outDir: temporary, emptyOutDir: true, cssCodeSplit: false, minify: 'esbuild', target: 'es2022',
        commonjsOptions: { transformMixedEsModules: true },
        rollupOptions: { input: { bootstrap: path.join(root, 'sdk/bootstrap.mjs') },
          preserveEntrySignatures: 'allow-extension', output: { onlyExplicitManualChunks: true, entryFileNames: '[name]-[hash].js', chunkFileNames: '[name]-[hash].js',
            assetFileNames: info => /\.css$/.test(info.names?.[0] || info.name || '') && (info.names?.[0] || info.name) !== 'macro.css' ? 'ketcher.css' : '[name]-[hash][extname]',
            manualChunks(id, api) {
              if (!microModules) {
                microModules = new Set();
                const walk = id => { if (microModules.has(id)) return; microModules.add(id); for (const child of api.getModuleInfo(id)?.importedIds || []) walk(child); };
                walk(micro);
              }
              if (id === macro || /ketcher-react\/dist\/index.modern-c0bcdf82/.test(id)) return 'macro';
              if (id.includes('node_modules')) return microModules.has(id) ? 'shared' : 'macro';
            }
          } }
      }
    });
    for (const file of await readdir(temporary)) {
      const data = await readFile(path.join(temporary, file));
      const role = file === manifest.bootstrap ? 'bootstrap' : file === manifest.micro ? 'micro' : file === manifest.macro ? 'macro' :
        file === manifest.macroStyle ? 'macro-style' : file === manifest.style ? 'micro-style' : file.startsWith('indigo-worker-') ? 'worker' : 'shared';
      manifest.assets.push({ file, role, bytes: data.length, sha256: digest(data) });
      if (data.length >= 32768 && /\.(js|css|wasm)$/.test(file)) {
        for (const [suffix, compressed] of [['gz', gzipSync(data, { level: 4 })], ['br', brotliCompressSync(data, { params: { [constants.BROTLI_PARAM_QUALITY]: 4 } })]]) {
          await writeFile(path.join(temporary, file + '.' + suffix), compressed);
          manifest.assets.push({ file: file + '.' + suffix, role, encoding: suffix === 'gz' ? 'gzip' : 'br', bytes: compressed.length, sha256: digest(compressed) });
        }
      }
    }
    if (JSON.stringify(await inputs()) !== JSON.stringify(sources)) {
      await rm(temporary, { recursive: true, force: true });
      return buildRuntime();
    }
    await writeFile(path.join(temporary, 'manifest.json'), JSON.stringify(manifest, null, 2));
    try { await rename(temporary, directory); }
    catch (error) {
      if (!['EEXIST', 'ENOTEMPTY'].includes(error.code)) throw error;
      const winner = JSON.parse(await readFile(manifestFile, 'utf8'));
      if (JSON.stringify(winner.assets) !== JSON.stringify(manifest.assets)) throw new Error('Concurrent SDK builds produced different assets');
      await rm(temporary, { recursive: true, force: true });
    }
    return { directory, manifest };
  } catch (error) { await rm(temporary, { recursive: true, force: true }); throw error; }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const result = await ensureKetcherRuntime();
  console.log(JSON.stringify({ directory: result.directory, bootstrap: result.manifest.bootstrap, assets: result.manifest.assets.map(({file,bytes}) => ({file,bytes})) }, null, 2));
}
