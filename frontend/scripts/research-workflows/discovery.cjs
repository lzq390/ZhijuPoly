'use strict';

const {
  C, f, at, g, panel, stroke, line, circle, arrow, slider, molecule,
  database, check, atomNode
} = require('./primitives.cjs');

const ids = [
  'polytaoGeneration', 'monomerRetrosynthesis', 'explorer',
  'databaseQuery', 'databaseFilter', 'database'
];

const fade = (content, progress) => g(content, { opacity: progress });
const smallBar = (x, y, width, color = C.border, height = 3) =>
  `<rect x="${f(x)}" y="${f(y)}" width="${f(width)}" height="${height}" rx="${height / 2}" fill="${color}"/>`;

function hexagon(x, y, radius, color = C.bond, width = 1.3) {
  const points = Array.from({ length: 6 }, (_, i) => {
    const angle = (i * 60 - 30) * Math.PI / 180;
    return [x + Math.cos(angle) * radius, y + Math.sin(angle) * radius];
  });
  return stroke(`M${points.map(([px, py]) => `${f(px)},${f(py)}`).join('L')}Z`, color, width);
}

/** A simplified aromatic ester: heteroatom dots remain attached to the bond graph. */
function ester({ x, y, scale = 1, cut = 0, highlight = 0 }) {
  const separation = cut * 8;
  const left = -separation;
  const right = separation;
  let drawing = hexagon(-32 + left, 0, 13.5) + hexagon(32 + right, 0, 13.5);
  drawing += line(-20.3 + left, -6.75, -7 + left, -7, C.bond, 1.3);
  drawing += line(-7 + left, -7, -7 + left, -20, C.bond, 1.3);
  drawing += line(-3.5 + left, -7, -3.5 + left, -20, C.bond, 1.3);
  drawing += atomNode(-5.3 + left, -23, 3.1, 'O');
  drawing += line(8 + right, 0, 20.3 + right, -6.75, C.bond, 1.3);
  drawing += atomNode(7 + right, 0, 3.1, 'O');
  drawing += fade(line(-5, -6, 4, -1, C.bond, 1.3), 1 - cut);
  drawing += fade(line(-5, -6, 4, -1, C.blue, 2.5, 'stroke-linecap="round"'), highlight * (1 - cut));
  drawing += fade(atomNode(-5 + left, -5, 2.7, 'O'), cut);
  drawing += fade(circle(7 + right, 0, 6.5, 'none', `stroke="${C.blue}" stroke-width="1"`), cut * .65);
  return g(drawing, { x, y, scale });
}

function acidFragment(x, y) {
  return g(
    hexagon(-10, 2, 12) + line(.4, -4, 13, -4, C.bond, 1.3) +
    line(13, -4, 13, -15, C.bond, 1.3) + line(16, -4, 16, -15, C.bond, 1.3) +
    atomNode(14.5, -18, 2.8, 'O') + line(14, -3, 24, 4, C.bond, 1.3) + atomNode(27, 6, 2.8, 'O'),
    { x, y, scale: .9 }
  );
}

function alcoholFragment(x, y) {
  return g(hexagon(7, 0, 12) + line(-3.4, -6, -16, -6, C.bond, 1.3) + atomNode(-19, -6, 2.8, 'O'), { x, y, scale: .9 });
}

function candidateCard(x, y, variant, progress, selected = false) {
  return fade(
    panel(x, y, 80, 34, { fill: C.white, stroke: selected ? C.selected : C.border, rx: 7 }) +
    molecule(x + 37, y + 17, .76, { variant }) +
    (selected ? check(x + 70, y + 8, .48) : ''), progress
  );
}

