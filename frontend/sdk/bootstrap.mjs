import { IndigoTransport } from './indigo-transport.mjs';
import { workerUrl, macroUrl, microUrl, macroStyleUrl } from 'virtual:ketcher-assets';

let prepared;
let microTask;
let macroTask;
let macroAttempt = 0;
let styleTask;
let styleController;
let macroStyleTask;
let macroCodeTask;
let active;
const mark = name => performance.mark('ketcher:' + name);
const retired = () => new DOMException('Prepared structure service cancelled', 'AbortError');
function newTransport() {
  mark('worker-start');
  return new IndigoTransport(new Worker(workerUrl));
}
export function cancelPrepared() {
  if (!prepared || prepared.state !== 'prepared') return;
  prepared.state = 'disposed';
  clearTimeout(prepared.timer);
  prepared.transport.destroy(retired());
  prepared = undefined;
}
export function prepareInitial() {
  if (prepared || (active && !active.transport.disposed)) return;
  const lease = prepared = { state: 'prepared', transport: newTransport(), path: location.pathname };
  lease.timer = setTimeout(cancelPrepared, 15000);
  lease.transport.info().then(() => mark('info-ready'), () => { if (prepared === lease) cancelPrepared(); });
  void preload().catch(() => {});
}
export function takeTransport(session) {
  session.check();
  if (active && !active.transport.disposed) throw new Error('A visible editor already owns the structure service');
  let transport;
  if (prepared?.state === 'prepared' && !prepared.transport.disposed && prepared.path === location.pathname) {
    const lease = prepared;
    lease.state = 'claimed';
    clearTimeout(lease.timer);
    transport = lease.transport;
    prepared = undefined;
    mark('worker-claimed');
  } else {
    cancelPrepared();
    transport = newTransport();
  }
  active = { session, transport };
  return transport;
}
function stylesheet(url, signal) {
  return new Promise((resolve, reject) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet'; link.href = url; link.dataset.ketcherRuntime = '';
    const done = error => {
      link.onload = link.onerror = null;
      signal?.removeEventListener('abort', cancel);
      if (error) { link.remove(); reject(error); } else resolve();
    };
    const cancel = () => done(new DOMException('Stylesheet load cancelled', 'AbortError'));
    link.onload = () => done();
    link.onerror = () => done(new Error('Ketcher stylesheet failed'));
    if (signal?.aborted) { cancel(); return; }
    signal?.addEventListener('abort', cancel, { once: true });
    document.head.append(link);
  });
}
function loadStyle() {
  if (styleTask) return styleTask;
  const controller = styleController = new AbortController();
  styleTask = stylesheet(new URL('./ketcher.css', import.meta.url).href, controller.signal)
    .catch(error => { styleTask = undefined; throw error; })
    .finally(() => { if (styleController === controller) styleController = undefined; });
  return styleTask;
}
export function cancelPreload() {
  cancelPrepared();
  styleController?.abort();
}
export function preload() {
  microTask ||= import(/* @vite-ignore */ microUrl).catch(error => { microTask = undefined; throw error; });
  return Promise.all([microTask, loadStyle()]).then(([module]) => module);
}
function trackedResource(task, cancel) {
  const resource = { state: 'pending', cancel };
  resource.promise = task.then(value => { resource.state = 'ready'; return value; }, error => {
    resource.state = 'failed'; throw error;
  });
  return resource;
}
export function loadMacro() {
  if (!macroTask) {
    const url = new URL(macroUrl);
    if (macroAttempt) url.searchParams.set('attempt', String(macroAttempt));
    const style = new URL(macroStyleUrl);
    if (macroAttempt) style.searchParams.set('attempt', String(macroAttempt));
    macroCodeTask ||= trackedResource(import(/* @vite-ignore */ url.href));
    if (!macroStyleTask) {
      const controller = new AbortController();
      macroStyleTask = trackedResource(stylesheet(style.href, controller.signal), () => controller.abort());
    }
    const code = macroCodeTask, css = macroStyleTask;
    let timer;
    // Leave time for the actual editor/library initialization inside the
    // controller's 15s deadline. A hung import must not poison later retries.
    const deadline = new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error('Macro resources timed out')), 12000);
    });
    const pending = macroTask = Promise.race([
      Promise.all([code.promise, css.promise]).then(([module]) => module), deadline
    ]).finally(() => clearTimeout(timer)).catch(error => {
      if (macroTask === pending) {
        ++macroAttempt;
        macroTask = undefined;
        // Late completion only updates these captured records. It must never
        // clear the code/style records of a newer initialization attempt.
        if (macroCodeTask === code && code.state !== 'ready') macroCodeTask = undefined;
        if (macroStyleTask === css && css.state !== 'ready') { macroStyleTask = undefined; css.cancel(); }
      }
      throw error;
    });
  }
  return macroTask;
}
// These events are emitted by the existing navigation transaction and HMR bridge.
window.addEventListener('nexpoly:structure-navigation', cancelPrepared);
window.addEventListener('pagehide', cancelPrepared);
