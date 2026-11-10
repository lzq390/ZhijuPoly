/* Generate the text-free knowledge-search walkthrough and matching static poster.
 * Run: node scripts/generate-knowledge-preview.cjs (requires Playwright and Pillow).
 * No API requests or runtime animation dependency. */
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');
const { C, atomNode } = require('./research-workflows/primitives.cjs');
const { playbackFrame, POSTER_TIME } = require('./research-workflows/visual-language.cjs');
const destination = path.resolve(__dirname, '../public/previews/research-tools');
const artifactDirectory = path.resolve(process.env.WORKFLOW_ARTIFACT_DIR || '/tmp/nexpoly-functional-workflows');
const clamp = n => Math.max(0, Math.min(1, n));
const ease = n => { n = clamp(n); return n * n * (3 - 2 * n); };
const enter = (t, start, length = 400) => ease((t - start) / length);
const f = n => Number(n.toFixed(3));
const mix = (a, b, p) => f(a + (b - a) * p);
const BLUE = C.blue;
const CYAN = C.cyan;
function searchIcon(x, y, s = 1, color = BLUE) {
  return `<g transform="translate(${f(x)} ${f(y)}) scale(${f(s)})" fill="none" stroke="${color}" stroke-width="1.8" stroke-linecap="round"><circle cx="8" cy="8" r="6.2"/><path d="m12.5 12.5 4.8 4.8"/></g>`;
}
function arrow(x, y, opacity = 1) {
  return `<path d="m${x - 4} ${y - 4} 4 4-4 4" fill="none" stroke="#8fb0e9" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" opacity="${f(opacity)}"/>`;
}
function line(x, y, width, color = '#c5d5e9', thickness = 3) {
  return `<rect x="${f(x)}" y="${f(y)}" width="${f(Math.max(width, .01))}" height="${thickness}" rx="${thickness / 2}" fill="${color}"/>`;
}
function search(t) {
  const compact = enter(t, 1520, 650);
  const x = mix(58, 20, compact), y = mix(69, 72, compact);
  const w = mix(244, 76, compact), h = mix(43, 37, compact);
  const query = enter(t, 280, 700);
  const query2 = enter(t, 700, 430);
  const click = enter(t, 1280, 330);
  const clickVisible = t >= 1280 && t < 1610;
  const buttonOpacity = 1 - compact;
  return `<ellipse cx="${mix(180, 58, compact)}" cy="${mix(126, 122, compact)}" rx="${mix(105, 36, compact)}" ry="6" fill="#2b61b7" opacity=".045"/>
    <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="9" fill="url(#surface)" stroke="${C.border}" filter="url(#shadow)"/>
    ${searchIcon(Number(x) + 14, Number(y) + 12, mix(1.12, .95, compact))}
    ${line(Number(x) + mix(43, 39, compact), Number(y) + mix(15, 13, compact), mix(62, 21, compact) * query, '#82aaf0', mix(4, 3, compact))}
    ${line(Number(x) + mix(43, 39, compact), Number(y) + mix(25, 22, compact), mix(94, 15, compact) * query2, '#bdd0ec', 3)}
    <g opacity="${f(buttonOpacity)}">
      <rect x="${Number(x) + Number(w) - 36}" y="${Number(y) + 7}" width="28" height="29" rx="8" fill="${t >= 1280 ? '#1d4ed8' : BLUE}"/>
      <path d="M${Number(x) + Number(w) - 28} ${Number(y) + 21.5}h12m-5-5 5 5-5 5" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      ${clickVisible ? `<circle cx="${Number(x) + Number(w) - 22}" cy="${Number(y) + 21.5}" r="${mix(7, 25, click)}" fill="none" stroke="${BLUE}" stroke-width="1.4" opacity="${f((1 - click) * .6)}"/>` : ''}
    </g>`;
}
function documents(t) {
  const show = enter(t, 1880, 500);
  const choose = enter(t, 2760, 420);
  const y = mix(61, 42, show);
  return `<g opacity="${f(show)}" transform="translate(0 ${mix(12, 0, show)})">
    <ellipse cx="173" cy="153" rx="55" ry="7" fill="#2b61b7" opacity=".045"/>
    <g transform="rotate(-8 169 96)"><rect x="119" y="${Number(y) + 8}" width="82" height="101" rx="9" fill="#eaf2fc" stroke="#d0e0f4"/></g>
    <g transform="rotate(7 174 96)"><rect x="139" y="${Number(y) + 6}" width="77" height="100" rx="9" fill="#f5f9ff" stroke="#cbdcf3"/></g>
    <rect x="130" y="${y}" width="82" height="104" rx="9" fill="url(#surface)" stroke="${choose > .5 ? C.selected : C.border}" stroke-width="${mix(1, 1.2, choose)}" filter="url(#shadow)"/>
    <path d="M189 ${y}v13a3 3 0 0 0 3 3h20" fill="#edf4ff" stroke="#d2e1f5" stroke-width="1"/>
    <rect x="141" y="${Number(y) + 14}" width="21" height="6" rx="2" fill="#7ca5ef"/>
    ${line(141, Number(y) + 28, 54, '#b0c5e1', 3)}
    ${line(141, Number(y) + 36, 42, '#c4d3e7', 2.5)}
    <rect x="141" y="${Number(y) + 49}" width="60" height="30" rx="4" fill="#f0f6fe"/>
    <path d="M147 ${Number(y) + 72}h47M148 ${Number(y) + 72}v-16" stroke="#c5d7ef" stroke-width="1" fill="none"/>
    <path d="M149 ${Number(y) + 69}c7-1 8-10 15-8s7 3 13-3 10-3 15-5" stroke="${BLUE}" stroke-width="1.5" fill="none" stroke-linecap="round"/>
    ${line(141, Number(y) + 87, 51, '#c9d8eb', 2.5)}
    ${line(141, Number(y) + 94, 38, '#d7e2f1', 2.5)}
    <g opacity="${f(choose)}"><circle cx="208" cy="143" r="9" fill="${BLUE}" stroke="white" stroke-width="2"/><path d="m204 143 3 3 5-6" fill="none" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></g>
  </g>`;
}
function molecule() {
  return `<g fill="none" stroke="${C.bond}" stroke-width="1.3"><path d="m12 19 10-7 12 7v13l-12 7-10-7zM34 19l11-6 11 6M34 32l11 6 11-6" transform="translate(5 -5) scale(.8)"/></g>
    ${[[14.6,10.2,'C'],[22.6,4.6,'C'],[32.2,10.2,'C'],[32.2,20.6,'O'],[22.6,26.2,'C'],[14.6,20.6,'C'],[50,10.2,'O']].map(([x,y,element]) => atomNode(x,y,2.6,element)).join('')}`;
}
function curve() {
  return `<path d="M11 7v23h49" fill="none" stroke="#c5d8eb" stroke-width="1"/><path d="M15 26c5 0 5-12 11-10s7 6 12-2 7-6 17-9" stroke="${CYAN}" stroke-width="1.8" fill="none" stroke-linecap="round"/><circle cx="38" cy="14" r="2.5" fill="${CYAN}" stroke="white"/>`;
}
function table() {
  return `<rect x="12" y="7" width="47" height="23" rx="3" fill="#f0f6ff" stroke="#d2e2f6"/><path d="M12 15h47M12 23h47M27 7v23" stroke="#d2e2f6" stroke-width=".8"/>${line(17,10,6,'#83acee',2)}${line(33,10,17,'#aac4ec',2)}${line(17,18,6,'#aac4ec',2)}${line(33,18,12,'#83acee',2)}${line(17,26,6,'#aac4ec',2)}${line(33,26,19,'#83acee',2)}`;
}
function data(t) {
  return [molecule, curve, table].map((draw,index) => {
    const show = enter(t, 3510 + index * 440, 430);
    const y = 26 + index * 48;
    return `<g opacity="${f(show)}">
      <path d="M214 94h26V${y + 18}h20" fill="none" stroke="#8fb0e9" stroke-width="1.25" stroke-dasharray="${f(show * 130)} 180" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="214" cy="94" r="2.3" fill="${BLUE}"/>
      <g transform="translate(${mix(12,0,show)} 0)"><rect x="263" y="${y}" width="72" height="37" rx="7" fill="url(#surface)" stroke="${C.border}" filter="url(#shadow)"/><g transform="translate(263 ${y})">${draw()}</g></g>
    </g>`;
  }).join('');
}
function body(t) {
  const connect = enter(t, 2320, 420);
  return `${search(t)}<g opacity="${f(connect)}"><path d="M99 91h25" fill="none" stroke="#8fb0e9" stroke-width="1.3" stroke-dasharray="${f(connect*27)} 40"/>${arrow(125,91,connect)}</g>${documents(t)}${data(t)}`;
}
function svg(t) {
  return playbackFrame(body,t,{contentScale:1});
}

