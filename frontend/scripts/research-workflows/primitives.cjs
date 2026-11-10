/** Shared text-free SVG drawing vocabulary for offline workflow previews. */
const geometry = require('./geometry.json');
const { C } = require('./visual-language.cjs');
const f = value => Number(Number(value).toFixed(3));
const clamp = value => Math.max(0,Math.min(1,value));
const smooth = value => {value=clamp(value);return value*value*(3-2*value)};
const at = (t,start,duration=400) => smooth((t-start)/duration);
function g(content,{x=0,y=0,scale=1,opacity=1,rotate=0}={}) {
  return `<g transform="translate(${f(x)} ${f(y)}) rotate(${f(rotate)}) scale(${f(scale)})" opacity="${f(opacity)}">${content}</g>`;
}
function panel(x,y,w,h,{fill=C.white,stroke=C.border,rx=9,shadow=true,opacity=1}={}) {
  const surface=fill===C.white?'url(#surface)':fill===C.soft?'url(#surface-soft)':fill;
  const selected=stroke===C.blue||stroke===C.selected;
  return `<rect x="${f(x)}" y="${f(y)}" width="${f(w)}" height="${f(h)}" rx="${f(rx)}" fill="${surface}" stroke="${selected?C.selected:stroke}" stroke-width="${selected?1.2:1}" opacity="${f(opacity)}"${shadow?' filter="url(#shadow)"':''}/>`;
}
function stroke(d,color=C.blue,width=1.5,attrs='') {
  // Standalone SVG uses strict XML: a caller overriding an attribute must not
  // duplicate it. Inline SVG parsing would otherwise conceal broken posters.
  const defaults=[['fill','none'],['stroke',color],['stroke-width',f(width)],['stroke-linecap','round'],['stroke-linejoin','round']];
  const base=defaults.filter(([name])=>!new RegExp(`(?:^|\\s)${name}\\s*=`).test(attrs)).map(([name,value])=>`${name}="${value}"`).join(' ');
  return `<path d="${d}" ${base} ${attrs}/>`;
}
function line(x1,y1,x2,y2,color=C.blue,width=1.5,attrs='') {return stroke(`M${f(x1)} ${f(y1)}L${f(x2)} ${f(y2)}`,color,width,attrs)}
function circle(x,y,r,fill=C.blue,attrs='') {return `<circle cx="${f(x)}" cy="${f(y)}" r="${f(r)}" fill="${fill}" ${attrs}/>`}
function atomNode(x,y,r=2.6,element='C',attrs='') {
  return circle(x,y,r,`url(#atom-${element})`,`stroke="white" stroke-opacity=".65" stroke-width=".55" ${attrs}`);
}
function arrow(x1,y1,x2,y2,progress=1,{color=C.connector,head=true}={}) {
  progress=clamp(progress); if(progress<=0)return '';
  const distance=Math.hypot(x2-x1,y2-y1),angle=Math.atan2(y2-y1,x2-x1);
  let result=line(x1,y1,x2,y2,color,1.3,`stroke-dasharray="${f(distance*progress)} ${f(distance+1)}"`);
  if(head&&progress>.9)result+=g(stroke('M-4-3.5 0 0-4 3.5',color,1.3),{x:x2,y:y2,rotate:angle*180/Math.PI,opacity:(progress-.9)*10});
  return result;
}
function slider(x,y,w,value) {
  value=clamp(value);
  return line(x,y,x+w,y,'#dbe6f4',3)+line(x,y,x+w*value,y,'#81a8ef',3)+circle(x+w*value,y,3.6,'white',`stroke="${C.blue}" stroke-width="1.3"`);
}
function molecule(x,y,scale=1,{variant='ring',reveal=1,rotation=0,highlightBond=-1,split=0}={}) {
  const source=geometry[variant]||geometry.ring;
  const points=source.atoms.map(([element,ax,ay,az])=>({element,px:ax,py:ay*.87-az*.46,z:ay*.46+az*.87}));
  const minX=Math.min(...points.map(p=>p.px)),maxX=Math.max(...points.map(p=>p.px));
  const minY=Math.min(...points.map(p=>p.py)),maxY=Math.max(...points.map(p=>p.py));
  const unit=66/Math.max(1,maxX-minX),cx=(minX+maxX)/2,cy=(minY+maxY)/2;
  let selected=highlightBond>=0?highlightBond:2;
  selected=Math.min(selected,source.bonds.length-1);
  const [splitA,splitB]=source.bonds[selected];
  const right=new Set([splitB]),queue=[splitB];
  while(queue.length){const atom=queue.shift();source.bonds.forEach(([a,b],index)=>{if(index===selected)return;const next=a===atom?b:b===atom?a:-1;if(next>=0&&!right.has(next)){right.add(next);queue.push(next)}})}
  const projected=points.map((p,index)=>({...p,px:(p.px-cx)*unit+(split?(right.has(index)?1:-1)*split*8:0),py:(p.py-cy)*unit}));
  const pieces=[];
  source.bonds.forEach(([a,b,order],index)=>{
    const start=projected[a],end=projected[b],visible=clamp(reveal*source.bonds.length-index);
    if(visible<=0||index===selected&&split>.3)return;
    const dx=(end.px-start.px)*visible,dy=(end.py-start.py)*visible;
    const accent=index===highlightBond?C.cyan:C.bond;
    let item=line(start.px,start.py,start.px+dx,start.py+dy,accent,index===highlightBond?2.4:1.3);
    if(order>=1.5){const norm=Math.max(1,Math.hypot(dx,dy));item+=line(start.px-dy/norm*1.8,start.py+dx/norm*1.8,start.px+dx*.92-dy/norm*1.8,start.py+dy*.92+dx/norm*1.8,C.bond,.75)}
    pieces.push({z:(start.z+end.z)/2-.1,svg:item});
  });
  projected.forEach((p,index)=>{const opacity=clamp(reveal*source.atoms.length-index+1);if(!opacity)return;pieces.push({z:p.z,svg:atomNode(p.px,p.py,p.element==='H'?1.45:p.element==='O'?2.75:2.6,p.element,`opacity="${f(opacity)}"`)})});
  return g(pieces.sort((a,b)=>a.z-b.z).map(p=>p.svg).join(''),{x,y,scale,rotate:rotation});
}
const defaultPoints=[[.08,.18],[.17,.45],[.21,.32],[.3,.69],[.38,.41],[.43,.58],[.52,.8],[.6,.54],[.65,.75],[.76,.66],[.81,.9],[.91,.79]];
function chart(x,y,w,h,{kind='curve',progress=1,color=C.blue,points=defaultPoints,selectedIndex=-1}={}) {
  const left=x+5,top=y+4,width=w-11,height=h-10;
  let result=stroke(`M${left} ${top}v${height}h${width}`,'#c4d7ed',1);
  if(kind==='scatter'){
    points.forEach((p,index)=>{const visible=clamp(progress*points.length-index);if(!visible)return;const px=left+p[0]*width,py=top+(1-p[1])*height;result+=circle(px,py,index===selectedIndex?3:2,index===selectedIndex?color:'#9dbde9',`opacity="${f(visible)}"`);if(index===selectedIndex)result+=circle(px,py,6,'none',`stroke="${color}" stroke-width="1" opacity=".7"`)});
  }else if(kind==='bars'){
    const values=[.42,.78,.61,.88];
    values.forEach((value,index)=>{const bh=height*value*clamp(progress*1.8-index*.2);result+=`<rect x="${f(left+6+index*(width-8)/4)}" y="${f(top+height-bh)}" width="${f((width-18)/6)}" height="${f(bh)}" rx="2" fill="${index%2?C.cyan:color}" opacity=".75"/>`});
  }else{
    const d=kind==='distribution'?`M${left+2} ${top+height-1}C${left+width*.32} ${top+height-1} ${left+width*.27} ${top+2} ${left+width*.52} ${top+3}S${left+width*.74} ${top+height-3} ${left+width-1} ${top+height-1}`:`M${left+2} ${top+height*.83}C${left+width*.2} ${top+height*.78} ${left+width*.2} ${top+height*.33} ${left+width*.4} ${top+height*.45}S${left+width*.58} ${top+height*.57} ${left+width*.69} ${top+height*.25}S${left+width*.85} ${top+height*.2} ${left+width-1} ${top+height*.1}`;
    result+=stroke(d,color,1.7,`pathLength="100" stroke-dasharray="${f(clamp(progress)*100)} 101"`);
  }
  return result;
}
function dataTable(x,y,w,h,{rows=3,progress=1,selectedRows=[],mutedRows=[]}={}) {
  const rowH=h/rows;let result='';
  for(let index=0;index<rows;index++){
    const visible=clamp(progress*rows-index);if(!visible)continue;
    const selected=selectedRows.includes(index),muted=mutedRows.includes(index);
    result+=g(panel(x,y+index*rowH,w,rowH-4,{rx:5,shadow:false,fill:selected?'#edf4ff':'white',stroke:selected?'#9cbcf2':'#d9e5f4'})+line(x+10,y+index*rowH+8,x+w*.6,y+index*rowH+8,selected?'#83a9ef':'#b9cce4',2.5)+line(x+10,y+index*rowH+14,x+w*.43,y+index*rowH+14,'#d4e0ef',2),{opacity:visible*(muted?.24:1)});
  }
  return result;
}
function database(x,y,w=50,h=60) {
  const ry=Math.min(7,h*.12),bottom=y+h-ry;
  return `<path d="M${f(x)} ${f(y+ry)}V${f(bottom)}a${f(w/2)} ${f(ry)} 0 0 0 ${f(w)} 0V${f(y+ry)}" fill="white" stroke="${C.border}" stroke-width="1.2" filter="url(#shadow)"/><ellipse cx="${f(x+w/2)}" cy="${f(y+ry)}" rx="${f(w/2)}" ry="${f(ry)}" fill="#edf4ff" stroke="${C.border}"/>${stroke(`M${x} ${y+h*.4}a${w/2} ${ry} 0 0 0 ${w} 0M${x} ${y+h*.68}a${w/2} ${ry} 0 0 0 ${w} 0`,'#d4e2f5',1)}`;
}
function check(x,y,scale=1) {
  return g(circle(0,0,7,C.blue,'stroke="white" stroke-width="1.5"')+stroke('M-3.2 0-.6 2.7 4-3',C.white,1.3),{x,y,scale});
}
module.exports={C,f,clamp,at,g,panel,stroke,line,circle,atomNode,arrow,slider,molecule,chart,dataTable,database,check};
