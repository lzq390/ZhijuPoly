/* Text-free, representative workflows. t is elapsed time in milliseconds. */
const {
  C, f, clamp, at, g, panel, stroke, line, circle, arrow, slider,
  molecule, atomNode, check
} = require('./primitives.cjs');

const ids = [
  'homopolymerPrediction', 'monomerPolymerization', 'mdSimulationDemo',
  'monomerMdSimulation', 'monomerDft', 'conditionalGeneration'
];

const fade = (content, opacity) => g(content, { opacity: clamp(opacity) });
const flow = (t) => ({
  input: at(t, 80, 450), action: at(t, 1600, 420), output: at(t, 3200, 420),
  work: at(t, 1600, 1600), result: at(t, 3500, 1350), complete: at(t, 4850, 250)
});
const dots = (x, y, progress, count = 3) => Array.from({ length: count }, (_, i) =>
  circle(x + i * 8, y, 2, progress >= (i + 1) / count ? C.blue : C.border)
).join('');
const repeatBrackets = (x, y, w, h) =>
  stroke(`M${x + 5} ${y}h-5v${h}h5 M${x + w - 5} ${y}h5v${h}h-5`, C.muted, 1.4);
const arrows = (s, y = 89) => (s.action > 0 ? arrow(102, y, 126, y, s.action) : '')
  + (s.output > 0 ? arrow(226, y, 246, y, s.output) : '');

function chain(x, y, scale = 1, progress = 1, bend = 0) {
  const pts = [[-27, 0], [-18, -8], [-9, 0], [0, -8], [9, 0], [18, -8], [27, 0]];
  const p = pts.map(([px, py], i) => [x + px * scale, y + (py + Math.sin(i * 1.8) * bend) * scale]);
  return p.slice(1).map((point, i) => fade(line(...p[i], ...point, C.bond, 1.3), at(progress, i / 6, 1 / 6))).join('')
    + p.map((point, i) => fade(atomNode(...point, 2.8 * scale, i === 3 ? 'O' : 'C'), at(progress, i / 7, 1 / 7))).join('');
}

function model(x, y, work) {
  const columns = [[0, 16, 32], [-8, 8, 24, 40], [0, 16, 32]];
  let content = '';
  for (let layer = 0; layer < 2; layer++) {
    for (const source of columns[layer]) for (const target of columns[layer + 1]) {
      content += line(x + layer * 25, y + source, x + (layer + 1) * 25, y + target, C.border, 1.1);
    }
  }
  for (let layer = 0; layer < 3; layer++) for (const offset of columns[layer]) {
    const lit = at(work, layer * 0.26, 0.2);
    content += circle(x + layer * 25, y + offset, 4.1, lit > 0.2 ? C.blue : C.white,
      `stroke="${lit > 0.2 ? C.blue : C.border}" stroke-width="1.3"`);
  }
  return content;
}

function prediction(t) {
  const s = flow(t);
  const input = panel(16, 42, 80, 96) + molecule(56, 78, 0.7, { variant: 'unit' })
    + repeatBrackets(30, 57, 53, 44) + slider(30, 119, 52, 0.56);
  const processing = panel(132, 43, 89, 94)
    + model(151, 65, s.work) + dots(165, 121, s.work);
  let output = '';
  for (let i = 0; i < 3; i++) {
    const y = 31 + i * 43;
    const reveal = at(s.result, i * 0.2, 0.45);
    output += panel(251, y, 94, 35);
    // Thermal, mechanical and permeation symbols, without numerical predictions.
    if (i === 0) output += stroke(`M266 ${y + 9}v12a4 4 0 1 0 6 0v-12a3 3 0 0 0-6 0Z`, C.blue, 1.4)
      + line(269, y + 13, 269, y + 24, C.cyan, 2);
    if (i === 1) output += line(262, y + 12, 278, y + 12, C.blue, 1.6)
      + line(262, y + 23, 278, y + 23, C.blue, 1.6)
      + arrow(270, y + 15, 270, y + 20, 1, { color: C.cyan });
    if (i === 2) output += line(269, y + 8, 269, y + 27, C.muted, 2)
      + [0, 1, 2].map((j) => circle(260 + j * 9, y + 16 + (j % 2) * 4, 2, C.cyan)).join('')
      + arrow(273, y + 18, 282, y + 18, reveal, { color: C.cyan });
    output += line(288, y + 13, 334, y + 13, C.soft, 4, 'stroke-linecap="round"')
      + line(288, y + 13, 288 + 43 * [0.72, 0.55, 0.86][i] * reveal, y + 13, C.blue, 4, 'stroke-linecap="round"')
      + line(288, y + 23, 326, y + 23, C.border, 2, 'stroke-linecap="round"');
  }
  return fade(input, s.input) + arrows(s) + fade(processing, s.action) + fade(output, s.output);
}