function polytaoGeneration(t) {
  const input = at(t, 100, 1050);
  const action = at(t, 1600, 1400);
  let drawing = panel(18, 36, 84, 108, { fill: C.white, rx: 9 });
  drawing += fade(
    smallBar(30, 49, 28, C.blue, 3) +
    slider(30, 70, 59, .2 + .42 * at(t, 150, 650)) +
    slider(30, 96, 59, .75 - .33 * at(t, 430, 650)) +
    slider(30, 122, 59, .3 + .46 * at(t, 700, 650)), input
  );
  drawing += arrow(107, 90, 125, 90, at(t, 1450, 400));

  const nodes = [[143, 66], [143, 113], [177, 54], [177, 90], [177, 126], [211, 73], [211, 108]];
  let network = panel(130, 36, 98, 108, { fill: C.soft, rx: 9 });
  for (const [a, b] of [[0, 2], [0, 3], [1, 3], [1, 4], [2, 5], [3, 5], [3, 6], [4, 6]]) {
    const [x1, y1] = nodes[a], [x2, y2] = nodes[b];
    network += line(x1, y1, x2, y2, C.border, 1.3);
    network += fade(line(x1, y1, x1 + (x2 - x1) * action, y1 + (y2 - y1) * action, C.blue, 1.5), action);
  }
  nodes.forEach(([x, y], index) => {
    const reached = at(t, 1750 + index * 125, 350);
    network += circle(x, y, 4.2, reached > .5 ? C.blue : C.white, `stroke="${C.blue}" stroke-width="1.3"`);
  });
  drawing += fade(network, at(t, 1600, 350));
  drawing += arrow(233, 90, 252, 90, at(t, 3000, 450));
  drawing += candidateCard(263, 28, 'unit', at(t, 3250, 480), true);
  drawing += candidateCard(263, 73, 'ring', at(t, 3650, 480));
  drawing += candidateCard(263, 118, 'chain', at(t, 4050, 480));
  return drawing;
}

function monomerRetrosynthesis(t) {
  const cut = at(t, 2300, 850);
  let drawing = panel(16, 45, 96, 90, { fill: C.white });
  drawing += fade(ester({ x: 64, y: 94, scale: .84 }), at(t, 120, 1000));
  drawing += arrow(116, 90, 131, 90, at(t, 1500, 400));
  let cleavage = panel(137, 37, 99, 106, { fill: C.soft });
  cleavage += ester({ x: 186, y: 94, scale: .92, cut, highlight: at(t, 1800, 500) });
  cleavage += fade(stroke('M180 73l11 11m0-11-11 11', C.blue, 1.3), at(t, 2050, 300) * (1 - cut));
  drawing += fade(cleavage, at(t, 1600, 400));
  drawing += arrow(240, 90, 254, 90, at(t, 3100, 400));
  drawing += fade(panel(263, 32, 80, 49, { fill: C.white, stroke: C.selected, rx: 7 }) + acidFragment(300, 57), at(t, 3400, 550));
  drawing += fade(line(299, 89, 307, 89, C.muted, 1.3) + line(303, 85, 303, 93, C.muted, 1.3), at(t, 3800, 350));
  drawing += fade(panel(263, 101, 80, 49, { fill: C.white, stroke: C.border, rx: 7 }) + alcoholFragment(301, 127), at(t, 3950, 550));
  return drawing;
}

const neighbors = [[140, 52], [154, 113], [164, 68], [180, 72], [193, 84], [176, 105], [220, 52], [222, 118], [203, 130], [150, 82], [213, 94], [163, 134], [202, 57]];

function explorer(t) {
  let drawing = panel(17, 53, 81, 75, { fill: C.white });
  drawing += fade(molecule(57.5, 89, .95, { variant: 'unit' }), at(t, 100, 1100));
  drawing += arrow(104, 90, 123, 90, at(t, 1450, 450));
  let space = panel(129, 31, 112, 119, { fill: C.soft });
  const scan = at(t, 1750, 1100);
  space += circle(182, 89, 35 * scan, 'none', `stroke="${C.blue}" stroke-width="1" stroke-dasharray="3 4" opacity="${f(scan * .45)}"`);
  neighbors.forEach(([x, y], i) => {
    const chosen = [3, 4, 5].includes(i);
    const selected = chosen ? at(t, 2300 + i * 70, 350) : 0;
    space += circle(x, y, chosen ? 3.1 : 2.4, selected ? C.blue : C.muted, `opacity="${f(chosen ? .55 + selected * .45 : .45 - .23 * at(t, 2550, 500))}"`);
    if (chosen) space += fade(circle(x, y, 6.2, 'none', `stroke="${C.cyan}" stroke-width="1.3"`), selected);
  });
  space += circle(182, 89, 4.5, C.blue) + circle(182, 89, 7.5, 'none', `stroke="${C.blue}" stroke-width="1" opacity=".45"`);
  drawing += fade(space, at(t, 1600, 450));
  drawing += arrow(245, 90, 258, 90, at(t, 3100, 450));
  drawing += candidateCard(263, 43, 'unit', at(t, 3300, 600), true);
  drawing += candidateCard(263, 104, 'ring', at(t, 3850, 600));
  drawing += fade(smallBar(273, 85, 55, C.blue, 2) + smallBar(273, 146, 39, C.cyan, 2), at(t, 4200, 500));
  return drawing;
}

