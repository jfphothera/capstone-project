// Lists every asset file the live rooms can load (arena-room.html, scene_composer.mjs in live
// mode), traced from composer_rooms.mjs and the kits' own catalogs, into
// frontend/assets/room-assets.txt. The desktop build copies exactly these files; the art kits'
// sources, previews and thumbnails stay out of the package.
//   node tools/room_assets.mjs           write the manifest
//   node tools/room_assets.mjs --check   fail if the manifest is out of date or a file is missing
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {ROOMS,THEMES,BULBS,DESIGN,roomLayout,plateFiles} from '../frontend/js/composer_rooms.mjs';
import {VIEWS,PROPS,viewFrames} from '../frontend/js/body_frames.mjs';
// The ticker messages scene_composer.mjs can show (tickerMessage).
const TICKER_MESSAGES=['working','thinking','out-for-lunch','coffee-break','closed','be-right-back'];

const ASSETS=path.join(path.dirname(fileURLToPath(import.meta.url)),'..','frontend','assets');
const MANIFEST=path.join(ASSETS,'room-assets.txt');
const read=f=>JSON.parse(fs.readFileSync(path.join(ASSETS,f),'utf8'));
const files=new Set(),add=(dir,...names)=>{for(const n of names.flat())if(n)files.add(path.posix.join(dir,n))};

const doors=read('doors-v1/catalog.json'),doorRooms=read('doors-v1/room-profiles.json');
const panels=read('wall-panels-v1/catalog.json'),panelRooms=read('wall-panels-v1/room-profiles.json');
const decos=read('decorations-v1/catalog.json'),finishing=read('finishing-v1/catalog.json'),assemblies=read('finishing-v1/assemblies.json');
const sidewall=read('sidewall-v2/catalog.json'),shared=read('scenes/_shared/sprites.json');
const chars=read('characters-v1/crew/catalog.json'),agentDirs=read('characters-v1/agents/catalog.json');
const tickers=read('ticker-signs-v2/catalog.json');
add('',['doors-v1/catalog.json','doors-v1/room-profiles.json','wall-panels-v1/catalog.json','wall-panels-v1/room-profiles.json',
  'decorations-v1/catalog.json','finishing-v1/catalog.json','finishing-v1/assemblies.json','sidewall-v2/catalog.json',
  'scenes/_shared/sprites.json','characters-v1/crew/catalog.json','characters-v1/agents/catalog.json','ticker-signs-v2/catalog.json','classic-arcade/classic-refined.svg']);

