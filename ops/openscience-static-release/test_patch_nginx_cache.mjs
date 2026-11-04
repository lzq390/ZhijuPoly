import assert from "node:assert/strict";
import test from "node:test";
import { patchNginxCache } from "./patch-nginx-cache.mjs";

const routes = `    listen 4454;
    root /usr/share/nginx/html;
    location = /healthz { return 200 'ok'; }
    location ^~ /openscience-api/ {
      set $backend openscience-backend:4096;
      proxy_pass http://$backend;
      proxy_set_header Host localhost;
    }
    location ^~ /knowledge-api/ { proxy_pass http://knowledge:8000; }
`;
const fixture = `events {}\nhttp {\n  server {\n${routes}    location ^~ /assets/ {
      try_files $uri =404;
      expires 1y;
      add_header Cache-Control 'public, immutable';
    }
    location / {
      try_files $uri $uri/ /index.html;
    }
  }
}\n`;

test("preserves upstream routes and gives index, SPA routes and mutable root files revalidation", () => {
  const patched = patchNginxCache(fixture);
  assert.ok(patched.includes(routes));
  assert.match(patched, /location = \/index\.html \{\n\s+try_files \$uri =404;\n\s+expires off;\n\s+add_header Cache-Control "no-cache, must-revalidate" always;/);
  assert.match(patched, /location \/ \{\n\s+try_files \$uri \$uri\/ \/index\.html;\n\s+expires off;\n\s+add_header Cache-Control "no-cache, must-revalidate" always;/);
  assert.equal(patchNginxCache(patched), patched);
});

test("successful assets are immutable and missing assets use a separate no-store response", () => {
  const patched = patchNginxCache(fixture);
  assert.match(patched, /try_files \$uri @nexpoly_static_not_found;/);
  assert.match(patched, /add_header Cache-Control "public, max-age=31536000, immutable";/);
  assert.match(patched, /location @nexpoly_static_not_found \{\n\s+expires off;\n\s+add_header Cache-Control "no-store" always;\n\s+return 404;/);
  assert.doesNotMatch(patched, /expires 1y/);
});

test("refuses unknown upstream static directives rather than silently removing them", () => {
  assert.throws(() => patchNginxCache(fixture.replace("expires 1y;", "expires 1y;\n      auth_basic protected;")), /unsupported assets/);
  assert.throws(() => patchNginxCache(fixture.replace("try_files $uri $uri/ /index.html;", "proxy_pass http:\/\/another-ui;")), /unsupported fallback/);
});

test("rejects ambiguous servers, conflicting locations and drift in previously patched output", () => {
  assert.throws(() => patchNginxCache(fixture + "\nserver {}\n"), /exactly one nginx server/);
  assert.throws(() => patchNginxCache(fixture.replace("listen 4454;", "listen 4454;\n    location = /index.html { return 200; }")), /requires manual review/);
  assert.throws(() => patchNginxCache(patchNginxCache(fixture).replace('"no-store"', '"public"')), /unsupported missing asset/);
});
