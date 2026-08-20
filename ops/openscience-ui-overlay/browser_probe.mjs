#!/usr/bin/env node

import assert from "node:assert/strict"
import http from "node:http"

import { chromium } from "playwright"

const PUBLIC_HOST = "114.214.255.154"
const CHILD_ORIGIN = `http://${PUBLIC_HOST}:9011`
const TRUSTED_PARENT_ORIGINS = [
  `http://${PUBLIC_HOST}:9000`,
  `http://${PUBLIC_HOST}:9001`,
]
const REJECTED_PARENT_ORIGIN = `http://${PUBLIC_HOST}:9002`
const NAMESPACE = "openscience.zhijupoly"
const VERSION = 1
const PROXY_PORT = 9080
const REVIEWED_PORTS = new Set(["9000", "9001", "9002", "9011"])
const PROXY_REQUESTS = []
const BASE_ENTRY_PATH = "/assets/index-B2eNxQLj.js"
const PATCHED_ENTRY_PATH = "/assets/index-nexpoly-3e4638285546.js"
const PATCHED_ENTRY_URL = `${CHILD_ORIGIN}${PATCHED_ENTRY_PATH}`

const PROJECTS_REQUEST = { namespace: NAMESPACE, version: VERSION, type: "projects.request" }
const SESSIONS_REQUEST = {
  namespace: NAMESPACE,
  version: VERSION,
  type: "general.sessions.request",
}

function parentDocument({ noReferrer = false } = {}) {
  const referrerPolicy = noReferrer ? ' referrerpolicy="no-referrer"' : ""
  return `<!doctype html>
<html>
  <body>
    <iframe id="workspace" src="${CHILD_ORIGIN}/"${referrerPolicy}></iframe>
    <script>
      const childOrigin = ${JSON.stringify(CHILD_ORIGIN)};
      const frame = document.getElementById("workspace");
      window.probe = {
        loaded: false,
        messages: [],
        sourceMessages: [],
        send(payload) {
          frame.contentWindow.postMessage(payload, childOrigin);
        },
        sendFromSibling(payload) {
          const sender = document.createElement("iframe");
          sender.src = "about:blank";
          sender.addEventListener("load", () => {
            const execute = sender.contentWindow.Function(
              "payload",
              "targetOrigin",
              'window.addEventListener("message", event => {' +
                'parent.probe.sourceMessages.push({ origin: event.origin, data: event.data });' +
              '});' +
              'parent.document.getElementById("workspace").contentWindow.postMessage(payload, targetOrigin);'
            );
            execute(payload, childOrigin);
          }, { once: true });
          document.body.appendChild(sender);
        }
      };
      frame.addEventListener("load", () => { window.probe.loaded = true; });
      window.addEventListener("message", event => {
        if (event.source === frame.contentWindow) {
          window.probe.messages.push({ origin: event.origin, data: event.data });
        }
      });
    </script>
  </body>
</html>`
}

function startParentServer(port) {
  const server = http.createServer((request, response) => {
    const url = new URL(request.url ?? "/", `http://${request.headers.host}`)
    const document = parentDocument({ noReferrer: url.pathname === "/no-referrer" })
    response.writeHead(200, {
      "cache-control": "no-store",
      "content-type": "text/html; charset=utf-8",
    })
    response.end(document)
  })
  return listen(server, port)
}

function startChildProxy() {
  const server = http.createServer((request, response) => {
    const upstream = http.request(
      {
        hostname: "127.0.0.1",
        port: 4454,
        method: request.method,
        path: request.url,
        headers: { ...request.headers, host: `${PUBLIC_HOST}:9011` },
      },
      (upstreamResponse) => {
        response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers)
        upstreamResponse.pipe(response)
      },
    )
    upstream.on("error", (error) => {
      response.writeHead(502, { "content-type": "text/plain; charset=utf-8" })
      response.end(`upstream error: ${error.message}`)
    })
    request.pipe(upstream)
  })
  return listen(server, 9011)
}

