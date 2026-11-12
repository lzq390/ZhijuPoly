// Symbolic previews of existing optimization workflows. No numerical results are
// implied: the Tg module searches candidates, and high throughput is a demo.
const { C, f, at, g, panel, line, circle, arrow, slider, molecule, chart, dataTable, check } = require('./primitives.cjs');

const ids = ['reverseDesign', 'highThroughputWorkflowDemo'];

function thermometer(x, y, progress) {
  const level = 12 + 18 * progress;
  return g(
    `<path d="M-3 18V-14a3 3 0 0 1 6 0v32a7 7 0 1 1-6 0Z" fill="${C.white}" stroke="${C.border}" stroke-width="1.2"/>`
    + `<path d="M0 20V${f(20-level)}" fill="none" stroke="${C.blue}" stroke-width="2.5" stroke-linecap="round"/>`
    + circle(0, 24, 4.2, C.blue)
    + [-10, -2, 6].map(tick => line(7, tick, 11, tick, C.border, 1)).join(''),
    { x, y }
  );
}

function targetMarker(x, y, opacity = 1) {
  return g(circle(0, 0, 5.5, C.white, `stroke="${C.cyan}" stroke-width="1.4"`)
    + circle(0, 0, 2.3, C.cyan), { x, y, opacity });
}

function tgSearch(t) {
  const input = at(t, 80, 1120), action = at(t, 1600, 1150), output = at(t, 3200, 1600);
  const target = 0.32 + 0.34 * input;
  const left = panel(16, 34, 82, 112)
    + thermometer(38, 62, input)
    + g(molecule(76, 73, .42, { variant: 'unit', reveal: input }), { opacity: .3 + .7 * input })
    + slider(30, 119, 54, target)
    + line(30, 132, 69, 132, C.border, 1.1)
    + circle(77, 132, 2.4, C.cyan);

  const scanX = 136 + 76 * action;
  const scanOpacity = at(t, 1660, 240) * (1 - at(t, 2840, 260));
  const center = panel(120, 40, 112, 100, { opacity: .45 + .55 * at(t, 1500, 300) })
    + g(chart(130, 74, 91, 49, { kind: 'distribution', progress: action, color: C.blue }), { opacity: .25 + .75 * action })
    + `<rect x="175" y="67" width="14" height="57" rx="3" fill="${C.cyan}" opacity="${f(.04 + .11 * action)}"/>`
    + line(182, 66, 182, 124, C.cyan, 1.1, `stroke-dasharray="3 3" opacity="${f(action)}"`)
    + targetMarker(182, 61, action)
    + g(line(scanX, 70, scanX, 121, C.blue, 1, 'stroke-dasharray="2 3"')
      + circle(scanX, 70, 2.5, C.blue), { opacity: scanOpacity })
    + g(check(219, 132, .65), { opacity: at(t, 2980, 300) });

  const right = [36, 77, 118].map((y, index) => {
    const reveal = at(t, 3200 + index * 360, 560);
    const body = panel(250, y, 94, 30, { opacity: reveal, stroke: index === 0 ? C.blue : C.border })
      + g(molecule(274, y + 15, .48, { variant: index === 1 ? 'ring' : 'unit', reveal }), { opacity: reveal })
      + g(line(299, y + 11, 329 - index * 5, y + 11, index === 0 ? C.blue : C.cyan, 2, 'stroke-linecap="round"')
        + line(299, y + 18, 319 - index * 3, y + 18, C.border, 1.3, 'stroke-linecap="round"'), { opacity: reveal })
      + g(check(337, y + 23, .48), { opacity: reveal });
    return g(body, { x: 7 * (1 - reveal), opacity: reveal });
  }).join('');

  return left
    + arrow(100, 90, 117, 90, at(t, 1400, 420))
    + center
    + arrow(234, 90, 247, 90, at(t, 3000, 430))
    + right
    + g(line(258, 157, 335, 157, C.border, 1), { opacity: output * .55 });
}

