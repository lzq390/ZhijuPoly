import assert from "node:assert/strict";
import test from "node:test";
import { handoffPageReady } from "../overlay/scripts/handoff-readiness.mjs";
import { encodeMockEvent, mockEventStream } from "../overlay/src/mocks/handoffState.ts";

const page = (fonts, panels) => ({ fonts: { status: fonts }, querySelectorAll: () => panels.map(visible => ({ checkVisibility: () => visible })) });
test("loaded page without an auth gate is ready", () => assert.equal(handoffPageReady(page("loaded", [])), true));
test("retained hidden membership notices do not block readiness", () => assert.equal(handoffPageReady(page("loaded", [false])), true));
test("a visible auth/loading/error gate blocks readiness", () => {
  assert.equal(handoffPageReady(page("loaded", [true])), false);
  assert.equal(handoffPageReady(page("loaded", [false, true])), false);
});
test("font loading still blocks a page without visible auth gates", () => {
  assert.equal(handoffPageReady(page("loading", [])), false);
  assert.equal(handoffPageReady(page("loading", [false])), false);
});
test("stream encoding preserves the explicit event and generic data", () => {
  assert.equal(new TextDecoder().decode(encodeMockEvent({ event: "unit-record", data: { sequence: 1 } })), 'event: unit-record\ndata: {"sequence":1}\n\n');
});
test("finite stream preserves order and terminates", async () => {
  const events = [{ event: "unit-record", data: { sequence: 1 } }, { event: "done", data: {} }];
  assert.equal(await mockEventStream(events).text(), events.map(e => new TextDecoder().decode(encodeMockEvent(e))).join(""));
});
test("an already aborted signal does not subscribe", async () => {
  const controller = new AbortController(); controller.abort(); let subscribed = false;
  const response = mockEventStream([], controller.signal, () => { subscribed = true; return () => {}; });
  await assert.rejects(response.text(), { name: "AbortError" }); assert.equal(subscribed, false);
});
test("subscribed streams preserve initial/live updates and abort cleanup", async () => {
  const controller = new AbortController(); let push; let removed = 0;
  const initial = { event: "unit-record", data: { sequence: 1 } };
  const response = mockEventStream([initial], controller.signal, listener => { push = listener; return () => { removed++; }; });
  const reader = response.body.getReader(); assert.deepEqual((await reader.read()).value, encodeMockEvent(initial));
  const update = { event: "unit-record", data: { sequence: 2 } }; push(update);
  assert.deepEqual((await reader.read()).value, encodeMockEvent(update)); controller.abort();
  await assert.rejects(reader.read(), { name: "AbortError" }); assert.equal(removed, 1);
});
test("reader cancellation cleans up once and ignores late updates", async () => {
  let push; let removed = 0;
  const response = mockEventStream([], null, listener => { push = listener; return () => { removed++; }; });
  const reader = response.body.getReader(); await reader.cancel();
  assert.doesNotThrow(() => push({ event: "unit-record", data: { sequence: 3 } })); assert.equal(removed, 1);
});
test("synchronous abort while subscribing releases the returned subscription", async () => {
  const controller = new AbortController(); let removed = 0;
  const response = mockEventStream([], controller.signal, () => { controller.abort(); return () => { removed++; }; });
  await assert.rejects(response.text(), { name: "AbortError" }); assert.equal(removed, 1);
});
test("a throwing subscription releases the abort listener and preserves the error", async () => {
  const controller = new AbortController(); let removed = 0;
  const remove = controller.signal.removeEventListener.bind(controller.signal);
  controller.signal.removeEventListener = (...args) => { removed++; return remove(...args); };
  const expected = new Error("unit-only subscription failure");
  const response = mockEventStream([], controller.signal, () => { throw expected; });
  await assert.rejects(response.text(), error => error === expected); assert.equal(removed, 1);
});
test("event streams retain cache and content-type boundaries", async () => {
  const response = mockEventStream([]); assert.equal(response.headers.get("Content-Type"), "text/event-stream");
  assert.equal(response.headers.get("Cache-Control"), "no-store"); assert.equal(await response.text(), "");
});
