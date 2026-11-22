// Admin traffic board: an LED dot-matrix board in the admin-traffic-board-v1 housing. Departures
// is work out at a head now; Arrivals is work that just came back. The rows are the console's
// /api/traffic, the lamps and the bottom marquee its /api/status. Everything on the faces is drawn
// here: the unlit dot grid, lit 5x7 letters with glow, a flip-style roll when a letter changes,
// blinking status words and the scrolling warnings line. Still mode drops roll, blink and scroll.
const ASSETS=new URL('../assets/admin-traffic-board-v1/',import.meta.url);
const PITCH=3,DOT=2,COLS=36,LINES=13,LINE_DOTS=10,GLYPH_W=6;   // 36 x 6 dots = 648 px of the 656 px face
const FRAME_MS=45,BLINK_MS=1100,SCROLL_MS=40,BASE_CHECK_MS=5000;
const UNLIT='#0e1519',DIM='#56666c',HEAD_TEXT='#f2e6c8',TIME_TEXT='#c9bfa6',GATEWAY='#b9c4ff';
const APP_COLORS=['#c792ff','#9cff6a','#ff8ad8','#7fb2ff','#ffe066'];
const STATUS={gate:['AT GATE','#ffd38a'],boarding:['BOARDING','#ffb347',true],running:['AIRBORNE','#fff1c9'],
  unknown:['UNKNOWN','#ffbe5a',true],landed:['LANDED','#7dff9a'],failed:['FAILED','#ff5a4a'],canceled:['CANCELED','#8a9aa3']};
const ROLL='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
// Row-major 5x7 glyphs, two hex digits per row, bit 4 = leftmost dot.
const FONT={A:'0E11111F111111',B:'1E11111E11111E',C:'0E11101010110E',D:'1C12111111121C',E:'1F10101E10101F',
  F:'1F10101E101010',G:'0E11101711110F',H:'1111111F111111',I:'0E04040404040E',J:'0702020202120C',K:'11121418141211',
  L:'1010101010101F',M:'111B1515111111',N:'11111915131111',O:'0E11111111110E',P:'1E11111E101010',Q:'0E11111115120D',
  R:'1E11111E141211',S:'0F10100E01011E',T:'1F040404040404',U:'1111111111110E',V:'11111111110A04',W:'1111111515150A',
  X:'11110A040A1111',Y:'1111110A040404',Z:'1F01020408101F',0:'0E11131519110E',1:'040C040404040E',2:'0E11010204081F',
  3:'1F02040201110E',4:'02060A121F0202',5:'1F101E0101110E',6:'0608101E11110E',7:'1F010204080808',8:'0E11110E11110E',
  9:'0E11110F01020C',':':'000C0C000C0C00','-':'0000001F000000','.':'00000000000C0C','/':'00010204081000',
  '(':'02040808080402',')':'08040202020408','>':'0004021F020400',',':'000000000C0408',"'":'0C040800000000',
  '+':'0004041F040400','%':'18190204081303','#':'0A0A1F0A1F0A0A','?':'0E110102040004','!':'04040404040004',
  '_':'0000000000001F','&':'0C12140815120D','=':'00001F001F0000','*':'0004150E150400','·':'0000000C0C0000',' ':'00000000000000'};
const ARROW='>';           // drawn as an arrow: the font's '>' is a full-height →

const img=src=>new Promise((ok,no)=>{const i=new Image();i.onload=()=>ok(i);i.onerror=()=>no(new Error('missing '+src));i.src=new URL(src,ASSETS)});
// Off-screen layers live in CPU memory (willReadFrequently keeps Chromium from putting them on the
// GPU): a GPU reset - display sleep, a driver hiccup - wipes GPU canvases, and the housing layer is
// drawn only once. Field defect 2026-11-04: after ~20 minutes on the hub the housing went blank and,
// with nothing repainting the board, the glass streaks and glow piled up into solid stripes.
function canvas(w,h){const c=document.createElement('canvas');c.width=w;c.height=h;const g=c.getContext('2d',{willReadFrequently:true});g.imageSmoothingEnabled=false;return [c,g]}
const rows=ch=>{const hex=FONT[ch]??FONT['?'];return Array.from({length:7},(_,i)=>parseInt(hex.slice(i*2,i*2+2),16))};
const GLYPHS=new Map(Object.keys(FONT).map(k=>[k,rows(k)]));
const glyph=ch=>GLYPHS.get(ch)||GLYPHS.get('?');
const hash=s=>[...s].reduce((h,c)=>(h*31+c.charCodeAt(0))>>>0,7);

