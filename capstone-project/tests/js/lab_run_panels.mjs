// Regression test (rc.25 hotfix): controls inside Run-screen panels must not
// close the panel. Every selection change rebuilds the Run screen (rerender);
// the tool-strategy panel used to come back closed on every choice.
//
// Drives the real static/js/lab/run.js against a minimal DOM stand-in (no
// browser, no Gateway). Usage: node lab_run_panels.mjs [path-to-lab-dir]
// Prints "OK <n> checks" and exits 0, or prints the failed check and exits 1.
import { pathToFileURL, fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const labDir = process.argv[2] || path.join(here, '..', '..', 'src', 'capstone_rag', 'webapp', 'static', 'js', 'lab');

// ---------------------------------------------------------------- DOM stand-in
class Text { constructor(text) { this.nodeType = 3; this.data = String(text); this.parentNode = null; } get textContent() { return this.data; } }

class Element {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this.childNodes = []; this.parentNode = null;
    this.attributes = {}; this.className = ''; this.dataset = {}; this.title = '';
    this.checked = false; this.disabled = false; this.hidden = false; this._open = false; this._value = undefined;
    const self = this;
    this.classList = {
      add: (...names) => { self.className = [...new Set([...self.className.split(/\s+/).filter(Boolean), ...names])].join(' '); },
      contains: name => self.className.split(/\s+/).includes(name),
    };
  }
  // <details>: like a browser, a change of `open` queues a toggle event.
  get open() { return this._open; }
  set open(value) {
    const next = Boolean(value);
    if (next === this._open) return;
    this._open = next;
    queueMicrotask(() => this.ontoggle && this.ontoggle({ type: 'toggle', target: this }));
  }
  // <select>/<input> value.
  get value() {
    if (this.tagName === 'SELECT') {
      const options = this.querySelectorAll('option');
      if (this._value !== undefined) return this._value;
      return options.length ? options[0].getAttribute('value') : '';
    }
    if (this._value !== undefined) return this._value;
    return this.attributes.value ?? '';
  }
  set value(value) {
    const text = String(value);
    if (this.tagName === 'SELECT') this._value = this.querySelectorAll('option').some(o => o.getAttribute('value') === text) ? text : '';
    else this._value = text;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
  }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === 'string' ? new Text(node) : node;
      if (child.parentNode) child.parentNode.childNodes = child.parentNode.childNodes.filter(c => c !== child);
      child.parentNode = this; this.childNodes.push(child);
    }
  }
  replaceChildren(...nodes) { for (const c of this.childNodes) c.parentNode = null; this.childNodes = []; this.append(...nodes); }
  get children() { return this.childNodes.filter(c => c.nodeType === 1); }
  get textContent() { return this.childNodes.map(c => c.textContent).join(''); }
  set textContent(value) { this.replaceChildren(new Text(value)); }
  *descendants() { for (const c of this.children) { yield c; yield* c.descendants(); } }
  matches(compound) {
    const m = compound.match(/^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[\w-]+="?[^\]"]*"?\])*)$/i);
    if (!m) throw new Error('unsupported selector ' + compound);
    if (m[1] && this.tagName !== m[1].toUpperCase()) return false;
    for (const cls of m[2].split('.').filter(Boolean)) if (!this.classList.contains(cls)) return false;
    for (const [, name, value] of m[3].matchAll(/\[([\w-]+)="?([^\]"]*)"?\]/g)) if (this.getAttribute(name) !== value) return false;
    return true;
  }
  querySelectorAll(selector) {
    const parts = selector.trim().split(/\s+/);
    let scope = [this];
    for (const part of parts) scope = [...new Set(scope.flatMap(s => [...s.descendants()].filter(d => d.matches(part))))];
    return scope;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => new Text(text), hidden: false };
const planBodies = [];
globalThis.fetch = async (url, options = {}) => {
  if (options.body) planBodies.push(JSON.parse(options.body));
  return { ok: true, status: 200, json: async () => ({ runs: 1, cases: 1, models: 1, methodologies: 1, repetitions: 1, parallel_flows: 1 }) };
};

// ---------------------------------------------------------------- fixture state
const heads = ['head-192-0-2-3'];
const state = {
  catalog: [
    { id: 'agentic-testing', name: 'Agentic Testing', description: 'd', scenario_ids: ['agentic:power-fault:cooperative'], calls: 10, minutes: 5 },
    { id: 'agentic-evaluation', name: 'Agentic Evaluation', description: 'd', scenario_ids: ['s1'], calls: 10, minutes: 5 },
    { id: 'capacity-pressure', name: 'Capacity', description: 'd', scenario_ids: [], calls: 32, minutes: 5 },
  ],
  agentic: { version: 'fixture', templates: { 'power-fault': { title: 'Power fault', behaviours: ['cooperative', 'vague'] } },
    presets: { 'vertical-slice': ['agentic:power-fault:cooperative'] },
    scenarios: [{ id: 'agentic:power-fault:cooperative' }, { id: 'agentic:power-fault:vague' }] },
  models: [
    { name: 'qwen3.5:4b', group: 'small', heads, presentation: { appearance: 'green', badges: [] }, native_tools: true, tool_strategy_default: { strategy: 'native', basis: 'gateway probe' } },
    { name: 'gemma3:4b', group: 'small', heads, presentation: { appearance: 'gray', reasons: ['scoped evidence'] }, native_tools: false, tools_source: 'gateway probe',
      tool_strategy_default: { strategy: 'delegated', delegate_model: 'qwen3.5:4b', default: true, basis: 'default: no native tool calling; tool calls go to qwen3.5:4b' } },
    { name: 'qwen3:4b', group: 'small', heads, presentation: {}, native_tools: true, tool_strategy_default: { strategy: 'native' } },
    { name: 'mistral:7b', group: 'medium', heads, presentation: {}, native_tools: null, tool_strategy_default: { incompatible: true } },
  ],
  methodologies: {
    topologies: [{ id: 'A', name: 'Baseline', communication: 'pipeline', advisory: {} }, { id: 'B', name: 'Supervisor', communication: 'hub', advisory: { appearance: 'gray', reason: 'r' } }],
    treatments: [{ id: '1', name: 'Direct', advisory: {} }, { id: '2', name: 'Reflect', advisory: {} }],
  },
  heads: [{ resource_id: heads[0], serving: true, number: 3, address: '192.0.2.3' }],
  scenarios: [{ id: 's1', name: 'Case one', category: 'power' }, { id: 's2', name: 'Case two', category: 'qms' }],
  reviewer: { identifier: 'qwen3:4b' },
  excluded_models: [{ name: 'tiny:1b', reason: 'too small' }],
  unavailable_models: [{ name: 'big:70b', reasons: { 'head-192-0-2-4': 'not loaded' } }],
  team_preset: null,
};

// ---------------------------------------------------------------- harness
const { runScreen, DEFAULT_SELECTION } = await import(pathToFileURL(path.join(labDir, 'run.js')).href);
const selection = structuredClone(DEFAULT_SELECTION);
selection.models = ['qwen3.5:4b', 'gemma3:4b', 'qwen3:4b', 'mistral:7b'];
let host = null;
let renders = 0;
// Same contract as testing_lab.js render(): rebuild the whole Run screen.
const rerender = () => { renders += 1; host = runScreen({ state, selection, rerender, onStarted: () => {} }); };
rerender();

let checks = 0;
function check(condition, label) {
  checks += 1;
  if (!condition) { console.log('FAIL: ' + label); process.exit(1); }
}
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
const panelNode = key => host.querySelector(`details[data-panel="${key}"]`);
async function openPanel(key) { panelNode(key).open = true; await settle(); }   // the user clicks the summary
async function closePanel(key) { panelNode(key).open = false; await settle(); }
async function change(node, value) {           // the user picks an option / ticks a box
  if (node.getAttribute('type') === 'checkbox' || node.getAttribute('type') === 'radio') node.checked = value;
  else node.value = value;
  node.onchange({ type: 'change', target: node });
  await settle();
}
function strategyRow(model) {
  return panelNode('agentic-advanced').querySelectorAll('div.team-row').find(row => row.children[0].textContent === model);
}
const rowSelects = model => strategyRow(model).querySelectorAll('select');
const modelBox = name => host.querySelectorAll('label.choice input').find(i => i.getAttribute('value') === name);

// 0. Model picker shows what will happen to each model's tool calls.
const toolsLine = name => modelBox(name).parentNode.querySelector('small.model-tools')?.textContent;
check(toolsLine('qwen3.5:4b') === 'tools: native', 'picker: native model says "tools: native"');
check(toolsLine('gemma3:4b') === 'tools: delegated to qwen3.5:4b', 'picker: gemma says "tools: delegated to qwen3.5:4b"');
check(toolsLine('mistral:7b') === 'tools: choose a strategy', 'picker: unresolved model says "tools: choose a strategy"');
check(modelBox('gemma3:4b').parentNode.title.includes('tools source: gateway probe'), 'picker: tools_source in the tooltip');

// 1. The tool-strategy panel stays open through every control inside it.
check(panelNode('agentic-advanced') && !panelNode('agentic-advanced').open, 'advanced panel exists and starts closed');
await openPanel('agentic-advanced');
check(panelNode('agentic-advanced').open, 'advanced panel opens');

// Resolved default delegated: delegate pre-selected; only native-capable models offered.
check(rowSelects('gemma3:4b')[0].value === 'delegated' && rowSelects('gemma3:4b')[1].value === 'qwen3.5:4b', 'gemma row starts delegated to qwen3.5:4b');
const delegateOptions = rowSelects('gemma3:4b')[1].querySelectorAll('option').map(o => o.getAttribute('value')).filter(Boolean);
check(JSON.stringify(delegateOptions) === JSON.stringify(['qwen3.5:4b', 'qwen3:4b']), 'delegate list = native_tools===true models, not the model itself: ' + delegateOptions);

let before = renders;
await change(rowSelects('qwen3.5:4b')[0], 'delegated');
check(renders > before, 'choosing a strategy rebuilds the screen (the path that used to close the panel)');
check(panelNode('agentic-advanced').open, 'panel stays open after choosing "delegated"');
check(selection.tool_strategies['qwen3.5:4b'].strategy === 'delegated', 'delegated strategy kept in the selection');
check(selection.tool_strategies['qwen3.5:4b'].delegate_model, 'delegated never submitted without a delegate');
check(rowSelects('qwen3.5:4b')[0].value === 'delegated', 'strategy select shows "delegated" after rebuild');
check(rowSelects('qwen3.5:4b').length === 2, 'delegate model select appears');
check(!rowSelects('qwen3.5:4b')[1].querySelectorAll('option').some(o => o.getAttribute('value') === 'qwen3.5:4b'), 'a model is never its own delegate');

await change(rowSelects('qwen3.5:4b')[1], 'qwen3:4b');
check(panelNode('agentic-advanced').open, 'panel stays open after picking the delegate model');
check(selection.tool_strategies['qwen3.5:4b'].delegate_model === 'qwen3:4b', 'delegate model kept in the selection');
check(rowSelects('qwen3.5:4b')[1].value === 'qwen3:4b', 'delegate select shows the chosen model after rebuild');

// Unresolved model: placeholder, then an explicit choice.
check(rowSelects('mistral:7b')[0].value === '', 'unresolved model shows the placeholder');
await change(rowSelects('mistral:7b')[0], 'delegated');
check(selection.tool_strategies['mistral:7b'].delegate_model === 'qwen3.5:4b', 'unresolved model choosing delegated gets a delegate');
await change(rowSelects('gemma3:4b')[0], 'mediated');
check(panelNode('agentic-advanced').open, 'panel stays open after choosing "mediated" for gemma');
check(rowSelects('gemma3:4b')[0].value === 'mediated', 'mediated kept for gemma');

await change(rowSelects('qwen3.5:4b')[0], 'native');
check(panelNode('agentic-advanced').open, 'panel stays open after switching back to native');
check(rowSelects('qwen3.5:4b').length === 1, 'delegate select hidden for native');
check(selection.tool_strategies['gemma3:4b'].strategy === 'mediated', 'other models keep their strategy');

// Reviewer and limits (these only refresh the plan) and a click anywhere on the panel.
const advanced = panelNode('agentic-advanced');
await change(advanced.querySelector('label.setting select'), 'qwen3:4b');
check(panelNode('agentic-advanced') === advanced && advanced.open, 'reviewer change leaves the panel open');
check(selection.reviewer_model === 'qwen3:4b', 'reviewer kept');
const limits = advanced.querySelectorAll('input');
for (const [index, input] of limits.entries()) {
  input.value = String(3 + index);
  input.onchange({ type: 'change', target: input });
}
await settle();
check(panelNode('agentic-advanced').open, 'limit changes leave the panel open');
check(selection.agentic_limits.max_customer_turns === 3, 'limit kept');

// 2. A rebuild triggered from elsewhere on the screen keeps the panel open.
await change(modelBox('qwen3:4b'), false);
check(panelNode('agentic-advanced').open, 'panel stays open when a model elsewhere is toggled');
check(!strategyRow('qwen3:4b') && strategyRow('gemma3:4b'), 'strategy rows follow the model selection');
rerender();
await settle();
check(panelNode('agentic-advanced').open, 'panel stays open across an inventory refresh re-render');

// 3. The user can still close it, and it stays closed.
await closePanel('agentic-advanced');
rerender();
await settle();
check(!panelNode('agentic-advanced').open, 'a panel the user closed stays closed after a rebuild');

// 4. Advanced model settings: panel and typed values survive a rebuild.
await openPanel('advanced-model-settings');
const typed = { temperature: '0.7', context: '16384', timeout: '900', seed: '19' };
for (const [id, value] of Object.entries(typed)) {
  const input = host.querySelector(`input[id="${id}"]`);
  input.value = value;
  if (input.oninput) input.oninput({ type: 'input', target: input });
}
await change(modelBox('qwen3:4b'), true);
check(panelNode('advanced-model-settings').open, 'advanced model settings panel stays open');
for (const [id, value] of Object.entries(typed)) check(host.querySelector(`input[id="${id}"]`).value === value, `${id} keeps ${value} after a rebuild`);

// 5. Model list panels.
await openPanel('excluded-models');
await openPanel('unavailable-models');
await change(modelBox('qwen3:4b'), false);
check(panelNode('excluded-models').open && panelNode('unavailable-models').open, 'model list panels stay open');

// 6. Legacy Agentic Evaluation: topology / treatment panels stay open while ticking.
const evaluation = host.querySelectorAll('input').find(i => i.getAttribute('name') === 'experiment' && i.getAttribute('value') === 'agentic-evaluation');
await openPanel('other-lab-tools');
await change(evaluation, true);
check(selection.experiment === 'agentic-evaluation', 'agentic evaluation selected');
check(panelNode('other-lab-tools').open, 'other lab tools panel stays open');
await openPanel('matrix-topologies');
const topologyB = () => panelNode('matrix-topologies').querySelectorAll('input').find(i => i.getAttribute('value') === 'B');
await change(topologyB(), false);
check(panelNode('matrix-topologies').open, 'topology panel stays open after unticking B');
check(!selection.methodology_matrix.topology_ids.includes('B') && !topologyB().checked, 'topology B stays unticked');
await openPanel('matrix-treatments');
const treatment2 = () => panelNode('matrix-treatments').querySelectorAll('input').find(i => i.getAttribute('value') === '2');
await change(treatment2(), false);
check(panelNode('matrix-treatments').open && panelNode('matrix-topologies').open, 'both matrix panels stay open');
check(!treatment2().checked, 'treatment 2 stays unticked');

// 7. The plan request carries the kept selection.
await new Promise(resolve => setTimeout(resolve, 250));
const last = planBodies.at(-1);
check(last && last.tool_strategies['gemma3:4b'].strategy === 'mediated', 'plan request submits the chosen strategies');

console.log(`OK ${checks} checks`);
