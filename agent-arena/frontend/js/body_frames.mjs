// Arena 2.5.0: the artist's animation frames for the eight-character family (assets/deliveries/
// 2026-11-18-eight-character-animation-addon, copied into assets/characters-v2). Every frame is 64 x 80, feet at 32,78,
// files bots/<body>/<view>/<animation>-NN.svg beside the original idle.svg and walk-01..04.svg.
// [frames, frame_ms]; frame_ms 0 = a pose the room selects and holds (blink, look-aside, startled, sit).
const SIDE={talk:[4,140],listen:[2,400],blink:[1,0],startled:[1,0],tap:[2,300],'run-frantic':[4,90]};
export const BODY_ANIMS={
  front:{talk:[4,140],listen:[2,400],blink:[1,0],startled:[1,0],'look-aside':[2,0],fidget:[2,350],'run-frantic':[4,90]},
  back:{'run-frantic':[4,90]},left:SIDE,right:SIDE};
const CAT_SIDE={sit:[1,0],'tail-flick':[2,300],leap:[3,160]};
export const CAT_ANIMS={front:{sit:[1,0],'tail-flick':[2,300]},back:{},left:CAT_SIDE,right:CAT_SIDE};
// Fixed-palette props drawn over a figure (characters-v2/props/<name>-NN.svg): size, frames, frame_ms.
export const PROPS={'startle-mark':{size:[16,24],frames:3,ms:80},'speech-bubble-typing':{size:[32,24],frames:3,ms:250},
  'speech-bubble-received':{size:[32,24],frames:1,ms:0},packet:{size:[16,12],frames:2,ms:200}};
export const VIEWS=['front','back','left','right'];
const nn=i=>String(i).padStart(2,'0');
// Frame file names (no extension) for one view of a body or the cat: idle, walk-01..04, then each animation's frames.
export function viewFrames(cat,view){
  const out=['idle','walk-01','walk-02','walk-03','walk-04'];
  for(const[name,[n]]of Object.entries((cat?CAT_ANIMS:BODY_ANIMS)[view]||{}))for(let i=1;i<=n;i++)out.push(`${name}-${nn(i)}`);
  return out;
}
// Which frame of an animation shows at time t (ms) for an animation that started at t0: looping ones cycle, the
// others play once and hold their last frame.
export function frameOf(view,name,t,t0=0,cat=false){
  const a=(cat?CAT_ANIMS:BODY_ANIMS)[view]?.[name];if(!a)return null;const[n,ms]=a;
  const i=ms?Math.floor(Math.max(0,t-t0)/ms):0,loop=name==='talk'||name==='listen'||name==='tap'||name==='run-frantic'||name==='tail-flick';
  return`${name}-${nn(1+(loop?i%n:Math.min(i,n-1)))}`;
}