function polymerization(t) {
  const s = flow(t);
  const input = panel(16, 28, 80, 54) + molecule(55, 54, 0.52, { variant: 'unit' })
    + panel(16, 98, 80, 54) + molecule(55, 125, 0.55, { variant: 'small' })
    + line(52, 87, 60, 87, C.muted, 1.4) + line(56, 83, 56, 91, C.muted, 1.4);
  const join = at(t, 2050, 850);
  const leftX = 151 + join * 12, rightX = 203 - join * 12;
  const process = panel(132, 42, 89, 96)
    + g(molecule(0, 0, 0.45, { variant: 'unit' }), { x: leftX, y: 81 })
    + g(molecule(0, 0, 0.44, { variant: 'small' }), { x: rightX, y: 101 })
    + atomNode(leftX + 8, 88, 2.8, 'O') + atomNode(rightX - 8, 94, 2.8, 'O')
    + fade(line(leftX + 8, 88, rightX - 8, 94, C.bond, 1.3), at(t, 2680, 420))
    + fade(check(177, 124, 0.6), at(t, 3000, 180));
  const output = panel(251, 39, 94, 104)
    + repeatBrackets(261, 59, 74, 42)
    + chain(298, 85, 1.07, s.result)
    + fade(chain(298, 119, 0.72, 1), at(t, 4150, 450));
  return fade(input, s.input) + arrows(s) + fade(process, s.action) + fade(output, s.output);
}

function simulationBox(x, y, w, h, content) {
  const depth = 11;
  return stroke(`M${x} ${y}l${depth} -${depth}h${w}v${h}l-${depth} ${depth}H${x}Z`, C.border, 1.2)
    + `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="2" fill="${C.soft}" fill-opacity=".6" stroke="${C.blue}" stroke-width="1.3"/>`
    + line(x + w, y, x + w + depth, y - depth, C.border, 1.2)
    + content;
}

function historyCurve(x, y, w, h, points, progress, color = C.blue) {
  const plotX = x + 4, plotY = y + 3, plotW = w - 9, plotH = h - 8;
  const path = points.map(([px, py], i) => `${i ? 'L' : 'M'}${f(plotX + px * plotW)} ${f(plotY + (1 - py) * plotH)}`).join('');
  return stroke(`M${plotX} ${plotY}v${plotH}h${plotW}`, C.border, 1)
    + stroke(path, color, 1.8, `pathLength="100" stroke-dasharray="${f(clamp(progress) * 100)} 101"`);
}

function polymerMd(t) {
  const s = flow(t);
  const input = panel(16, 40, 80, 98) + repeatBrackets(28, 52, 56, 42)
    + chain(56, 78, 0.83) + slider(30, 111, 52, 0.6) + slider(30, 123, 52, 0.36);
  const pack = at(t, 1680, 950), run = at(t, 2550, 650);
  let packed = '';
  for (let i = 0; i < 4; i++) {
    const shift = (1 - pack) * (i % 2 ? 18 : -18);
    packed += fade(chain(173 + shift, 64 + i * 17, 0.78, 1, run * (i % 2 ? -3 : 3)), at(t, 1650 + i * 170, 360));
  }
  const process = simulationBox(139, 47, 69, 85, packed)
    + line(140, 145, 211, 145, C.border, 3, 'stroke-linecap="round"')
    + line(140, 145, 140 + run * 71, 145, C.blue, 3, 'stroke-linecap="round"');
  const output = panel(251, 25, 94, 66)
    + chain(296, 54, 0.96, at(t, 3500, 520), -3)
    + fade(stroke('M267 73Q278 64 289 72T327 69', C.cyan, 1.5, 'stroke-dasharray="3 3"'), s.result)
    + panel(251, 100, 94, 55)
    + historyCurve(263, 110, 69, 33,
      [[0, 0.12], [0.13, 0.48], [0.27, 0.65], [0.43, 0.73], [0.63, 0.77], [0.82, 0.76], [1, 0.78]], s.result);
  return fade(input, s.input) + arrows(s) + fade(process, s.action) + fade(output, s.output);
}

function beadMolecule(x, y, angle, color = C.blue, scale = 1) {
  return g(line(-4, 0, 4, 0, C.bond, 1.3)
    + atomNode(-4, 0, 2.8, color === C.cyan ? 'O' : 'C') + atomNode(4, 0, 2.8, 'O'),
    { x, y, scale, rotate: angle });
}

function monomerMd(t) {
  const s = flow(t);
  const input = panel(16, 35, 80, 110)
    + molecule(42, 66, 0.45, { variant: 'small' })
    + molecule(72, 89, 0.43, { variant: 'ring' })
    + slider(29, 121, 54, 0.65);
  const run = at(t, 2200, 1000);
  const seeds = [[151, 61, -20], [178, 59, 55], [200, 72, 115], [154, 91, 40], [182, 92, -35], [204, 112, 60], [160, 119, 95], [185, 122, 5]];
  const packed = seeds.map(([x, y, angle], i) => {
    const move = Math.sin(i * 1.9) * run * 7;
    return fade(beadMolecule(x + move, y + Math.cos(i) * run * 5, angle + run * (i % 2 ? 30 : -35), i % 3 ? C.blue : C.cyan, 0.86), at(t, 1630 + i * 110, 300));
  }).join('');
  const process = simulationBox(139, 47, 69, 85, packed)
    + dots(165, 147, run);
  const trace = stroke('M267 58Q280 35 299 54T329 49 M273 75Q288 57 301 72T328 68', C.cyan, 1.4, 'stroke-dasharray="3 3"')
    + beadMolecule(299, 54, 35, C.blue, 1.15) + beadMolecule(301, 72, -25, C.cyan, 0.86);
  const output = panel(251, 25, 94, 66) + fade(trace, s.result)
    + panel(251, 100, 94, 55)
    + historyCurve(263, 110, 69, 33,
      [[0, 0.18], [0.12, 0.57], [0.3, 0.43], [0.43, 0.66], [0.6, 0.59], [0.8, 0.65], [1, 0.63]], s.result);
  return fade(input, s.input) + arrows(s) + fade(process, s.action) + fade(output, s.output);
}

