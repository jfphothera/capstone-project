// The robot cat's tour (Arena 2.4.0): one room at a time, the same on every screen without any message.
// Every viewer computes the cat's room from the wall clock (heads keep within a second of the hub's clock), so
// the cat shows up in one room, leaves, and turns up in the next. The order is a fresh shuffle each day.
import {ROOMS,PLATES} from './composer_rooms.mjs';

export const ADMIN_HUB='admin-hub';
export const TOUR_ROOMS=[...Object.keys(PLATES),...Object.keys(ROOMS),ADMIN_HUB];
export const SLOT_MS=150000;          // one stop every 2.5 minutes
export const ENTER_MS=6000;           // walks in a few seconds into its slot...
export const LEAVE_MS=SLOT_MS-14000;  // ...and is out of the door before the next room's slot begins

function shuffle(list,seed){
  const a=[...list];let s=seed>>>0||1;
  const r=()=>{s^=s<<13;s>>>=0;s^=s>>>17;s^=s<<5;s>>>=0;return s/4294967296};
  for(let i=a.length-1;i>0;i--){const j=Math.floor(r()*(i+1));[a[i],a[j]]=[a[j],a[i]]}
  return a;
}

// {room, slotStart, enterAt, leaveAt} for the slot containing `now` (ms since the epoch).
export function catStop(now=Date.now(),rooms=TOUR_ROOMS){
  const slot=Math.floor(now/SLOT_MS),day=Math.floor(slot*SLOT_MS/86400000);
  const order=shuffle(rooms,day*2654435761+17);
  const slotStart=slot*SLOT_MS;
  return {room:order[slot%order.length],slotStart,enterAt:slotStart+ENTER_MS,leaveAt:slotStart+LEAVE_MS};
}

// True while the cat should be (or be on its way) in this room.
export const catHere=(room,now=Date.now())=>{const s=catStop(now);return s.room===room&&now>=s.enterAt&&now<s.leaveAt};

// The next time the cat visits this room (for previews and the ?cat=here test switch).
export function nextVisit(room,now=Date.now()){
  for(let t=now;t<now+86400000*2;t+=SLOT_MS){const s=catStop(t);if(s.room===room&&s.leaveAt>now)return s}
  return null;
}
