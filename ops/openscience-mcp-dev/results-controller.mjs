/** Inline display of original read-only tool results in the development UI. */
import { extractFilterTurns, renderFilterResult } from "./results-view.mjs"

const API_PREFIX = "/openscience-api/"
const SESSION_ID = /^ses_[A-Za-z0-9_-]+$/

export function sessionFromPath(pathname) {
  const match = /^\/chat\/(ses_[A-Za-z0-9_-]+)\/?$/.exec(pathname)
  return match?.[1] ?? null
}

export function managedDirectory(value) {
  if (typeof value !== "string" || value.length > 2048 || /[\u0000-\u001f]/u.test(value)) return null
  if (value.split("/").some(segment => segment === "." || segment === "..")) return null
  if (value === "/openscience/files/general" || value === "/openscience/files/projects"
      || value.startsWith("/openscience/files/projects/")) return value
  return null
}

export function sessionRequest(input, init, origin) {
  try {
    const address = new URL(typeof input === "string" || input instanceof URL ? input : input.url, origin)
    if (address.origin !== origin || !address.pathname.startsWith(API_PREFIX)) return null
    const headers = new Headers(init?.headers ?? (typeof input === "object" ? input.headers : undefined))
    const match = /^\/openscience-api\/session\/(ses_[A-Za-z0-9_-]+)(?:\/|$)/.exec(address.pathname)
    const method = String(init?.method ?? input?.method ?? "GET").toUpperCase()
    const directory = address.searchParams.has("directory")
      ? address.searchParams.get("directory") : headers.get("x-openscience-directory")
    return { sessionId: match?.[1] ?? null, directory: managedDirectory(directory),
      messages: method === "GET" && Boolean(match) && address.pathname === `${API_PREFIX}session/${match[1]}/message` }
  } catch { return null }
}

function originalAnswerIsJson(messages, turnId) {
  const answer = [...messages].reverse().find(message => message.info?.role === "assistant"
    && message.info.parentID === turnId && message.info.time?.completed
    && message.parts?.some(part => part.type === "text" && part.text?.trim()))
  const text = answer?.parts?.filter(part => part.type === "text").map(part => part.text).join("\n").trim()
  if (!text) return false
  const fenced = /^```(?:json)?\s*\n([\s\S]*?)\n```$/i.exec(text)
  try {
    const value = JSON.parse(fenced ? fenced[1] : text)
    return value !== null && typeof value === "object"
  } catch { return false }
}

