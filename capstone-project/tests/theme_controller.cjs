/* Behavioural checks without a browser dependency or operational database. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('src/capstone_rag/webapp/static/js/theme.js', 'utf8');
function page({stored=null, dark=false, blocked=false}={}) {
  const listeners = {}, picker = {value:'', addEventListener:(name, fn) => {listeners['picker:'+name]=fn;}};
  const root = {dataset:{},style:{}}, media={matches:dark,addEventListener:(name,fn)=>{listeners['media:'+name]=fn;}};
  let saved = stored;
  const context = {
    document:{documentElement:root,readyState:'loading',querySelectorAll:()=>listeners.ready?[picker]:[],addEventListener:(name,fn)=>{listeners.ready=()=>{fn();};}},
    window:{matchMedia:()=>media,localStorage:{getItem:()=>{if(blocked)throw Error('blocked');return saved;},setItem:(key,value)=>{if(blocked)throw Error('blocked');saved=value;}},addEventListener:(name,fn)=>{listeners[name]=fn;}}
  };
  vm.runInNewContext(code,context);
  return {root,picker,ready:()=>listeners.ready(),choose:value=>{picker.value=value;listeners['picker:change']();},system:dark=>{media.matches=dark;listeners['media:change']();},storage:(value,key='capstone.theme.v1')=>listeners.storage({key,newValue:value}),saved:()=>saved};
}
const a=page({dark:true});
assert.equal(a.root.dataset.theme,'dark','system choice applies before DOM ready');
a.ready();assert.equal(a.picker.value,'system');
a.system(false);assert.equal(a.root.dataset.theme,'light');
a.choose('dark');assert.equal(a.saved(),'dark');assert.equal(a.root.style.colorScheme,'dark');
a.system(false);assert.equal(a.root.dataset.theme,'dark','manual choice overrides system');
const b=page({stored:a.saved()});b.ready();assert.equal(b.root.dataset.theme,'dark','new page persists preference');
a.choose('light');b.storage(a.saved());assert.equal(b.root.dataset.theme,'light');assert.equal(b.picker.value,'light','storage synchronizes other window picker');
b.storage('dark','unrelated');assert.equal(b.root.dataset.theme,'light');
b.storage(null);assert.equal(b.root.dataset.themePreference,'system');
assert.equal(page({stored:'invalid',dark:true}).root.dataset.theme,'dark');
const privatePage=page({blocked:true});privatePage.ready();privatePage.choose('dark');assert.equal(privatePage.root.dataset.theme,'dark');
console.log('Theme controller passed: startup, OS changes, overrides, persistence, cross-window sync, invalid/blocked storage.');