(async () => {
  await fs.mkdir(destination,{recursive:true});
  await fs.mkdir(artifactDirectory,{recursive:true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(),'knowledge-abstract-'));
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport:{width:720,height:360},deviceScaleFactor:1});
    await page.setContent('<style>body{margin:0;overflow:hidden}svg{display:block}</style><main></main>');
    const poster=svg(POSTER_TIME);
    if (poster.includes('<text') || /[\u3400-\u9fff]/.test(poster)) throw new Error('Preview must remain text-free');
    const xmlError=await page.evaluate(markup=>new DOMParser().parseFromString(markup,'image/svg+xml').querySelector('parsererror')?.textContent||null,poster);
    if(xmlError) throw new Error('Knowledge poster must be valid standalone SVG XML: '+xmlError);
    await fs.writeFile(path.join(destination,'knowledge.svg'),poster);
    if(process.argv.includes('--posters-only')) return;
    for(let frame=0;frame<90;frame++) {
      await page.locator('main').evaluate((node,markup)=>{node.innerHTML=markup},svg(frame*80));
      await page.screenshot({path:path.join(temporary,`${String(frame).padStart(3,'0')}.png`)});
    }
    const result=spawnSync('python3',[path.join(__dirname,'research-workflows/encode_gif.py'),temporary,path.join(destination,'knowledge.gif'),artifactDirectory],{encoding:'utf8'});
    if(result.status!==0) throw new Error(result.stderr||result.stdout);
    process.stdout.write(result.stdout);
  }finally{
    await browser.close();
    await fs.rm(temporary,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1});
