// Exercise the actual patched published methods without starting Indigo/WASM.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { EventEmitter } from 'node:events';
const require = createRequire(import.meta.url);
const Command = Object.fromEntries(['Info','Convert','Layout','Clean','Aromatize','Dearomatize','CalculateCip','Automap','Check','Calculate','GenerateImageAsBase64','GetInChIKey','ExplicitHydrogens','CalculateMacromoleculeProperties'].map((name, index) => [name, index]));
const ChemicalMimeType = Object.fromEntries(['Mol','Rxn','DaylightSmiles','ExtendedSmiles','DaylightSmarts','InChI','InChIAuxInfo','InChIKey','CML','KET','CDXML','CDX','SDF','FASTA','SEQUENCE','PeptideSequenceThreeLetter','IDT','HELM','RDF'].map(name => [name, name]));
function loadService(entry) {
  const source = readFileSync(new URL(`../node_modules/ketcher-standalone/${entry}`, import.meta.url), 'utf8');
  const workers = [], timers = new Map();
  class Worker {
    messages = [];
    terminated = false;
    constructor() { workers.push(this); }
    postMessage(message) { assert.equal(this.terminated, false); this.messages.push(message); }
    terminate() { this.terminated = true; }
    reply(payload, hasError = false) { const request = this.messages.at(-1); this.onmessage?.({ data: { type: request.type, inputData: request.data?.struct, hasError, payload, error: hasError ? payload : undefined } }); }
  }
  const context = {
    WorkerFactory: Worker, Command, ChemicalMimeType, SupportedFormat: { Ket: 'ket', Mol: 'mol', Smiles: 'smiles' },
    WorkerEvent: {}, EventEmitter, DOMException, Error,
    CoreEditor: { provideEditorInstance: () => undefined },
    ketcherCore: { ChemicalMimeType, CoreEditor: { provideEditorInstance: () => undefined }, pickStandardServerOptions: (_id, options) => options, getLabelRenderModeForIndigo: () => 'all' },
    pickStandardServerOptions: (_id, options) => options, getLabelRenderModeForIndigo: () => 'all',
    _classCallCheck: () => {}, _defineProperty: (object, key, value) => (object[key] = value, object),
    _createClass: (Class, descriptors) => descriptors.forEach(({ key, ...descriptor }) => Object.defineProperty(Class.prototype, key, { ...descriptor, configurable: true })),
    _slicedToArray: value => value, _objectWithoutProperties: (object, keys) => Object.fromEntries(Object.entries(object).filter(([key]) => !keys.includes(key))),
    _asyncToGenerator: require('@babel/runtime/helpers/asyncToGenerator'), _regeneratorRuntime: require('@babel/runtime/regenerator'),
    setTimeout: (callback, ms) => { const token = {}; timers.set(token, { callback, ms }); return token; }, clearTimeout: token => timers.delete(token),
  };
  for (const name of ['_classCallCheck','_defineProperty','_createClass','_slicedToArray','_objectWithoutProperties','_asyncToGenerator','_regeneratorRuntime']) context[`${name}__default`] = { default: context[name] };
  context.events = { EventEmitter };
  const code = source.slice(source.indexOf('var _excluded ='), source.indexOf('var StandaloneStructServiceProvider ='));
  vm.createContext(context); vm.runInContext(code, context);
  return { Service: context.IndigoService, workers, timers };
}
for (const entry of ['dist/main.js', 'dist/cjs/main.js']) {
  test(`${entry}: same input, different formats are strictly serialized`, async () => {
    const { Service, workers, timers } = loadService(entry); const service = new Service({});
    const smiles = service.convert({ struct: 'CCO', output_format: 'DaylightSmiles' });
    const ket = service.convert({ struct: 'CCO', output_format: 'KET' });
    assert.equal(workers[0].messages.length, 1);
    workers[0].reply('CCO'); assert.equal((await smiles).struct, 'CCO');
    assert.equal(workers[0].messages.length, 2);
    workers[0].reply('{"root":{"nodes":[]}}'); assert.equal((await ket).format, 'KET');
    assert.equal(timers.size, 0); service.destroy();
  });
  test(`${entry}: chemical failure advances queue; destroy settles every old request`, async () => {
    const { Service, workers } = loadService(entry); const old = new Service({}); const next = new Service({});
    assert.notEqual(workers[0], workers[1]);
    const promises = [old.convert({ struct: 'bad', output_format: 'KET' }), old.info(), old.getInChIKey('CCO')];
    const ended = Promise.allSettled(promises);
    const chemicalError = { message: 'Cannot parse structure' }; workers[0].reply(chemicalError, true);
    assert.equal(workers[0].messages.length, 2); assert.equal(workers[0].terminated, false);
    const late = workers[0].onmessage; old.destroy(); old.destroy();
    const fresh = next.info(); late({ data: { type: Command.Info, hasError: false, payload: 'OLD' } });
    workers[1].reply('NEW'); assert.equal((await fresh).indigoVersion, 'NEW');
    const results = await ended; assert.equal(results[0].reason, chemicalError); assert(results.every(result => result.status === 'rejected'));
    next.destroy();
  });
  for (const fault of ['timeout','error','messageerror','bad-response']) test(`${entry}: ${fault} retires whole service`, async () => {
    const { Service, workers, timers } = loadService(entry); const service = new Service({});
    const ended = Promise.allSettled([service.info(), service.info()]);
    if (fault === 'timeout') { const [timer] = timers.values(); assert.equal(timer.ms, 15000); timer.callback(); }
    if (fault === 'error') workers[0].onerror({ preventDefault() {}, message: 'worker died' });
    if (fault === 'messageerror') workers[0].onmessageerror();
    if (fault === 'bad-response') workers[0].onmessage({ data: { type: Command.Convert } });
    assert((await ended).every(result => result.status === 'rejected'));
    assert.equal(workers[0].terminated, true); assert.equal(timers.size, 0);
    await assert.rejects(service.info()); assert.equal(workers[0].messages.length, 1);
  });
}