function startBrowserProxy() {
  const server = http.createServer((request, response) => {
    PROXY_REQUESTS.push(request.url ?? "")
    let target
    try {
      target = new URL(request.url ?? "")
    } catch {
      response.writeHead(400)
      response.end()
      return
    }
    if (
      target.protocol !== "http:" ||
      target.hostname !== PUBLIC_HOST ||
      !REVIEWED_PORTS.has(target.port)
    ) {
      response.writeHead(403)
      response.end()
      return
    }
    const upstream = http.request(
      {
        hostname: "127.0.0.1",
        port: Number(target.port),
        method: request.method,
        path: `${target.pathname}${target.search}`,
        headers: { ...request.headers, host: target.host },
      },
      (upstreamResponse) => {
        response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers)
        upstreamResponse.pipe(response)
      },
    )
    upstream.on("error", (error) => {
      response.writeHead(502, { "content-type": "text/plain; charset=utf-8" })
      response.end(`proxy error: ${error.message}`)
    })
    request.pipe(upstream)
  })
  server.on("connect", (_request, socket) => socket.destroy())
  return listen(server, PROXY_PORT)
}

function listen(server, port) {
  return new Promise((resolve, reject) => {
    server.once("error", reject)
    server.listen(port, "0.0.0.0", () => resolve(server))
  })
}

async function openParent(browser, origin, path = "/") {
  const context = await browser.newContext()
  const page = await context.newPage()
  const diagnostics = []
  page.on("console", (message) => {
    if (message.type() === "error" || message.type() === "warning") {
      diagnostics.push(`console.${message.type()}: ${message.text()}`)
    }
  })
  page.on("pageerror", (error) => diagnostics.push(`pageerror: ${error.message}`))
  page.on("requestfailed", (request) => {
    diagnostics.push(`requestfailed: ${request.url()} ${request.failure()?.errorText ?? "unknown"}`)
  })
  await page.goto(`${origin}${path}`, { waitUntil: "domcontentloaded" })
  await page.waitForFunction(() => window.probe?.loaded === true)
  await page.waitForTimeout(750)
  assert.ok(PROXY_REQUESTS.includes(`${origin}${path}`), `${origin}${path} bypassed the local proxy`)
  assert.ok(PROXY_REQUESTS.includes(`${CHILD_ORIGIN}/`), `${CHILD_ORIGIN}/ bypassed the local proxy`)
  const childFrame = page.frames().find((frame) => frame.url().startsWith(`${CHILD_ORIGIN}/`))
  assert.ok(childFrame, `${origin}${path} did not load the OpenScience child frame`)
  const childEntry = await childFrame.evaluate(() => ({
    importMaps: Array.from(document.querySelectorAll('script[type="importmap"]')).map(
      (element) => element.textContent ?? "",
    ),
    moduleSources: Array.from(document.querySelectorAll('script[type="module"][src]')).map(
      (element) => element.src,
    ),
  }))
  assert.deepEqual(childEntry.moduleSources, [PATCHED_ENTRY_URL])
  assert.equal(childEntry.importMaps.length, 1)
  assert.deepEqual(JSON.parse(childEntry.importMaps[0]), {
    imports: { [BASE_ENTRY_PATH]: PATCHED_ENTRY_PATH },
  })
  await page.evaluate(() => {
    window.probe.messages = []
    window.probe.sourceMessages = []
  })
  return { context, diagnostics, page }
}

