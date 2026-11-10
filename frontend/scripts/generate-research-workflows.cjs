/** Generate functional walkthrough GIFs and SVG posters offline.
 * node scripts/generate-research-workflows.cjs [--only=module,module] [--jobs=3]
 * The approved knowledge-search assets have their own generator and are preserved.
 */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const {playbackFrame,POSTER_TIME}=require('./research-workflows/visual-language.cjs');
const workflows=[require('./research-workflows/discovery.cjs'),require('./research-workflows/design.cjs'),require('./research-workflows/optimization.cjs')];
const entries=workflows.flatMap(workflow=>workflow.ids.map(id=>({id,render:workflow.render})));
const only=process.argv.find(arg=>arg.startsWith('--only='))?.slice(7).split(',');
if(only)for(const id of only)assert(entries.some(entry=>entry.id===id),`Unknown workflow ${id}`);
const selected=only?entries.filter(entry=>only.includes(entry.id)):entries;
const postersOnly=process.argv.includes('--posters-only');
const jobs=Math.max(1,Math.min(4,Number(process.argv.find(arg=>arg.startsWith('--jobs='))?.slice(7)||3)));
const destination=path.resolve(__dirname,'../public/previews/research-tools');
const artifactDirectory=path.resolve(process.env.WORKFLOW_ARTIFACT_DIR||'/tmp/nexpoly-functional-workflows');
const frameCount=90,frameDuration=80;
function svg(render,id,t) {
  const body=render(id,t);
  assert(!/<text(?:\s|>)/.test(body),`${id}: no explanatory text in workflow`);
  assert(!/NaN|undefined|Infinity/.test(body),`${id}: invalid coordinates`);
  return playbackFrame(time=>render(id,time),t);
}
function python(args) {
  return new Promise((resolve,reject)=>{
    const child=spawn('python3',args,{stdio:['ignore','pipe','pipe']});let stdout='',stderr='';
    child.stdout.on('data',chunk=>{stdout+=chunk});child.stderr.on('data',chunk=>{stderr+=chunk});
    child.on('error',reject);child.on('exit',code=>code===0?resolve(stdout):reject(new Error(stderr||stdout||`Python exited ${code}`)));
  });
}
(async()=>{
  await fs.mkdir(destination,{recursive:true});await fs.mkdir(artifactDirectory,{recursive:true});
  const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'nexpoly-workflows-'));
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const results=[];
  try {
    let next=0;
    await Promise.all(Array.from({length:Math.min(jobs,selected.length)},async()=>{
      const page=await browser.newPage({viewport:{width:720,height:360},deviceScaleFactor:1});
      await page.route('**/*',route=>route.abort());
      await page.setContent('<style>body{margin:0;overflow:hidden}svg{display:block}</style><main></main>');
      while(next<selected.length){
        const {id,render}=selected[next++];
        const poster=svg(render,id,POSTER_TIME);
        const xmlError=await page.evaluate(markup=>new DOMParser().parseFromString(markup,'image/svg+xml').querySelector('parsererror')?.textContent||null,poster);
        assert.equal(xmlError,null,`${id}: poster must be valid standalone SVG XML`);
        await fs.writeFile(path.join(destination,`${id}.svg`),poster);
        if(postersOnly){results.push({id,posterValid:true});continue}
        const frames=path.join(temporary,id);await fs.mkdir(frames);
        for(let frame=0;frame<frameCount;frame++){
          await page.locator('main').evaluate((node,markup)=>{node.innerHTML=markup},svg(render,id,frame*frameDuration));
          await page.screenshot({path:path.join(frames,`${String(frame).padStart(3,'0')}.png`)});
        }
        const result=JSON.parse((await python([path.join(__dirname,'research-workflows/encode_gif.py'),frames,path.join(destination,`${id}.gif`),artifactDirectory])).trim());
        results.push(result);process.stdout.write(JSON.stringify(result)+'\n');
        await fs.rm(frames,{recursive:true,force:true});
      }
      await page.close();
    }));
    results.sort((a,b)=>entries.findIndex(entry=>entry.id===a.id)-entries.findIndex(entry=>entry.id===b.id));
    if(postersOnly){await fs.writeFile(path.join(artifactDirectory,'poster-validation.json'),JSON.stringify(results,null,2)+'\n');return}
    await fs.writeFile(path.join(artifactDirectory,'workflow-assets.json'),JSON.stringify(results,null,2)+'\n');
    await python(['-c',`
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
import json,sys,math
p=Path(sys.argv[1]);rows=json.loads((p/'workflow-assets.json').read_text())
cellw,cellh=360,202
out=Image.new('RGB',(cellw*3,cellh*math.ceil(len(rows)/3)),'#edf3f9')
font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',11)
draw=ImageDraw.Draw(out)
for i,item in enumerate(rows):
    im=Image.open(p/f"{item['id']}-poster.png").resize((360,180),Image.Resampling.LANCZOS)
    x,y=(i%3)*cellw,(i//3)*cellh
    out.paste(im,(x,y));draw.text((x+8,y+183),item['id'],font=font,fill='#0b1f3a')
out.save(p/'all-workflow-posters.png')
`,artifactDirectory]);
  }finally{await browser.close();await fs.rm(temporary,{recursive:true,force:true})}
})().catch(error=>{console.error(error);process.exitCode=1});
