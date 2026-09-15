// SDK-level gates: active before onInit and shared by every formatter factory.
var nexpolyCreateFormatter = FormatterFactory.prototype.create;
FormatterFactory.prototype.create = function (format, options, queryPropertiesAreUsed) {
  var factory = this;
  var service = factory.nexpolyStructService;
  var formatter = nexpolyCreateFormatter.call(factory, format, options, queryPropertiesAreUsed);
  var parse = formatter.getStructureFromStringAsync;
  if (parse) formatter.getStructureFromStringAsync = async function (source) {
    var controller = service?.nexpolyMacroController;
    if (!controller) return parse.apply(this, arguments);
    var normalized = await controller.normalize(source, options?.['input-format'] || format, options);
    controller.check();
    if (normalized.format !== format && normalized.format === 'ket') {
      return nexpolyCreateFormatter.call(factory, SupportedFormat.ket, options, queryPropertiesAreUsed)
        .getStructureFromStringAsync(normalized.source);
    }
    return parse.call(this, normalized.source);
  };
  var serialize = formatter.getStructureFromStructAsync;
  if (serialize) formatter.getStructureFromStructAsync = async function () {
    var controller = service?.nexpolyMacroController;
    if (controller?.requiresMacroFormat(format)) await controller.ensure();
    controller?.check();
    // Preserve the caller's struct, drawing manager and selection, including
    // internal save dialogs which deliberately export only selected entities.
    return serialize.apply(this, arguments);
  };
  return formatter;
};
var nexpolySetMolecule = Ketcher.prototype.setMolecule;
// Use the normal local KET commit; host validation and settle remain intact.
Ketcher.prototype.clear = function () { return this.setMolecule('{"root":{"nodes":[]}}'); };
['setMolecule', 'addFragment'].forEach(function (name) {
  var original = Ketcher.prototype[name];
  Ketcher.prototype[name] = function (source, options) {
    var controller = this.structService.nexpolyMacroController;
    if (!controller) return original.apply(this, arguments);
    return controller.import(source, options, (value, opts) => original.call(this, value, opts));
  };
});
var nexpolySetHelm = Ketcher.prototype.setHelm;
Ketcher.prototype.setHelm = function (source) {
  var controller = this.structService.nexpolyMacroController;
  if (!controller) return nexpolySetHelm.apply(this, arguments);
  return controller.import(source, { inputFormat: 'helm' }, value => nexpolySetMolecule.call(this, value));
};
Ketcher.prototype.ensureMacroReady = function () {
  var controller = this.structService.nexpolyMacroController;
  return controller ? controller.ensure().then(() => {}) : Promise.resolve();
};
['switchToMacromoleculesMode', 'switchToMoleculesMode'].forEach(function (name, index) {
  var original = Ketcher.prototype[name];
  Ketcher.prototype[name] = function () {
    var controller = this.structService.nexpolyMacroController;
    return controller ? controller.requestMode(index === 0) : Promise.resolve(original.apply(this, arguments));
  };
});
['getFasta', 'getSequence', 'getIdt'].forEach(function (name) {
  var original = Ketcher.prototype[name];
  Ketcher.prototype[name] = function (format) {
    var controller = this.structService.nexpolyMacroController;
    if (!controller) return original.apply(this, arguments);
    return controller.enqueue(async () => {
      await controller.ensure();
      controller.check();
      // getKet chooses the actual owning model, including micro-mode monomers.
      var ket = await this.getKet();
      var output = name === 'getFasta' ? ChemicalMimeType.FASTA : name === 'getIdt' ? ChemicalMimeType.IDT :
        format === '3-letter' ? ChemicalMimeType.PeptideSequenceThreeLetter : ChemicalMimeType.SEQUENCE;
      var result = await this.structService.convert({ struct: ket, output_format: output });
      controller.check();
      return result.struct;
    });
  };
});
['updateMonomersLibrary', 'setMode', 'setZoom', 'exportImage'].forEach(function (name) {
  var original = Ketcher.prototype[name];
  if (!original) return;
  Ketcher.prototype[name] = function () {
    var controller = this.structService.nexpolyMacroController;
    if (controller && controller.state !== 'ready') throw new Error('Macro is not ready; await ensureMacroReady() first');
    return original.apply(this, arguments);
  };
});