async function expectSnapshots(browser, origin) {
  const { context, diagnostics, page } = await openParent(browser, origin)
  try {
    await page.evaluate(
      ([projects, sessions]) => {
        const requestSnapshots = () => {
          window.probe.send(projects)
          window.probe.send(sessions)
        }
        requestSnapshots()
        window.probe.requestTimer = window.setInterval(requestSnapshots, 1_000)
      },
      [PROJECTS_REQUEST, SESSIONS_REQUEST],
    )
    try {
      await page.waitForFunction(
        () => {
          const types = new Set(window.probe.messages.map((message) => message.data?.type))
          return types.has("projects.snapshot") && types.has("general.sessions.snapshot")
        },
        undefined,
        { timeout: 45_000 },
      )
    } catch (error) {
      const observedTypes = await page.evaluate(() =>
        window.probe.messages.map((message) => message.data?.type ?? "unknown"),
      )
      const childFrame = page
        .frames()
        .find((frame) => frame.url().startsWith(`${CHILD_ORIGIN}/`))
      const childState = childFrame
        ? await childFrame.evaluate(() => ({
            bodyText: document.body?.innerText.slice(0, 500) ?? "",
            embedded: window.parent !== window,
            readyState: document.readyState,
            referrer: document.referrer,
            url: window.location.href,
          }))
        : { missing: true }
      throw new Error(
        `${origin} did not return both bridge snapshots; ` +
          `observed=${JSON.stringify(observedTypes)}; ` +
          `child=${JSON.stringify(childState)}; ` +
          `diagnostics=${JSON.stringify(diagnostics.slice(-20))}; ` +
          `proxyRequests=${JSON.stringify(PROXY_REQUESTS.slice(-30))}`,
        { cause: error },
      )
    } finally {
      await page.evaluate(() => window.clearInterval(window.probe.requestTimer))
    }
    const messages = await page.evaluate(() => window.probe.messages)
    for (const type of ["projects.snapshot", "general.sessions.snapshot"]) {
      const message = messages.find((candidate) => candidate.data?.type === type)
      assert.ok(message, `${origin} did not receive ${type}`)
      assert.equal(message.origin, CHILD_ORIGIN)
      assert.equal(message.data.namespace, NAMESPACE)
      assert.equal(message.data.version, VERSION)
    }
  } finally {
    await context.close()
  }
}

async function expectNoSnapshot(browser, origin, options = {}) {
  const { context, page } = await openParent(browser, origin, options.path)
  try {
    const requests = options.requests ?? [PROJECTS_REQUEST, SESSIONS_REQUEST]
    await page.evaluate((payloads) => {
      for (const payload of payloads) window.probe.send(payload)
    }, requests)
    await page.waitForTimeout(1_500)
    const messages = await page.evaluate(() => window.probe.messages)
    assert.equal(
      messages.some((message) =>
        ["projects.snapshot", "general.sessions.snapshot"].includes(message.data?.type),
      ),
      false,
      `${origin}${options.path ?? "/"} unexpectedly received a bridge snapshot`,
    )
  } finally {
    await context.close()
  }
}

async function expectWrongSourceRejected(browser) {
  const { context, page } = await openParent(browser, TRUSTED_PARENT_ORIGINS[0])
  try {
    await page.evaluate((request) => window.probe.sendFromSibling(request), PROJECTS_REQUEST)
    await page.waitForTimeout(1_500)
    const messages = await page.evaluate(() => window.probe.sourceMessages)
    assert.equal(
      messages.some((message) => message.data?.type === "projects.snapshot"),
      false,
      "a sibling-window request unexpectedly received a project snapshot",
    )
  } finally {
    await context.close()
  }
}

async function main() {
  const servers = []
  let browser
  try {
    servers.push(await startChildProxy())
    for (const port of [9000, 9001, 9002]) servers.push(await startParentServer(port))
    servers.push(await startBrowserProxy())
    browser = await chromium.launch({
      headless: true,
      proxy: { server: `http://127.0.0.1:${PROXY_PORT}` },
      args: ["--no-sandbox", "--proxy-bypass-list=<-loopback>"],
    })
    for (const origin of TRUSTED_PARENT_ORIGINS) await expectSnapshots(browser, origin)
    await expectNoSnapshot(browser, REJECTED_PARENT_ORIGIN)
    await expectNoSnapshot(browser, TRUSTED_PARENT_ORIGINS[0], { path: "/no-referrer" })
    await expectNoSnapshot(browser, TRUSTED_PARENT_ORIGINS[0], {
      requests: [
        { ...PROJECTS_REQUEST, namespace: "other.namespace" },
        { ...SESSIONS_REQUEST, version: 2 },
      ],
    })
    await expectWrongSourceRejected(browser)
  } finally {
    if (browser) await browser.close()
    await Promise.all(
      servers.map(
        (server) =>
          new Promise((resolve) => {
            server.close(resolve)
            server.closeAllConnections()
          }),
      ),
    )
  }
}

main().then(
  () => {
    process.stdout.write("OpenScience browser bridge policy verified\n")
    process.exit(0)
  },
  (error) => {
    process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`)
    process.exit(1)
  },
)