function geometry(x, y, relaxed, scale = 1) {
  const start = [[-22, 6], [-12, -16], [9, -7], [25, 15], [3, 20]];
  const end = [[-22, 0], [-11, -18], [11, -18], [22, 0], [0, 16]];
  const points = start.map((p, i) => [p[0] + (end[i][0] - p[0]) * relaxed, p[1] + (end[i][1] - p[1]) * relaxed]);
  let content = '';
  [[0, 1], [1, 2], [2, 3], [3, 4], [4, 0]].forEach(([a, b]) => { content += line(...points[a], ...points[b], C.bond, 1.3); });
  points.forEach((p, i) => { content += atomNode(...p, 2.8, i === 2 ? 'O' : 'C'); });
  return g(content, { x, y, scale });
}

function dft(t) {
  const s = flow(t);
  const relaxed = at(t, 1800, 1300);
  const input = panel(16, 42, 80, 96) + geometry(56, 82, 0, 0.86)
    + slider(30, 120, 52, 0.43);
  const orbit = `<ellipse cx="176" cy="87" rx="${f(35 - relaxed * 7)}" ry="16" transform="rotate(-32 176 87)" fill="${C.cyan}" fill-opacity=".09" stroke="${C.cyan}" stroke-opacity=".3"/>`
    + `<ellipse cx="176" cy="87" rx="${f(35 - relaxed * 7)}" ry="16" transform="rotate(36 176 87)" fill="${C.blue}" fill-opacity=".06" stroke="${C.blue}" stroke-opacity=".25"/>`;
  const process = panel(132, 39, 89, 104) + orbit + geometry(176, 88, relaxed, 0.92)
    + dots(164, 128, relaxed);
  const output = panel(251, 25, 94, 66)
    + fade(geometry(297, 61, 1, 0.85), at(t, 3400, 500))
    + panel(251, 100, 94, 55)
    + historyCurve(263, 110, 69, 33,
      [[0, 0.9], [0.13, 0.62], [0.28, 0.4], [0.44, 0.27], [0.63, 0.19], [0.8, 0.17], [1, 0.16]], s.result);
  return fade(input, s.input) + arrows(s) + fade(process, s.action) + fade(output, s.output);
}

function conditional(t) {
  const s = flow(t);
  const target = at(t, 450, 1000);
  const input = panel(16, 29, 80, 65) + molecule(56, 61, 0.63, { variant: 'unit' })
    + panel(16, 104, 80, 45)
    + line(29, 129, 82, 129, C.border, 2)
    + line(42, 121, 42, 137, C.muted, 1.3)
    + circle(42 + target * 25, 129, 4.1, C.blue)
    + arrow(45, 117, 67, 117, target, { color: C.cyan });
  const process = panel(132, 41, 89, 99)
    + molecule(176, 72, 0.57, { variant: 'unit' })
    + fade(stroke('M176 92v10m0 0h-24v12m24-12h24v12', C.cyan, 1.5), at(t, 1970, 500))
    + fade(molecule(152, 123, 0.28, { variant: 'small' }), at(t, 2350, 450))
    + fade(molecule(200, 123, 0.28, { variant: 'ring' }), at(t, 2650, 450));
  let output = '';
  for (let i = 0; i < 3; i++) {
    const y = 25 + i * 44;
    const reveal = at(t, 3390 + i * 350, 500);
    output += fade(panel(251, y, 94, 37)
      + molecule(274, y + 18, 0.29, { variant: ['unit', 'ring', 'chain'][i] })
      + `<rect x="301" y="${y + 9}" width="17" height="19" rx="3" fill="${C.cyan}" fill-opacity=".15"/>`
      + line(293, y + 19, 334, y + 19, C.border, 1.4)
      + circle(307 + (i - 1) * 4, y + 19, 3.2, C.blue)
      + check(333, y + 8, 0.42), reveal);
  }
  return fade(input, s.input) + arrows(s) + fade(process, s.action) + fade(output, s.output);
}

const renderers = {
  homopolymerPrediction: prediction,
  monomerPolymerization: polymerization,
  mdSimulationDemo: polymerMd,
  monomerMdSimulation: monomerMd,
  monomerDft: dft,
  conditionalGeneration: conditional
};

function render(id, t) {
  if (!renderers[id]) throw new Error(`Unknown design workflow: ${id}`);
  return renderers[id](Math.max(0, Number(t) || 0));
}

module.exports = { ids, render };
