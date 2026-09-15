const aborted = () => new DOMException('Editor initialization disposed', 'AbortError');
const macroTypes = new Set(['monomer', 'ambiguousMonomer', 'monomerTemplate',
  'ambiguousMonomerTemplate', 'monomerGroupTemplate']);
const macroFormats = new Map([
  ['helm', 'helm'], ['chemical/x-helm', 'helm'], ['fasta', 'fasta'], ['chemical/x-fasta', 'fasta'],
  ['sequence', 'sequence'], ['chemical/x-sequence', 'sequence'],
  ['sequence3Letter', 'sequence-3-letter'], ['sequence-3-letter', 'sequence-3-letter'],
  ['chemical/x-peptide-sequence-3-letter', 'sequence-3-letter'],
  ['idt', 'idt'], ['chemical/x-idt', 'idt'],
  ['chemical/x-rna-sequence', 'sequence'], ['chemical/x-dna-sequence', 'sequence'],
  ['chemical/x-peptide-sequence', 'sequence']
]);

export function containsMacro(source) {
  if (typeof source !== 'string' || !source.trim().startsWith('{')) return false;
  const document = JSON.parse(source);
  return [...Object.values(document), ...(document.root?.nodes || []), ...(document.root?.templates || [])]
    .some(value => value && typeof value === 'object' && macroTypes.has(value.type));
}

/** One controller per SDK, registered before any initial document writes. */
export class MacroController {
  constructor({ load, show, fail, deferred = true }) {
    Object.assign(this, { load, show, fail, deferred });
    this.state = 'idle';
    this.generation = 0;
    this.tail = Promise.resolve();
    this.waiters = new Set();
    this.view = false;
  }
  check() { if (this.state === 'disposed' || this.sdk?.disposed) throw aborted(); }
  attach(sdk) {
    this.check(); this.sdk = sdk;
    this.revision = 0;
    this.onChange = () => { ++this.revision; };
    sdk.changeEvent?.add(this.onChange);
  }
  requiresMacroFormat(format) { return macroFormats.has(format); }
  detectInputFormat(source, explicit) {
    if (explicit) return explicit;
    if (/^\s*(?:PEPTIDE|RNA|CHEM|BLOB)\d+\s*\{/.test(source)) return 'helm';
    if (/^\s*>/.test(source)) return 'fasta';
  }
  enqueue(action) {
    try { this.check(); } catch (error) { return Promise.reject(error); }
    const task = this.tail.then(() => { this.check(); return action(); });
    this.tail = task.catch(() => {});
    return this.guard(task);
  }
  guard(task) {
    this.check();
    return new Promise((resolve, reject) => {
      const stop = error => reject(error);
      this.waiters.add(stop);
      Promise.resolve(task).then(value => { this.check(); resolve(value); }, reject)
        .catch(reject).finally(() => this.waiters.delete(stop));
    });
  }
  ensure() {
    try { this.check(); } catch (error) { return Promise.reject(error); }
    if (this.state === 'ready') return Promise.resolve(this.macro);
    if (this.pending) return this.pending.promise;
    const generation = ++this.generation;
    this.state = 'loading';
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    promise.catch(() => {});
    const timer = setTimeout(() => this.rejectAttempt(new Error('Macro initialization timed out'), generation), 15000);
    this.pending = { promise, resolve, reject, timer, generation };
    Promise.resolve().then(() => this.load(generation)).catch(error => this.rejectAttempt(error, generation));
    return promise;
  }
  initialized(editor, generation) {
    if (this.state === 'disposed' || generation !== this.generation || !this.pending) {
      editor.destroy(); return;
    }
    this.macro = editor;
    // A hidden virgin Macro does not own the small-molecule document.
    editor._type = 0;
    this.state = 'ready';
    const pending = this.pending;
    this.pending = null;
    clearTimeout(pending.timer);
    pending.resolve(editor);
  }
  rejectAttempt(error, generation) {
    if (generation !== this.generation || this.state === 'disposed') return;
    const pending = this.pending;
    this.pending = null;
    this.state = 'failed';
    if (pending) { clearTimeout(pending.timer); pending.reject(error); }
    this.fail(error, generation);
  }
  async setMode(value) {
    this.check();
    const macro = value ? await this.ensure() : this.macro;
    this.check();
    if (macro && !macro.disposed && macro._type !== (value ? 1 : 0)) {
      if (value) macro.switchToMacromolecules();
      else macro.switchToMicromolecules();
    }
    this.view = value;
    window.isPolymerEditorTurnedOn = value;
    // The host acknowledges a command id, even if the boolean did not change.
    await this.guard(this.show(value));
    if (!value) this.sdk?.editor?.focusCliparea();
  }
  requestMode(value) { return this.enqueue(() => this.setMode(value)); }
  async normalize(source, format, options) {
    this.check();
    const explicit = this.detectInputFormat(source, format);
    if (macroFormats.has(explicit) || containsMacro(source)) await this.ensure();
    if (macroFormats.has(explicit)) {
      const sequenceType = /^chemical\/x-(rna|dna|peptide)-sequence$/.exec(explicit)?.[1].toUpperCase();
      const result = await this.sdk.structService.convert({ struct: source,
        input_format: macroFormats.get(explicit), output_format: 'chemical/x-indigo-ket' },
      { ...options, ...(sequenceType ? { 'sequence-type': sequenceType } : {}) });
      this.check();
      return { source: result.struct, format: 'ket' };
    }
    // Formats that may hide monomers are normalized only during an actual import.
    if (['mol', 'molV3000', 'cdxml', 'smilesExt', 'chemical/x-mdl-molfile', 'chemical/x-cdxml'].includes(explicit)) {
      const result = await this.sdk.structService.convert({ struct: source, output_format: 'chemical/x-indigo-ket' });
      this.check();
      if (containsMacro(result.struct)) { await this.ensure(); return { source: result.struct, format: 'ket' }; }
    }
    return { source, format };
  }
  import(source, options, original) {
    return this.enqueue(async () => {
      const normalized = await this.normalize(source, options?.inputFormat, options);
      this.check();
      const result = await original(normalized.source, options?.inputFormat || normalized.format
        ? { ...options, inputFormat: normalized.format } : options);
      this.check();
      // Importers can change model ownership without changing the visible mode.
      if (this.macro && this.macro._type !== (this.view ? 1 : 0)) await this.setMode(this.view);
      return result;
    });
  }
  dispose() {
    if (this.state === 'disposed') return;
    ++this.generation;
    this.state = 'disposed';
    this.sdk?.changeEvent?.remove(this.onChange);
    const error = aborted();
    if (this.pending) { clearTimeout(this.pending.timer); this.pending.reject(error); this.pending = null; }
    for (const reject of this.waiters) reject(error);
    this.waiters.clear();
    // SDK caches can retain an old service wrapper. Retired capabilities must
    // not keep its document, React callbacks or editor reachable through it.
    this.sdk = this.macro = this.onChange = undefined;
    this.load = this.show = this.fail = () => {};
    this.tail = Promise.resolve();
  }
}
