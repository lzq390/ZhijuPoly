import { readFile } from 'node:fs/promises';
import path from 'node:path';

export function negotiateAsset(assets, file, acceptEncoding = '') {
  const qualities = new Map(String(acceptEncoding).split(',').map(value => {
    const [encoding, ...parameters] = value.trim().toLowerCase().split(';');
    const declared = parameters.map(value => value.trim()).find(value => value.startsWith('q='));
    const q = declared ? Number(declared.slice(2)) : 1;
    return [encoding, Number.isFinite(q) && q >= 0 && q <= 1 ? q : 0];
  }));
  const quality = encoding => qualities.get(encoding) ?? qualities.get('*') ?? 0;
  const identity = qualities.get('identity') ?? (qualities.get('*') === 0 ? 0 :
    Math.min(1, ...[...qualities.values()].filter(value => value > 0)));
  return [{ encoding: 'br', suffix: '.br', quality: quality('br') },
    { encoding: 'gzip', suffix: '.gz', quality: quality('gzip') },
    { suffix: '', quality: identity }]
    .map(value => ({ ...value, asset: assets.find(asset => asset.file === file + value.suffix) }))
    .filter(value => value.quality > 0 && value.asset)
    .sort((a, b) => b.quality - a.quality)[0];
}

/** Versioned artifacts remain available to mounted sessions after an HMR rebuild. */
export function runtimeMiddleware(getRuntime, base = '/') {
  const versions = new Map();
  const prefix = `${base}assets/ketcher/`;
  return async function serve(request, response, next) {
    const url = new URL(request.url || '/', 'http://localhost');
    if (!url.pathname.startsWith(prefix)) return next();
    const match = /^([a-f0-9]{20})\/(?:retry\/\d+\/)?([^/]+)$/.exec(url.pathname.slice(prefix.length));
    if (!match) { response.statusCode = 404; response.end(); return; }
    const [, version, file] = match;
    const current = getRuntime();
    versions.set(current.manifest.version, current);
    let runtime = versions.get(version);
    if (!runtime) {
      const directory = path.join(path.dirname(current.directory), version);
      try {
        const manifest = JSON.parse(await readFile(path.join(directory, 'manifest.json'), 'utf8'));
        if (manifest.version === version) { runtime = { directory, manifest }; versions.set(version, runtime); }
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    const asset = runtime?.manifest.assets.find(item => item.file === file && !/\.(br|gz)$/.test(file));
    if (!asset) { response.statusCode = 404; response.end('Unknown SDK resource'); return; }
    if (!['GET', 'HEAD'].includes(request.method || 'GET')) {
      response.statusCode = 405; response.setHeader('Allow', 'GET, HEAD'); response.end(); return;
    }
    response.setHeader('Vary', 'Accept-Encoding');
    const selection = negotiateAsset(runtime.manifest.assets, file, request.headers['accept-encoding']);
    if (!selection) { response.statusCode = 406; response.end(); return; }
    const selected = selection.asset;
    const etag = `"${selected.sha256}"`;
    response.setHeader('Cache-Control', 'public, max-age=31536000, immutable');
    response.setHeader('ETag', etag);
    response.setHeader('Content-Type', file.endsWith('.css') ? 'text/css; charset=utf-8' :
      file.endsWith('.wasm') ? 'application/wasm' : 'text/javascript; charset=utf-8');
    if (selection.encoding) response.setHeader('Content-Encoding', selection.encoding);
    if (String(request.headers['if-none-match'] || '').split(',').some(value => value.trim() === '*' || value.trim().replace(/^W\//, '') === etag)) {
      response.statusCode = 304; response.end(); return;
    }
    response.setHeader('Content-Length', selected.bytes);
    if (request.method === 'HEAD') { response.end(); return; }
    response.end(await readFile(path.join(runtime.directory, selected.file)));
  };
}
