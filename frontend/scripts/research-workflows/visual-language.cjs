/** One visual vocabulary for the fifteen text-free scientific walkthroughs. */
const C = Object.freeze({
  blue: '#2563eb', cyan: '#0891b2', ink: '#315d94', muted: '#718caf',
  border: '#c8dbf3', soft: '#edf4ff', white: '#ffffff',
  bond: '#80a9ed', connector: '#8fb0e9', selected: '#6797f2'
});
const f = value => Number(Number(value).toFixed(3));

function definitions() {
  return `<defs>
    <linearGradient id="background" x1="0" x2="1" y2="1"><stop stop-color="#fafcff"/><stop offset="1" stop-color="#edf5ff"/></linearGradient>
    <radialGradient id="ambient"><stop stop-color="#d9f2fc" stop-opacity=".55"/><stop offset="1" stop-color="#edf5ff" stop-opacity="0"/></radialGradient>
    <linearGradient id="surface" x1="0" y1="0" x2=".35" y2="1"><stop stop-color="#ffffff"/><stop offset="1" stop-color="#fbfdff"/></linearGradient>
    <linearGradient id="surface-soft" x1="0" y1="0" x2=".35" y2="1"><stop stop-color="#f3f8ff"/><stop offset="1" stop-color="#edf4ff"/></linearGradient>
    <filter id="shadow" x="-30%" y="-30%" width="160%" height="180%"><feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#2556a8" flood-opacity=".055"/></filter>
    ${[['C', '#9abcf4', C.blue], ['O', '#7ecddc', C.cyan], ['N', '#a5c4fa', '#4c76c8'], ['H', '#ffffff', '#c7daf4']].map(([element, light, color]) => `<radialGradient id="atom-${element}" cx="32%" cy="26%" r="85%"><stop stop-color="${light}"/><stop offset=".65" stop-color="${color}"/><stop offset="1" stop-color="${color}"/></radialGradient>`).join('')}
  </defs>`;
}

function frame(body, { reset = 0, start = '', contentScale = .96 } = {}) {
  // Fixed framing keeps subject sizes and safe margins stable throughout a GIF.
  // The original knowledge composition already includes this outer safe margin.
  const transform = `translate(${f(180 * (1 - contentScale))} ${f(90 * (1 - contentScale))}) scale(${contentScale})`;
  const background = '<rect width="360" height="180" fill="url(#background)"/><ellipse cx="295" cy="30" rx="130" ry="100" fill="url(#ambient)"/>';
  return `<svg xmlns="http://www.w3.org/2000/svg" width="720" height="360" viewBox="0 0 360 180" stroke-linecap="round" stroke-linejoin="round">
    ${definitions()}${background}
    <g transform="${transform}"><g opacity="1">${body}</g></g>${reset ? `<g opacity="${f(reset)}">${background}<g transform="${transform}">${start}</g></g>` : ''}
  </svg>`;
}

const POSTER_TIME = 5840;
const ease = value => { value = Math.max(0, Math.min(1, value)); return value * value * (3 - 2 * value); };

function playbackFrame(render, time, options = {}) {
  const cover = render(POSTER_TIME);
  // The first and last sampled frames are the same composition as the SVG
  // poster. Holds at both ends also cover the browser's initial image decode.
  if (time < 160 || time >= 7040) return frame(cover, options);
  if (time < 640) return frame(cover, { ...options, reset: ease((time - 160) / 480), start: render(time) });
  if (time >= 6800) return frame(render(time), { ...options, reset: ease((time - 6800) / 240), start: cover });
  return frame(render(time), options);
}

module.exports = { C, frame, playbackFrame, POSTER_TIME };