function databaseQuery(t) {
  let drawing = panel(18, 44, 83, 94, { fill: C.white });
  drawing += fade(molecule(59.5, 79, .92, { variant: 'ring' }), at(t, 100, 900));
  drawing += fade(smallBar(29, 116, 9, C.blue, 2) + smallBar(42, 116, 17, C.muted, 2) + smallBar(63, 116, 7, C.blue, 2) + smallBar(74, 116, 14, C.muted, 2), at(t, 800, 600));
  drawing += arrow(107, 90, 136, 90, at(t, 1450, 450));
  let lookup = database(147, 47, 69, 82);
  const scan = at(t, 1750, 1150);
  lookup += fade(line(151, 63 + scan * 40, 212, 63 + scan * 40, C.blue, 2.5), at(t, 1700, 300));
  lookup += fade(circle(214, 118, 12, C.white, `stroke="${C.blue}" stroke-width="1.3"`) + check(214, 118, .8), at(t, 2700, 450));
  drawing += fade(lookup, at(t, 1600, 400));
  drawing += arrow(228, 90, 254, 90, at(t, 3100, 450));
  let record = panel(263, 32, 80, 117, { fill: C.white, stroke: C.selected, rx: 8 });
  record += molecule(303, 64, .88, { variant: 'ring' });
  record += line(273, 89, 333, 89, C.border, 1);
  record += smallBar(275, 103, 16, C.muted) + smallBar(300, 103, 29, C.blue);
  record += smallBar(275, 118, 16, C.muted) + smallBar(300, 118, 23, C.cyan);
  record += smallBar(275, 133, 16, C.muted) + smallBar(300, 133, 32, C.blue);
  drawing += fade(record, at(t, 3350, 800));
  return drawing;
}

const recordMarks = [.36, .72, .24, .85, .57];
function filterRecord(x, y, width, index, selected, opacity = 1) {
  return fade(
    panel(x, y, width, 19, { fill: C.white, stroke: selected ? C.selected : C.border, rx: 4, shadow: false }) +
    circle(x + 9, y + 9.5, 2.5, index % 2 ? C.cyan : C.blue) +
    smallBar(x + 18, y + 7, width * .28, C.muted, 2.5) +
    smallBar(x + width * .62, y + 7, width * .27 * recordMarks[index], selected ? C.blue : C.border, 2.5), opacity
  );
}

function databaseFilter(t) {
  let drawing = panel(16, 35, 86, 111, { fill: C.white });
  drawing += fade(
    slider(28, 61, 60, .18 + .34 * at(t, 100, 800)) +
    slider(28, 91, 60, .8 - .4 * at(t, 420, 800)) +
    slider(28, 121, 60, .12 + .52 * at(t, 700, 800)), at(t, 100, 950)
  );
  drawing += arrow(107, 90, 125, 90, at(t, 1450, 450));
  const rejected = at(t, 2200, 900);
  let filtering = '';
  for (let i = 0; i < 5; i++) {
    const matches = [1, 3, 4].includes(i);
    filtering += filterRecord(132, 32 + i * 25, 103, i, matches && rejected > .6, matches ? 1 : 1 - .83 * rejected);
  }
  drawing += fade(filtering, at(t, 1600, 500));
  drawing += arrow(241, 90, 256, 90, at(t, 3100, 400));
  [1, 3, 4].forEach((recordIndex, i) => {
    const progress = at(t, 3350 + i * 350, 650);
    const y = 48 + i * 33;
    drawing += g(filterRecord(263, y, 80, recordIndex, true), { x: 10 * (1 - progress), opacity: progress });
  });
  return drawing;
}

