import React, { useState } from "react";
import assert from "assert";
import { callbackify } from "util";
import { createRoot } from "react-dom/client";
import { Editor } from "ketcher-react";
import { StandaloneStructServiceProvider } from "ketcher-standalone";
import "ketcher-react/dist/index.css";

Object.assign(window, { __retiredEditors: [] as WeakRef<object>[], __compatErrors: [] as string[], __compatInitCount: 0 });
function retireProbe() {
  const globals = window as unknown as { __compatEditor?: object; __retiredEditors: WeakRef<object>[] };
  if (globals.__compatEditor) globals.__retiredEditors.push(new WeakRef(globals.__compatEditor));
  Object.assign(window, { __compatEditor: null });
}
Object.assign(window, { __compatPolyfills: async () => {
  let assertion = false;
  try { assert.strictEqual(1, 2); } catch (error) { assertion = error instanceof assert.AssertionError; }
  const value = await new Promise((resolve, reject) => callbackify(async () => "util-nextTick")((error, result) => error ? reject(error) : resolve(result)));
  return { assertion, value, hasGlobalProcess: Object.hasOwn(window, "process") };
} });
const provider = new StandaloneStructServiceProvider();
const createService = provider.createStructService.bind(provider);
provider.createStructService = options => {
  const controls = window as unknown as { __compatFailNext?: boolean; __compatUnmountOnCreate?: boolean; __compatToggle: () => void };
  if (controls.__compatFailNext) { controls.__compatFailNext = false; throw new Error("Expected initialization failure"); }
  if (controls.__compatUnmountOnCreate) { controls.__compatUnmountOnCreate = false; queueMicrotask(() => controls.__compatToggle()); }
  const service = createService(options);
  Object.assign(window, { __compatService: service });
  return service;
};
function Proof() {
  const [epoch, setEpoch] = useState(0);
  const [visible, setVisible] = useState(true);
  Object.assign(window, { __compatRemount: () => { retireProbe(); setEpoch(n => n + 1); }, __compatToggle: () => { retireProbe(); setVisible(v => !v); } });
  return <><input aria-label="Host input" style={{ height: 30 }} /><div data-structure-editor data-editor-engine="react" data-ketcher-session="proof" style={{ width: "100vw", height: "calc(100vh - 40px)" }}>{visible &&
    <Editor key={epoch} staticResourcesUrl="/ketcher-assets/3.8.0" structServiceProvider={provider}
      onInit={(ketcher) => { const w = window as unknown as { __compatInitCount: number }; ++w.__compatInitCount; Object.assign(window, { __compatEditor: ketcher, ketcher }); }}
      errorHandler={(error) => { (window as unknown as { __compatErrors: string[] }).__compatErrors.push(String(error)); }} />
  }</div></>;
}
createRoot(document.getElementById("root")!).render(<React.StrictMode><Proof /></React.StrictMode>);
