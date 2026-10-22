// Regenerate matching public CSVs using unchanged current scenario logic and synthetic rows.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../../..');
const ts = require(path.join(root, 'frontend/node_modules/typescript'));
function loadModule(file, requireHook) {
  const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const context = { exports: {}, require: requireHook };
  vm.runInNewContext(compiled, context);
  return context.exports;
}
const rows = loadModule(path.join(__dirname, 'source/highThroughputDoePiRows.ts'), require);
const scenario = loadModule(path.join(root, 'frontend/src/constants/highThroughputDemoScenario.ts'), id => id === './highThroughputDoePiRows' ? rows : require(id)).highThroughputDemoScenario;
const csv = value => /[",\n]/.test(String(value)) ? `"${String(value).replaceAll('"', '""')}"` : String(value);
const out = path.join(__dirname, 'public/demo-data');
fs.mkdirSync(out, { recursive: true });
for (const file of Object.values(scenario.doeCsvFiles)) {
  const columns = ['doe_run','candidate_id','pi_source_id','monomer_a','monomer_b','cluster','polybert_x','polybert_y','polymer_smiles','monomer_a_smiles','monomer_b_smiles',file.propertyColumn];
  const data = file.rows.map(row => [row.doeRun,row.candidateId,row.sourcePiId,row.monomerA,row.monomerB,row.cluster,row.polybertX,row.polybertY,row.polymerSmiles,row.monomerASmiles,row.monomerBSmiles,row.propertyValue]);
  fs.writeFileSync(path.join(out, file.fileName), [columns, ...data].map(row => row.map(csv).join(',')).join('\n') + '\n');
}
console.log('Generated 4 public CSVs, 12 synthetic rows each.');