export function appColor(appId){
  if(appId==='capstone')return '#ffb347';
  if(appId.startsWith('coder'))return '#5fe3ea';
  return APP_COLORS[hash(appId)%APP_COLORS.length];
}
// "Ticketing" -> TICKETING; "Coder (SYNTHETIC TEST)" -> TEST CODER, so test data still reads as test data.
export function appLabel(name){
  let short=String(name).split(' (')[0].toUpperCase();
  if(/TEST/i.test(name)&&!short.includes('TEST'))short='TEST '+short;
  return short.slice(0,10);
}
const hhmm=t=>new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hourCycle:'h23'});

// One face: LINES x COLS cells. A cell shows `ch`; when its target letter changes it rolls through
// a few random letters (left cells first), then settles. Colour changes apply at once.
class Face{
  constructor(x,y){this.x=x;this.y=y;this.cells=Array.from({length:LINES},()=>Array.from({length:COLS},()=>({ch:' ',color:DIM,want:' ',blink:false,roll:0,wait:0})));
    [this.layer,this.g]=canvas(COLS*GLYPH_W*PITCH,LINES*LINE_DOTS*PITCH);this.marquee=null}
  set(line,segments,{roll=true,still=false}={}){
    const want=Array.from({length:COLS},()=>({ch:' ',color:DIM,blink:false}));
    for(const s of segments)[...s.text].forEach((ch,i)=>{const c=s.col+i;if(c>=0&&c<COLS)want[c]={ch:ch.toUpperCase(),color:s.color,blink:!!s.blink}});
    want.forEach((w,c)=>{const cell=this.cells[line][c];cell.blink=w.blink;cell.next=w.color;
      if(cell.want===w.ch){if(!cell.roll&&!cell.wait)cell.color=w.color;return}
      cell.want=w.ch;
      if(!roll||still||cell.ch===' '&&w.ch===' '){cell.ch=w.ch;cell.color=w.color;cell.roll=cell.wait=0;return}
      cell.wait=Math.floor(c*.5);cell.roll=2+Math.floor(Math.random()*4)});
  }
  // A rolling letter takes its new colour from the first random letter, so a row never shows its
  // old text in the new row's colour.
  tick(){let busy=false;for(const line of this.cells)for(const cell of line){
    if(cell.wait>0){cell.wait--;busy=true}
    else if(cell.roll>0){cell.roll--;cell.color=cell.next;cell.ch=cell.roll?ROLL[Math.floor(Math.random()*ROLL.length)]:cell.want;busy=true}}
    return busy}
  draw(blinkOn){
    const g=this.g;g.clearRect(0,0,this.layer.width,this.layer.height);
    this.cells.forEach((line,l)=>{
      if(this.marquee&&l===LINES-1)return this.drawMarquee(l);
      line.forEach((cell,c)=>{if(cell.ch===' '||cell.blink&&!blinkOn&&!cell.roll)return;dots(g,glyph(cell.ch),c*GLYPH_W,l*LINE_DOTS,cell.color)})});
  }
  drawMarquee(l){
    const m=this.marquee,g=this.g;let x=-m.offset;
    while(x<COLS*GLYPH_W){for(const s of m.segments){for(const ch of s.text){
      if(x>-GLYPH_W&&x<COLS*GLYPH_W)dots(g,glyph(ch.toUpperCase()),x,l*LINE_DOTS,s.color);x+=GLYPH_W}}
      if(!m.scroll)break}
  }
}
function dots(g,bits,dx,dy,color){
  g.fillStyle=color;
  bits.forEach((row,r)=>{for(let b=0;b<5;b++)if(row&(16>>b)){const x=dx+b;if(x>=0&&x<COLS*GLYPH_W)g.fillRect(x*PITCH,(dy+r)*PITCH,DOT,DOT)}});
}