for(const id of Object.keys(ROOMS)){
  const L=roomLayout(id);
  if(L.kit==='plate'){add('',plateFiles(id));continue}   // fixed-layout rooms carry their own files
  add('',[L.plate,L.scene]);
  if(L.type==='lab')add(`scenes/${id}`,read(L.scene).screens.file);
  const door=doors.items.find(i=>i.id===doorRooms.profiles.find(p=>p.scene===id).doors.a.design);
  add('doors-v1',door.portal,door.frame,door.emission,door.glow,door.animation.panel_frames);
  const set=panelRooms.profiles.find(p=>p.scene===id).placements[0].item.split('/')[0];
  for(const p of panels.items.filter(i=>i.set===set)){add('wall-panels-v1',p.body);for(const s of Object.values(p.states))add('wall-panels-v1',s.emission,s.glow)}
  const ticker=tickers.items.find(i=>i.id===L.tickerHousing);
  add('ticker-signs-v2',ticker.housing);for(const m of TICKER_MESSAGES)add('ticker-signs-v2',ticker.states[m].emission_atlas,ticker.states[m].glow_atlas);
}
for(const theme of Object.values(THEMES)){
  for(const [pool,ids] of Object.entries(theme)){
    if(pool==='wallSurface'||pool==='wallProp'){for(const id of Object.values(ids).flat())add('sidewall-v2',sidewall.items.find(i=>i.id===id).body)}
    else if(pool==='bench'||pool==='sidePieces'){for(const id of ids){const a=assemblies.assemblies.find(x=>x.id===id);
      for(const part of [a.surface,a.prop])add('finishing-v1',finishing.items.find(x=>x.id===part).body)}}
    else for(const id of ids){const d=decos.items.find(i=>i.id===id);if(!d)throw new Error('unknown decoration '+id);
      add('decorations-v1',d.animation.frames);if(d.lighting?.enabled)add('decorations-v1',d.unlit,d.animation.glow_frames)}
  }
}
// Pieces the per-room designs name directly (lamps, units, assemblies), beyond the theme pools.
const named=new Set();(function walk(v){if(typeof v==='string')named.add(v);else if(v&&typeof v==='object')Object.values(v).forEach(walk)})(DESIGN);
for(const id of named){
  const d=decos.items.find(i=>i.id===id);if(d){add('decorations-v1',d.animation.frames);if(d.lighting?.enabled)add('decorations-v1',d.unlit,d.animation.glow_frames);continue}
  const a=assemblies.assemblies.find(x=>x.id===id);if(a){for(const part of [a.surface,a.prop])add('finishing-v1',finishing.items.find(x=>x.id===part).body);continue}
  const w=sidewall.items.find(i=>i.id===id);if(w)add('sidewall-v2',w.body);
}
add('decorations-v1',decos.items.find(i=>i.id==='lights/signal-tower').unlit,decos.items.find(i=>i.id==='lights/signal-tower').animation.frames,decos.items.find(i=>i.id==='lights/signal-tower').animation.glow_frames);
for(const id of BULBS){const b=finishing.items.find(i=>i.id===id);add('finishing-v1',b.body,b.emission_mask,b.glow_mask)}
// People (characters-v1): every crew/staff frame and the bucket; the agents' directional walk SVGs.
for(const a of Object.values(chars.actors))for(const v of Object.values(a.views))for(const an of Object.values(v.animations))add('characters-v1',an.frames);
for(const pr of Object.values(chars.props))add('characters-v1',pr.png);
for(const v of agentDirs.views)add('characters-v1/agents',v.frames.map(f=>f.replace(/\.png$/,'.svg')));
// Arena 2.4.0, the eight-character family (characters-v2): the agent bodies and the robot cat, four views, idle plus
// a four-frame walk (scene_composer.mjs loads the SVGs; the admin hub draws the cat from the PNGs).
for(const b of ['a-round','b-wheel','c-hex','d-wide','e-dome','f-kite','g-tread','h-robot-cat'])for(const v of ['front','back','left','right'])
  for(const f of viewFrames(b==='h-robot-cat',v))add(`characters-v2/bots/${b}/${v}`,f+'.svg',b==='h-robot-cat'?f+'.png':null);
// Arena 2.5.0: the artist's animation frames (body_frames.mjs) and the props over the figures.
for(const[n,p]of Object.entries(PROPS))for(let i=1;i<=p.frames;i++)add('characters-v2/props',`${n}-0${i}.svg`);
add('scenes/_shared',shared.props.crates.map(c=>c.file),shared.props.debris.map(c=>c.file));
for(const s of Object.values(shared.staff))for(const a of Object.values(s.animations))add('scenes/_shared',a.frames);

const list=[...files].sort(),missing=list.filter(f=>!fs.existsSync(path.join(ASSETS,f)));
if(missing.length){console.error('missing:\n'+missing.join('\n'));process.exit(1)}
const text='# Generated by tools/room_assets.mjs: every asset the live rooms load. Do not edit.\n'+list.join('\n')+'\n';
const bytes=list.reduce((n,f)=>n+fs.statSync(path.join(ASSETS,f)).size,0);
if(process.argv.includes('--check')){
  if(!fs.existsSync(MANIFEST)||fs.readFileSync(MANIFEST,'utf8').replace(/\r\n/g,'\n')!==text){console.error('room-assets.txt is out of date: run node tools/room_assets.mjs');process.exit(1)}
  console.log(`room-assets.txt is current: ${list.length} files, ${(bytes/1048576).toFixed(1)} MB`);
}else{fs.writeFileSync(MANIFEST,text);console.log(`wrote ${list.length} files, ${(bytes/1048576).toFixed(1)} MB`)}