const correlationPoints = [[.1, .2], [.19, .22], [.27, .35], [.35, .32], [.43, .5], [.53, .44], [.61, .62], [.71, .69], [.79, .82], [.88, .79]];

function analysisDatasetTile(x, y, variant, selected, progress) {
  let body = panel(x, y, 35, 43, { fill: C.white, stroke: selected ? C.selected : C.border, rx: 6, shadow: false });
  if (variant === 'structure') body += molecule(x + 17.5, y + 21, .42, { variant: 'ring' });
  if (variant === 'property') body += line(x + 8, y + 30, x + 8, y + 13, C.cyan, 3) + line(x + 17, y + 30, x + 17, y + 19, C.blue, 3) + line(x + 26, y + 30, x + 26, y + 10, C.cyan, 3);
  if (variant === 'process') body += smallBar(x + 8, y + 12, 18, C.muted, 2.5) + smallBar(x + 8, y + 21, 13, C.blue, 2.5) + smallBar(x + 8, y + 30, 18, C.muted, 2.5);
  if (variant === 'dft') body += stroke(`M${x + 7} ${y + 30}q5-1 9-18t11 15`, C.cyan, 1.3);
  return fade(body, progress);
}

function databaseAnalysis(t) {
  let drawing = analysisDatasetTile(18, 39, 'process', false, at(t, 100, 650));
  drawing += analysisDatasetTile(62, 39, 'property', false, at(t, 300, 650));
  drawing += analysisDatasetTile(18, 97, 'dft', false, at(t, 500, 650));
  drawing += analysisDatasetTile(62, 97, 'structure', t >= 1200, at(t, 700, 650));
  drawing += fade(check(92, 134, .55), at(t, 1200, 350));
  drawing += arrow(103, 90, 122, 90, at(t, 1450, 450));

  const x = 140, y = 132, width = 87, height = 77;
  let analysis = panel(128, 32, 112, 117, { fill: C.white });
  analysis += stroke(`M${x} ${y - height}V${y}H${x + width}`, C.border, 1);
  correlationPoints.forEach(([px, py], i) => {
    analysis += fade(circle(x + px * width, y - py * height, 2.6, C.blue, 'opacity=".7"'), at(t, 1750 + i * 95, 320));
  });
  const trend = at(t, 2450, 650);
  analysis += line(x + 5, y - 9, x + 5 + 74 * trend, y - 9 - 58 * trend, C.cyan, 1.3, 'stroke-dasharray="3 4" opacity=".6"');
  const [sx, sy] = correlationPoints[7];
  const selectedX = x + sx * width, selectedY = y - sy * height;
  analysis += fade(circle(selectedX, selectedY, 6, 'none', `stroke="${C.blue}" stroke-width="1.3"`), at(t, 3250, 450));
  drawing += fade(analysis, at(t, 1600, 450));
  drawing += fade(stroke(`M${f(selectedX + 7)} ${f(selectedY)}H248V63H261`, C.blue, 1.3, 'stroke-dasharray="3 3"'), at(t, 3550, 500));

  let record = panel(264, 34, 79, 112, { fill: C.white, stroke: C.selected, rx: 8 });
  record += molecule(303.5, 64, .82, { variant: 'ring' });
  record += line(274, 88, 333, 88, C.border, 1);
  record += smallBar(275, 101, 16, C.muted) + smallBar(301, 101, 27, C.blue);
  record += smallBar(275, 116, 16, C.muted) + smallBar(301, 116, 21, C.cyan);
  record += smallBar(275, 131, 16, C.muted) + smallBar(301, 131, 30, C.blue);
  drawing += fade(record, at(t, 3750, 750));
  return drawing;
}

const renderers = {
  polytaoGeneration,
  monomerRetrosynthesis,
  explorer,
  databaseQuery,
  databaseFilter,
  database: databaseAnalysis
};

function render(id, t) {
  if (!renderers[id]) throw new Error(`Unknown discovery workflow: ${id}`);
  return renderers[id](t);
}

module.exports = { ids, render };