export async function createBoard(target){
  const catalog=await (await fetch(new URL('catalog.json',ASSETS))).json();
  const [housing,glass,...lampImgs]=await Promise.all([img(catalog.housing),img(catalog.glass),
    ...['green','amber','red'].map(s=>img(catalog.lamps.states[s]))]);
  const [W,H]=catalog.canvas,out=target.getContext('2d');target.width=W;target.height=H;out.imageSmoothingEnabled=false;
  // The housing with every dot unlit, drawn once - and again whenever it is found blank.
  const [base,bg]=canvas(W,H);
  const faces={};
  for(const [id,[fx,fy,fw,fh]] of Object.entries(catalog.faces)){
    const ox=fx+Math.floor((fw-COLS*GLYPH_W*PITCH)/2),oy=fy+Math.floor((fh-((LINES-1)*LINE_DOTS+7)*PITCH)/2);
    faces[id]=new Face(ox,oy);
  }
  function paintBase(){
    bg.setTransform(1,0,0,1,0,0);bg.globalAlpha=1;bg.globalCompositeOperation='source-over';bg.imageSmoothingEnabled=false;
    bg.clearRect(0,0,W,H);bg.drawImage(housing,0,0);bg.fillStyle=UNLIT;
    for(const f of Object.values(faces))for(let y=0;y<LINES*LINE_DOTS-3;y++)for(let x=0;x<COLS*GLYPH_W;x++)bg.fillRect(f.x+x*PITCH,f.y+y*PITCH,DOT,DOT);
  }
  // The housing is opaque at its centre top; a transparent pixel there means the layer was lost.
  const baseLost=()=>{try{return bg.isContextLost?.()||bg.getImageData(W>>1,4,1,1).data[3]===0}catch{return true}};
  paintBase();
  const [lit,lg]=canvas(W,H);
  let still=false,lamps=[false,false,false],dirty=true,lastFrame=0,lastScroll=0,lastBaseCheck=0,traffic=null,status=null,offline=null;

  function header(){
    const now=new Date();
    faces.departures.set(0,[{col:0,text:'DEPARTURES',color:HEAD_TEXT},{col:28,text:now.toLocaleTimeString([],{hourCycle:'h23'}),color:TIME_TEXT}],{roll:false});
    const hour=status?.hour;
    faces.arrivals.set(0,[{col:0,text:'ARRIVALS',color:HEAD_TEXT},...(hour?[{col:36-`${hour.pass} OK ${hour.fail} FAIL`.length,text:`${hour.pass} OK`,color:'#7dff9a'},
      {col:36-`${hour.fail} FAIL`.length,text:`${hour.fail} FAIL`,color:hour.fail?'#ff5a4a':DIM}]:[])],{roll:false});
    faces.departures.set(1,[{col:0,text:'TIME',color:DIM},{col:6,text:'FROM',color:DIM},{col:19,text:'TO',color:DIM},{col:28,text:'STATUS',color:DIM}],{roll:false});
    faces.arrivals.set(1,[{col:0,text:'TIME',color:DIM},{col:6,text:'FROM',color:DIM},{col:17,text:'TO',color:DIM},{col:28,text:'STATUS',color:DIM}],{roll:false});
  }
  // The gateway's own summary on Departures' last line, apart from the applications' rows: the
  // rows are what applications report, this line is what the Inference Gateway's ledger shows.
  const GATEWAY_WORDS={stale:'NO RECENT DATA',invalid:'UNREADABLE',resync:'RESYNC NEEDED',not_published:'NOT PUBLISHED'};
  function gatewayLine(){
    const gw=status?.gateway;if(!gw||offline)return;
    const o={still},line=LINES-1;
    if(gw.state!=='ok'){faces.departures.set(line,[{col:0,text:'GATEWAY',color:GATEWAY},{col:8,text:GATEWAY_WORDS[gw.state]||'UNKNOWN',color:'#ffbe5a',blink:true}],o);return}
    const words=[['GATEWAY',GATEWAY],[gw.admission_open?'OPEN':'CLOSED',gw.admission_open?'#7dff9a':'#ff5a4a'],
      ['Q',DIM],[String(gw.queued),HEAD_TEXT],['ACTIVE',DIM],[String(gw.active),HEAD_TEXT],
      [`${gw.heads_up}/${gw.heads_total}`,gw.heads_up<gw.heads_total?'#ffbe5a':HEAD_TEXT],['HEADS',DIM]];
    if(words.reduce((n,[w])=>n+w.length+1,-1)>COLS)words[0][0]='GW';
    let col=0;faces.departures.set(line,words.map(([text,color])=>{const s={col,text,color};col+=text.length+1;return s}),o);
  }
  function statusCell(code){const [word,color,blink]=STATUS[code]||STATUS.unknown;return {col:28,text:word,color,blink}}
  function rowsFor(){
    const dep=faces.departures,arr=faces.arrivals,o={still};
    const message=(face,from,text,color)=>{for(let l=from;l<(face===arr?LINES-1:LINES);l++)face.set(l,l===from?[{col:0,text,color}]:[],o)};
    if(offline){message(dep,2,offline,'#ff5a4a');message(arr,2,'',DIM);return}
    if(!traffic)return;
    const last=status?.gateway?LINES-1:LINES;                 // the gateway line takes Departures' last line
    const out=traffic.departures,cap=last-2,shown=out.length>cap?out.slice(0,cap-1):out;
    shown.forEach((t,i)=>dep.set(2+i,[{col:0,text:hhmm(t.at),color:TIME_TEXT},{col:6,text:appLabel(t.app_name),color:appColor(t.app_id)},
      {col:17,text:ARROW,color:DIM},{col:19,text:t.head_name.toUpperCase().slice(0,8),color:HEAD_TEXT},statusCell(t.status)],o));
    if(out.length>cap)dep.set(last-1,[{col:6,text:`+${out.length-shown.length} MORE OUT`,color:DIM}],o);
    for(let l=2+shown.length+(out.length>cap?1:0);l<last;l++)dep.set(l,l===2&&!out.length?[{col:0,text:'NOTHING OUT AT THE HEADS',color:DIM}]:[],o);
    gatewayLine();
    const back=traffic.arrivals.slice(-(LINES-3));
    back.forEach((t,i)=>arr.set(2+i,[{col:0,text:hhmm(t.at),color:TIME_TEXT},{col:6,text:t.head_name.toUpperCase().slice(0,8),color:HEAD_TEXT},
      {col:15,text:ARROW,color:DIM},{col:17,text:appLabel(t.app_name),color:appColor(t.app_id)},statusCell(t.status)],o));
    for(let l=2+back.length;l<LINES-1;l++)arr.set(l,l===2&&!back.length?[{col:0,text:'NOTHING BACK RECENTLY',color:DIM}]:[],o);
  }
  function marquee(){
    const issues=status?.issues||[],m=faces.arrivals.marquee||{offset:0};
    if(offline||!issues.length){
      const text=offline?'':'ALL SYSTEMS NORMAL';
      faces.arrivals.marquee={segments:[{text:' '.repeat(Math.floor((COLS-text.length)/2))+text,color:'#4f9a62'}],offset:0,scroll:false};return}
    const segments=issues.flatMap(i=>[{text:`${i.who} ${i.what}`,color:i.level==='down'?'#ff5a4a':'#ffbe5a'},{text:'   ·   ',color:DIM}]);
    const fits=segments.reduce((n,s)=>n+s.text.length,0)-7<=COLS;
    if(still||fits){const first=issues[Math.floor(Date.now()/4000)%issues.length];
      faces.arrivals.marquee={segments:[{text:`${first.who} ${first.what}`.slice(0,COLS),color:first.level==='down'?'#ff5a4a':'#ffbe5a'}],offset:0,scroll:false};return}
    const width=segments.reduce((n,s)=>n+s.text.length,0)*GLYPH_W;
    faces.arrivals.marquee={segments,offset:m.scroll?m.offset%width:-COLS*GLYPH_W,scroll:true,width};
  }
  function frame(t){
    requestAnimationFrame(frame);
    if(t-lastFrame<FRAME_MS)return;
    let busy=false;
    for(const f of Object.values(faces))busy=f.tick()||busy;
    const m=faces.arrivals.marquee;
    if(m?.scroll&&t-lastScroll>=SCROLL_MS){m.offset=(m.offset+Math.max(1,Math.round((t-lastScroll)/SCROLL_MS)))%m.width;if(m.offset<-COLS*GLYPH_W)m.offset=0;lastScroll=t;busy=true}
    const blinking=!still&&Object.values(faces).some(f=>f.cells.some(l=>l.some(c=>c.blink)));
    if(!busy&&!dirty&&!blinking)return;
    lastFrame=t;dirty=false;
    const blinkOn=still||t%BLINK_MS<BLINK_MS*.62;
    lg.clearRect(0,0,W,H);
    for(const f of Object.values(faces)){f.draw(blinkOn);lg.drawImage(f.layer,f.x,f.y)}
    // Every frame starts from a clean canvas with default state, so nothing can accumulate even if
    // a layer is lost; a context reset also restores imageSmoothingEnabled to true.
    if(t-lastBaseCheck>BASE_CHECK_MS){lastBaseCheck=t;if(baseLost())paintBase()}
    out.setTransform(1,0,0,1,0,0);out.imageSmoothingEnabled=false;
    out.globalAlpha=1;out.globalCompositeOperation='source-over';out.filter='none';out.clearRect(0,0,W,H);out.drawImage(base,0,0);
    out.globalCompositeOperation='lighter';out.filter='blur(3px)';out.globalAlpha=.7;out.drawImage(lit,0,0);
    out.filter='none';out.globalAlpha=1;out.globalCompositeOperation='source-over';out.drawImage(lit,0,0);
    out.drawImage(glass,0,0);
    lamps.forEach((on,i)=>{if(on)out.drawImage(lampImgs[i],...catalog.lamps.top_left[i])});
  }
  // Chromium fires contextrestored after a GPU reset: repaint everything at once.
  for(const c of [target,base])c.addEventListener('contextrestored',()=>{paintBase();dirty=true;lastBaseCheck=0});
  setInterval(()=>{header();dirty=true},1000);
  header();marquee();requestAnimationFrame(frame);
  return {
    traffic(data){traffic=data;offline=null;rowsFor();dirty=true},
    status(s){status=s;const down=!s.service.up;lamps=[!down,s.issues.some(i=>i.level!=='down'),down];header();marquee();rowsFor();dirty=true},
    offline(text){offline=String(text).toUpperCase().slice(0,COLS);lamps=[false,false,true];rowsFor();marquee();dirty=true},
    setStill(on){still=!!on;marquee();dirty=true},
  };
}