function highThroughput(t) {
  const input = at(t, 80, 1120), action = at(t, 1600, 1300);
  const recommended = at(t, 2150, 500), verified = at(t, 2700, 460), blend = at(t, 3760, 1000);
  const points = [[139, 111], [151, 99], [161, 109], [170, 87], [185, 96], [200, 75], [212, 87]];
  const left = panel(16, 34, 82, 112)
    + g(dataTable(24, 48, 66, 58, { rows: 3, progress: input }), { opacity: .25 + .75 * input })
    + [0, 1, 2, 3].map(index => g(circle(32 + index * 15, 128, 3, index % 2 ? C.cyan : C.blue)
      + line(28 + index * 15, 137, 36 + index * 15, 137, C.border, 1.2), { opacity: at(t, 600 + index * 160, 320) })).join('');

  const ringRadius = 7.5, circumference = 2 * Math.PI * ringRadius;
  const center = panel(120, 34, 112, 112, { opacity: .45 + .55 * at(t, 1500, 320) })
    + line(132, 127, 220, 127, C.border, 1)
    + line(132, 127, 132, 49, C.border, 1)
    + points.map(([x, y], index) => g(circle(x, y, 2.8, index % 3 === 0 ? C.cyan : C.blue),
      { opacity: .65 * at(t, 1620 + index * 80, 350) })).join('')
    + g(`<path d="M136 117C154 113 163 103 176 97S201 80 216 69" fill="none" stroke="${C.blue}" stroke-width="1.6" stroke-linecap="round" pathLength="1" stroke-dasharray="1" stroke-dashoffset="${f(1-action)}"/>`, { opacity: .7 * action })
    + circle(190, 84, ringRadius, 'none', `stroke="${C.cyan}" stroke-width="1.5" stroke-dasharray="${f(circumference)}" stroke-dashoffset="${f(circumference*(1-recommended))}" opacity="${f(recommended)}"`)
    + g(circle(190, 84, 3.2, C.cyan), { opacity: recommended })
    + g(check(202, 72, .72), { opacity: verified })
    + g(`<path d="M208 96C222 110 202 139 167 133C156 131 147 126 144 120" fill="none" stroke="${C.cyan}" stroke-width="1.2" stroke-linecap="round" pathLength="1" stroke-dasharray="1" stroke-dashoffset="${f(1-verified)}"/>`
      + `<path d="m141 123 2-6 6 2" fill="none" stroke="${C.cyan}" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>`, { opacity: verified * .75 });

  const colors = [C.blue, C.cyan, '#86a9eb', '#75c9cf'];
  const components = [[269, 54], [325, 54], [269, 84], [325, 84]].map(([x, y], index) => {
    const reveal = at(t, 3200 + index * 160, 420);
    return g(panel(x - 16, y - 12, 32, 24, { rx: 5, shadow: false, stroke: C.border })
      + molecule(x, y, .36, { variant: index % 2 ? 'ring' : 'small', reveal })
      + circle(x + 13, y + 10, 2, colors[index]), { opacity: reveal, y: 4 * (1-reveal) });
  }).join('');
  const widths = [25, 17, 22, 14];
  let offset = 258;
  const mixture = widths.map((width, index) => {
    const fill = `<rect x="${offset}" y="115" width="${width}" height="11" fill="${colors[index]}" opacity="${f(.2 + .8*blend)}"/>`;
    offset += width;
    return fill;
  }).join('');
  const right = panel(250, 34, 94, 112, { opacity: .35 + .65 * at(t, 3160, 400) })
    + components
    + g(line(270, 99, 270, 105, C.border, 1)
      + line(324, 99, 324, 105, C.border, 1)
      + line(270, 105, 324, 105, C.border, 1)
      + arrow(297, 105, 297, 112, blend, { color: C.cyan }), { opacity: blend })
    + g(mixture + `<rect x="258" y="115" width="78" height="11" rx="2" fill="none" stroke="${C.border}" stroke-width=".8"/>`, { opacity: blend })
    + g(check(297, 137, .65), { opacity: at(t, 4740, 260) });

  return left
    + arrow(100, 90, 117, 90, at(t, 1400, 420))
    + center
    + arrow(234, 90, 247, 90, at(t, 3000, 430))
    + right;
}

function render(id, t) {
  const time = Math.max(0, Math.min(7200, Number(t) || 0));
  if (id === 'reverseDesign') return tgSearch(time);
  if (id === 'highThroughputWorkflowDemo') return highThroughput(time);
  throw new Error(`Unknown optimization workflow: ${id}`);
}

module.exports = { ids, render };