export function startResultsUi(win = window, doc = document) {
  if (doc.documentElement.dataset.nexpolyResultsUi) return
  doc.documentElement.dataset.nexpolyResultsUi = "1"
  const nativeFetch = win.fetch.bind(win)
  const directories = new Map()
  const records = new Map()
  const nativePending = new Map()
  const latestIssued = new Map()
  let sequence = 0
  let currentSession = null
  let currentContext = null
  let inFlight = null
  let mountTimer = null
  let fetchTimer = null

  const contextKey = (sessionId, directory) => `${directory}\0${sessionId}`

  function remember(sessionId, directory, messages, issued) {
    const key = contextKey(sessionId, directory)
    if (!SESSION_ID.test(sessionId) || !managedDirectory(directory) || !Array.isArray(messages)
        || directories.get(sessionId) !== directory || latestIssued.get(key) !== issued) return
    try {
      records.delete(key)
      records.set(key, { messages, models: extractFilterTurns(messages), updatedAt: Date.now() })
      while (records.size > 5) records.delete(records.keys().next().value)
      scheduleMount()
    } catch { /* An unavailable panel must not break the normal conversation. */ }
  }

  // Observe only same-origin API request context. Keep the native arguments,
  // response and Promise unchanged; no tool calls or prompt requests originate here.
  win.fetch = function(input, init) {
    const request = sessionRequest(input, init, win.location.origin)
    if (request?.sessionId && request.directory) {
      directories.set(request.sessionId, request.directory)
      scheduleMount()
    }
    const pending = nativeFetch(input, init)
    if (request?.messages && request.sessionId && request.directory) {
      const key = contextKey(request.sessionId, request.directory)
      const issued = ++sequence
      latestIssued.set(key, issued)
      nativePending.set(key, { startedAt: Date.now(), pending })
      pending.then(async response => {
        if (response.ok && response.headers.get("content-type")?.includes("application/json")) {
          remember(request.sessionId, request.directory, await response.clone().json(), issued)
        }
      }).catch(() => {}).finally(() => {
        if (nativePending.get(key)?.pending === pending) nativePending.delete(key)
      })
    }
    return pending
  }

  function removePanels() {
    for (const node of doc.querySelectorAll("[data-nexpoly-results-host]")) node.remove()
    for (const node of doc.querySelectorAll(".nexpoly-original-answer-hidden")) {
      node.classList.remove("nexpoly-original-answer-hidden")
    }
  }

  function mount() {
    mountTimer = null
    const sessionId = sessionFromPath(win.location.pathname)
    const directory = sessionId && directories.get(sessionId)
    const key = sessionId && directory ? contextKey(sessionId, directory) : null
    if (sessionId !== currentSession || key !== currentContext) {
      inFlight?.controller.abort()
      inFlight = null
      currentSession = sessionId
      currentContext = key
      removePanels()
      scheduleFetch(100)
    }
    if (!key) return
    const record = records.get(key)
    if (!record) { scheduleFetch(100); return }
    const turns = new Map()
    for (const node of doc.querySelectorAll("[data-message-id]")) {
      const turnId = node.getAttribute("data-message-id")
      const content = node.querySelector('[data-component="session-turn"] [data-slot="session-turn-content"]')
      if (turnId && content && !turns.has(turnId)) turns.set(turnId, content)
    }
    const byTurn = new Map()
    for (const model of record.models) {
      const models = byTurn.get(model.turnId) ?? []
      models.push(model)
      byTurn.set(model.turnId, models)
    }
    for (const [turnId, content] of turns) {
      const models = byTurn.get(turnId)
      let host = [...content.children].find(node => node.hasAttribute("data-nexpoly-results-host"))
      if (!models?.length) {
        content.querySelector('[data-slot="session-turn-response"]')?.classList.remove("nexpoly-original-answer-hidden")
        host?.remove()
        continue
      }
      if (!host) {
        host = doc.createElement("div")
        host.setAttribute("data-nexpoly-results-host", "")
        host.dataset.turnId = turnId
        host.dataset.sessionId = sessionId
        host.dataset.directory = directory
        let anchor = content.querySelector('[data-slot="session-turn-response"]')
        while (anchor && anchor.parentElement !== content) anchor = anchor.parentElement
        content.insertBefore(host, anchor)
      }
      const signature = JSON.stringify(models)
      if (host._nexpolySignature !== signature) {
        const fragments = models.map(model => renderFilterResult(doc, model))
        host.replaceChildren(...fragments)
        host._nexpolySignature = signature
      }
      const response = content.querySelector('[data-slot="session-turn-response"]')
      const hideJson = models.some(model => model.search || model.histograms?.length)
        && originalAnswerIsJson(record.messages, turnId)
      if (response) response.classList.toggle("nexpoly-original-answer-hidden", hideJson && host.dataset.originalExpanded !== "true")
      let toggle = host.querySelector("[data-nexpoly-original-toggle]")
      if (hideJson && response) {
        if (!toggle) {
          toggle = doc.createElement("button")
          toggle.type = "button"
          toggle.className = "nexpoly-original-answer-toggle"
          toggle.setAttribute("data-nexpoly-original-toggle", "")
          toggle.addEventListener("click", () => {
            host.dataset.originalExpanded = host.dataset.originalExpanded === "true" ? "false" : "true"
            scheduleMount()
          })
          host.append(toggle)
        }
        const expanded = host.dataset.originalExpanded === "true"
        toggle.setAttribute("aria-expanded", String(expanded))
        toggle.textContent = expanded ? "收起原始回答" : "查看原始回答"
      } else toggle?.remove()
    }
    const known = new Set(record.messages.map(message => message.info?.id))
    if ([...turns.keys()].some(id => !known.has(id))) scheduleFetch(100)
  }

  function scheduleMount() {
    if (mountTimer === null) mountTimer = win.setTimeout(mount, 80)
  }

  function scheduleFetch(delay = 1000) {
    if (fetchTimer === null) fetchTimer = win.setTimeout(refresh, delay)
  }

  async function refresh() {
    fetchTimer = null
    const sessionId = sessionFromPath(win.location.pathname)
    const directory = sessionId && directories.get(sessionId)
    const key = sessionId && directory ? contextKey(sessionId, directory) : null
    const observed = nativePending.get(key)
    if (!sessionId || !directory || doc.visibilityState === "hidden" || inFlight
        || (observed && Date.now() - observed.startedAt < 10000)) { scheduleFetch(2000); return }
    const record = records.get(key)
    if (record && Date.now() - record.updatedAt < 1200) { scheduleFetch(1500); return }
    const controller = new AbortController()
    const issued = ++sequence
    latestIssued.set(key, issued)
    inFlight = { controller, key }
    const timeout = win.setTimeout(() => controller.abort(), 8000)
    try {
      const address = new URL(`${API_PREFIX}session/${encodeURIComponent(sessionId)}/message`, win.location.origin)
      address.searchParams.set("limit", "400")
      const response = await nativeFetch(address.href, { method: "GET", credentials: "same-origin",
        headers: { "x-openscience-directory": directory }, signal: controller.signal })
      if (response.ok && key === currentContext && sessionId === sessionFromPath(win.location.pathname)
          && directories.get(sessionId) === directory) remember(sessionId, directory, await response.json(), issued)
    } catch { /* Preserve current results during a transient read failure. */ }
    finally {
      win.clearTimeout(timeout)
      if (inFlight?.controller === controller) inFlight = null
      scheduleFetch(4000)
    }
  }

  const observer = new MutationObserver(changes => {
    if (changes.some(change => !change.target.closest?.("[data-nexpoly-results-host]"))) scheduleMount()
  })
  const observe = () => {
    observer.observe(doc.body, { childList: true, subtree: true })
    scheduleMount()
    scheduleFetch()
  }
  if (doc.body) observe()
  else doc.addEventListener("DOMContentLoaded", observe, { once: true })
  win.addEventListener("popstate", scheduleMount)
  doc.addEventListener("visibilitychange", () => { scheduleMount(); scheduleFetch(100) })
}

if (typeof window !== "undefined" && typeof document !== "undefined") startResultsUi()
