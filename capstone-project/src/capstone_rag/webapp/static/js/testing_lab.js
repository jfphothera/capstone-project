// Testing Lab entry point: screen switching, Gateway inventory and polling.
// Screens live in ./lab/*.js; this file only wires them together.
import { request, createPoller } from './lab/api.js';
import { el, replace } from './lab/dom.js';
import { headsScreen } from './lab/heads.js';
import { runScreen, DEFAULT_SELECTION } from './lab/run.js';
import { ResultsView } from './lab/results.js';
import { CompatibilityView } from './lab/compatibility.js';

const app = document.querySelector('#lab-app');
const loading = document.querySelector('#lab-loading');
const SCREENS = ['heads', 'run', 'results', 'compatibility'];

let state = null;
let screen = 'run';
let currentExperiment = '';
const selection = structuredClone(DEFAULT_SELECTION);

// One banner for connection problems; replaced, never stacked.
const banner = el('div', { class: 'lab-message error', role: 'status', hidden: true });
function report(text) { banner.textContent = text; banner.hidden = false; }
function clearReport() { banner.hidden = true; }

const results = new ResultsView({
  getExperiment: () => currentExperiment,
  setExperiment: id => { currentExperiment = id; },
  refresh: () => resultsPoller.refresh(),
  report,
});
const resultsPoller = createPoller(() => results.load(), {
  intervalMs: 3000,
  onError: (error, failures) => report(`Results are not updating (${error.message}). Retrying automatically${failures > 1 ? ` — attempt ${failures}` : ''}.`),
  onRecover: clearReport,
});
const headsPoller = createPoller(() => loadState(false), { intervalMs: 30000 });
const compatibility = new CompatibilityView({ onStarted: id => { currentExperiment = id; show('results'); }, report });

function tabs() {
  return el('nav', { class: 'lab-tabs', 'aria-label': 'Testing Lab screens' },
    ...SCREENS.map(id => el('button', { class: screen === id ? 'active' : '', onclick: () => show(id) }, id[0].toUpperCase() + id.slice(1))));
}

function inventoryNotice() {
  if (state.inventory_status === 'ready') return null;
  const partial = state.inventory_status === 'partial';
  return el('div', { class: 'lab-message ' + (partial ? 'error' : '') },
    el('strong', {}, partial ? 'Gateway inventory incomplete' : 'Checking Gateway inventory'),
    el('p', {}, state.inventory_message || 'Waiting for the authenticated model inventory.'),
    el('button', { class: 'btn primary', onclick: () => loadState(true) }, 'Retry Gateway inventory'));
}

function render() {
  const page = screen === 'heads' ? headsScreen(state, { onRefresh: () => loadState(true) })
    : screen === 'run' ? runScreen({ state, selection, rerender: render, onStarted: id => { currentExperiment = id; show('results'); } })
    : screen === 'compatibility' ? compatibility.node
    : results.node;
  replace(app, tabs(), banner, inventoryNotice(), page);
  clearTimeout(window.capstoneLabBootWatchdog);
}

function show(name) {
  screen = name;
  headsPoller.stop();
  resultsPoller.stop();
  render();
  if (name === 'heads') headsPoller.start();
  if (name === 'results') resultsPoller.start();
  if (name === 'compatibility') compatibility.load();
}

async function loadState(force = false) {
  try {
    state = await request('/api/lab/state' + (force ? '?refresh=true' : ''));
    loading.hidden = true;
    // The Results screen is long-lived; re-rendering it would only reset scroll.
    if (screen !== 'results' && screen !== 'compatibility') render();
    if (state.inventory_status === 'checking') setTimeout(() => loadState(false), 1000);
  } catch (error) {
    if (state) { report('Could not refresh the Gateway inventory: ' + error.message); return; }
    loading.hidden = false;
    loading.className = 'lab-message error';
    replace(loading, 'The Testing Lab could not open: ' + error.message + ' ', el('button', { class: 'btn primary', onclick: () => loadState(true) }, 'Retry'));
  }
}

async function init() {
  try {
    const mode = await request('/api/lab/mode');
    if (mode.mode !== 'gateway') throw new Error('Capstone requires the Inference Gateway');
    await loadState();
  } catch (error) {
    loading.className = 'lab-message error';
    replace(loading, 'Could not open the Testing Lab: ' + error.message + ' ', el('button', { class: 'btn primary', onclick: init }, 'Retry'));
  }
}

init();
