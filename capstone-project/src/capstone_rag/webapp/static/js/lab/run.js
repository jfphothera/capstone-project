// "Run" screen: choose an experiment, models, heads and traffic, then start.
import { request, TIMEOUTS } from './api.js';
import { el, headNumber } from './dom.js';

export const DEFAULT_SELECTION = {
  experiment: 'agentic-testing',
  reviewer_model: '',
  tool_strategies: {},
  agentic_limits: {},
  use_team: false,
  models: ['qwen3.5:4b'],
  heads: [],
  repetitions: 1,
  capacity_calls: 32,
  concurrency: 1,
  stair_step: false,
  stair_maximum: 8,
  step_calls: 32,
  step_minutes: 5,
  step_basis: 'calls',
  stair_stop_rule: 'maximum',
  continue_after_failures: true,
  case_count: 1,
  scenario_ids: [],
  review_mode: 'automated',
  // Agentic Testing starts with one cell (current baseline A + treatment 1);
  // the legacy matrix card selects all 32 when chosen.
  methodology_matrix: { topology_ids: ['A'], treatment_ids: ['1'] },
  screening_roles: [],
  role_models: {},
  role_budgets: {},
  team_base_model: '',
};

// Model-backed roles (deterministic workers are not screened).
export const MODEL_ROLES = ['ticket_interpretation', 'erp_research', 'qms_research', 'investigation', 'resolution_planning',
  'customer_communication', 'communication_review', 'independent_validation', 'supervisor', 'peer_reviewer', 'reflection'];
const roleLabel = role => role.replace(/_/g, ' ');

const clamp = (value, low, high, fallback) => Math.max(low, Math.min(high, Number(value) || fallback));

// rc.25 hotfix: every selection change rebuilds the whole Run screen
// (rerender), so each <details> panel came back closed and inputs that are not
// part of `selection` came back at their defaults. Choosing a tool strategy
// inside "Advanced: reviewer, tool strategy, limits" therefore closed it on
// every choice. Panel open state and the advanced model settings now live
// here, outside the rebuilt DOM, for the life of the page.
const panelOpen = new Map();
function panel(key, attrs, summary, ...children) {
  return el('details', {
    ...attrs, 'data-panel': key, open: attrs.open || panelOpen.get(key),
    ontoggle: event => panelOpen.set(key, event.target.open),
  }, el('summary', {}, summary), ...children);
}
const runParameters = { timeout: '600', seed: '7', temperature: '0', context: '8192' };
const keepParameter = key => event => { runParameters[key] = event.target.value; };

function checkbox(value, checked, onchange, label, disabled = false) {
  return el('label', { class: 'choice' }, el('input', { type: 'checkbox', value, checked, onchange, disabled }), el('span', {}, label));
}

// rc.25 (U1): advisory recommendation state is presentation only. A gray
// option is a normal, enabled checkbox (mouse and keyboard), stays in the
// submitted configuration and runs if its profile is valid. Text badges carry
// the meaning as well as colour.
function advisoryChoice(item, checked, onchange, label) {
  const advisory = item.advisory || {};
  const gray = advisory.appearance === 'gray';
  const node = checkbox(item.id, checked, onchange, label);
  node.classList.add(gray ? 'advisory-gray' : 'advisory-normal');
  const badges = [];
  if (gray) badges.push('discouraged (advisory)');
  if (item.implementation_qualified === false) badges.push('not implementation-qualified');
  if (badges.length) node.querySelector('span').append(el('small', { class: 'advisory-badge' }, ' · ' + badges.join(' · ')));
  const reason = [advisory.reason, item.implementation_note].filter(Boolean).join(' — ');
  if (reason) node.title = reason;
  node.dataset.advisory = gray ? 'gray' : 'normal';
  return node;
}

function modelChoice(model, checked, onchange, label) {
  const view = model.presentation || {};
  const node = checkbox(model.name, checked, onchange, label);
  node.classList.add(view.appearance === 'gray' ? 'advisory-gray' : view.appearance === 'green' ? 'model-tested' : 'advisory-normal');
  node.dataset.advisory = view.appearance || 'normal';
  if ((view.reasons || []).length) node.title = view.reasons.join(' | ') + (view.note ? ` (${view.note})` : '');
  return node;
}

function setting(label, input, help) {
  input.title = help;
  return el('label', { class: 'setting', title: help }, el('span', {}, label), input, el('small', { class: 'control-help' }, help));
}

function numberInput(value, min, max, onValue, extra = {}) {
  return el('input', { type: 'number', min: String(min), max: String(max), value: String(value), onchange: event => onValue(event.target.value), ...extra });
}

function select(options, value, onValue) {
  const node = el('select', { onchange: event => onValue(event.target.value) }, ...options.map(([key, label]) => el('option', { value: key }, label)));
  node.value = value;
  return node;
}

// Pre-fill the evidence-based default team; models not served right now are
// left on the base model and listed so nothing is silently substituted.
export function applyTeamPreset(state, selection) {
  const preset = state.team_preset;
  if (!preset) return [];
  const served = new Set(state.models.map(m => m.name));
  const missing = [];
  const keep = model => (served.has(model) ? model : (missing.push(model), ''));
  selection.role_models = Object.fromEntries(Object.entries(preset.role_models).map(([role, model]) => [role, keep(model)]));
  selection.team_base_model = keep(preset.team_base_model);
  selection.role_budgets = structuredClone(preset.role_budgets);
  const baselines = preset.baselines.filter(m => served.has(m));
  if (baselines.length) selection.models = baselines;
  selection.team_preset_missing = [...new Set(missing)];
  return selection.team_preset_missing;
}

function experimentCard(state, item, selection, rerender) {
  const input = el('input', {
    type: 'radio', name: 'experiment', value: item.id, checked: selection.experiment === item.id,
    onchange: () => {
      selection.experiment = item.id;
      selection.scenario_ids = [];
      if (item.id === 'capacity-pressure') selection.concurrency = 6;
      if (item.id === 'agentic-evaluation') selection.methodology_matrix = { topology_ids: ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'], treatment_ids: ['1', '2', '3', '4'] };
      if (item.id === 'team-comparison' && !Object.values(selection.role_models).some(Boolean)) applyTeamPreset(state, selection);
      if (item.id === 'agentic-testing') {
        selection.scenario_ids = [...(state.agentic?.presets?.['vertical-slice'] || [])];
        selection.methodology_matrix = { topology_ids: ['A'], treatment_ids: ['1'] };
      }
      rerender();
    },
  });
  const summary = item.id === 'model-compatibility' ? `${item.scenario_ids.length} tasks across 11 model-backed roles, per model`
    : item.id === 'team-comparison' ? 'Team + each baseline model on the same cases'
    : item.id === 'agentic-evaluation' ? '32 methodology runs per selected case, model and repetition'
    : item.id === 'capacity-pressure' ? 'One fixed, review-free call per trial · 32 calls selected initially · parallel flows controls calls kept active'
    : `${item.scenario_ids.length} case${item.scenario_ids.length === 1 ? '' : 's'} · about ${item.calls} model calls · about ${item.minutes} min on a CPU head`;
  return el('label', { class: 'experiment-card' }, input, el('div', {}, el('h3', {}, item.name), el('p', {}, item.description), el('small', {}, summary)));
}

function agenticBox(state, selection, rerender, updatePlan) {
  const matrix = selection.methodology_matrix;
  const toggle = (group, id, checked) => {
    if (checked) matrix[group] = [...new Set([...matrix[group], id])];
    else if (matrix[group].length > 1) matrix[group] = matrix[group].filter(value => value !== id);
    rerender();
  };
  const count = numberInput(selection.case_count, 1, state.scenarios.length, value => { selection.case_count = clamp(value, 1, state.scenarios.length, 1); updatePlan(); });
  const review = select([
    ['automated', 'Automated synthetic reviewer — recommended'],
    ['autonomous_sandbox', 'Autonomous sandbox decisions'],
    ['interactive', 'Human review'],
  ], selection.review_mode, value => { selection.review_mode = value; updatePlan(); });
  const cases = state.scenarios.map(scenario => checkbox(scenario.id, selection.scenario_ids.includes(scenario.id), event => {
    selection.scenario_ids = event.target.checked ? [...selection.scenario_ids, scenario.id] : selection.scenario_ids.filter(id => id !== scenario.id);
    selection.case_count = Math.max(1, selection.scenario_ids.length || selection.case_count);
    rerender();
  }, `${scenario.name} · ${scenario.category}`));
  return el('section', { class: 'run-box methodology-box' },
    el('h3', {}, 'Agentic Evaluation matrix'),
    el('p', {}, 'Every selected case runs through each selected topology × reasoning/retrieval treatment. The same seed and model configuration are paired across cells.'),
    el('div', { class: 'advanced-grid' },
      setting('Number of cases', count, 'How many synthetic support cases are paired across every selected methodology.'),
      setting('Review mode', review, 'Automated completes synthetic approvals deterministically; autonomous sandbox permits bounded synthetic decisions; human review pauses for you.')),
    checkbox('continue', selection.continue_after_failures, event => { selection.continue_after_failures = event.target.checked; updatePlan(); },
      'Attempt every methodology even if individual trials fail — recommended for comparisons'),
    el('p', { class: 'context-meta' }, 'Leave the individual cases unchecked for a reproducible random sample. Check cases to choose them directly.'),
    el('div', { class: 'choice-grid' }, ...cases),
    panel('matrix-topologies', {}, 'Topologies A–H', el('div', { class: 'choice-grid' },
      ...state.methodologies.topologies.map(t => advisoryChoice(t, matrix.topology_ids.includes(t.id), event => toggle('topology_ids', t.id, event.target.checked), `${t.id} · ${t.name} · ${t.communication}`)))),
    panel('matrix-treatments', {}, 'Reasoning / retrieval treatments 1–4', el('div', { class: 'choice-grid' },
      ...state.methodologies.treatments.map(t => advisoryChoice(t, matrix.treatment_ids.includes(t.id), event => toggle('treatment_ids', t.id, event.target.checked), `${t.id} · ${t.name}`)))),
    el('p', { class: 'context-meta' }, 'Gray = discouraged by the recorded evidence (advisory only); every option stays selectable and runs.'));
}

function capacityBox(selection, rerender, updatePlan) {
  const calls = numberInput(selection.capacity_calls, 1, 10000, value => { selection.capacity_calls = clamp(value, 1, 10000, 32); updatePlan(); });
  const maximum = numberInput(selection.stair_maximum, 1, 1024, value => { selection.stair_maximum = clamp(value, 1, 1024, 8); updatePlan(); });
  const stepCalls = numberInput(selection.step_calls, 1, 10000, value => { selection.step_calls = clamp(value, 1, 10000, 32); updatePlan(); });
  const stepMinutes = numberInput(selection.step_minutes, 1, 1440, value => { selection.step_minutes = clamp(value, 1, 1440, 5); updatePlan(); });
  const basis = select([['calls', 'Calls per step'], ['minutes', 'Minutes per step']], selection.step_basis, value => { selection.step_basis = value; updatePlan(); });
  const stopRule = select([['maximum', 'Run every step'], ['first_failure', 'Stop at first failure'], ['diminishing_returns', 'Stop at diminishing returns']],
    selection.stair_stop_rule, value => { selection.stair_stop_rule = value; updatePlan(); });
  return el('section', { class: 'run-box methodology-box' },
    el('h3', {}, 'Sustained capacity traffic'),
    el('p', {}, 'Each isolated trial sends the same fixed prompt and bounded output with no review stage. Completed calls are immediately replaced up to the selected limit.'),
    setting('Total model calls', calls, 'Used when Stair-step is off.'),
    checkbox('stair', selection.stair_step, event => { selection.stair_step = event.target.checked; rerender(); }, 'Stair-step from 1 call at a time to a chosen maximum'),
    el('div', { class: 'advanced-grid' },
      setting('Maximum', maximum, 'Highest simultaneous call target, from 1 through 1024.'),
      setting('Step length', basis, 'Measure each step by a call count or by elapsed minutes.'),
      setting('Calls per step', stepCalls, 'At least this many calls are measured; high steps automatically send enough calls to fill the step.'),
      setting('Minutes per step', stepMinutes, 'Duration target when Minutes per step is selected.'),
      setting('Stop rule', stopRule, 'Run through the maximum, stop on any failure, or stop after two consecutive throughput gains below 10%.')));
}

function screeningBox(selection, rerender, updatePlan) {
  const roles = selection.screening_roles;
  return el('section', { class: 'run-box methodology-box' },
    el('h3', {}, 'Model compatibility check'),
    el('p', {}, 'Each selected model runs 3 frozen tasks per selected role through that role’s production prompt. Every attempt is recorded permanently; the Compatibility tab shows passed / failed / inconclusive / not tested per model and role. Leave roles unchecked to screen all of them.'),
    el('div', { class: 'choice-grid' }, ...MODEL_ROLES.map(role => checkbox(role, roles.includes(role), event => {
      selection.screening_roles = event.target.checked ? [...roles, role] : roles.filter(r => r !== role);
      rerender();
    }, roleLabel(role)))),
    el('p', { class: 'context-meta' }, 'Choose the models to screen in step 2. The model list is frozen into the campaign when it starts.'));
}

function teamBox(state, selection, rerender, updatePlan, { includeCases = true } = {}) {
  const models = state.models.map(m => m.name);
  const status = el('p', { class: 'context-meta' });
  const seed = el('button', { class: 'btn' }, 'Fill from compatibility results');
  seed.onclick = async () => {
    seed.disabled = true;
    try {
      const value = await request('/api/lab/screening/team-seed');
      selection.role_models = { ...value.role_models };
      rerender();
    } catch (failure) {
      status.textContent = 'Could not read compatibility results: ' + failure.message;
    } finally { seed.disabled = false; }
  };
  const budget = (role, key, low, high) => numberInput(selection.role_budgets[role]?.[key] ?? '', low, high, value => {
    const current = { ...(selection.role_budgets[role] || {}) };
    if (value === '' || value == null) delete current[key]; else current[key] = clamp(value, low, high, low);
    selection.role_budgets = { ...selection.role_budgets, [role]: current };
    updatePlan();
  }, { placeholder: 'default' });
  const rows = MODEL_ROLES.map(role => el('div', { class: 'team-row' },
    el('span', {}, roleLabel(role)),
    select([['', '— base model —'], ...models.map(m => [m, m])], selection.role_models[role] || '', value => {
      selection.role_models = { ...selection.role_models, [role]: value };
      updatePlan();
    }),
    budget(role, 'num_ctx', 512, 65536), budget(role, 'num_predict', 16, 8192)));
  const base = select([['', '— choose —'], ...models.map(m => [m, m])], selection.team_base_model, value => { selection.team_base_model = value; updatePlan(); });
  const cases = state.scenarios.map(scenario => checkbox(scenario.id, selection.scenario_ids.includes(scenario.id), event => {
    selection.scenario_ids = event.target.checked ? [...selection.scenario_ids, scenario.id] : selection.scenario_ids.filter(id => id !== scenario.id);
    rerender();
  }, `${scenario.name} · ${scenario.category}`));
  const preset = el('button', { class: 'btn' }, 'Use evidence-based defaults');
  preset.onclick = () => { applyTeamPreset(state, selection); rerender(); };
  const missing = selection.team_preset_missing?.length
    ? el('p', { class: 'lab-message error' }, `Not served right now, left on the base model: ${selection.team_preset_missing.join(', ')}`) : null;
  return el('section', { class: 'run-box methodology-box' },
    el('h3', {}, 'Team comparison'),
    el('p', {}, 'The team (one model per role, optional per-role context and output budgets) and each baseline model chosen in step 2 run the same cases. Every model call records the model requested for its role and the model that executed it; a mismatch makes the run not count.'),
    state.team_preset ? el('p', { class: 'context-meta' }, `Pre-filled: ${state.team_preset.name}. ${state.team_preset.note} Source: ${state.team_preset.evidence}. The run measures the time cost against both uniform baselines.`) : null,
    missing,
    el('div', { class: 'result-actions' }, preset, seed), status,
    setting('Base model for unassigned roles', base, 'Roles left on “base model” use this model in the team.'),
    el('div', { class: 'team-row header' }, el('span', {}, 'Role'), el('span', {}, 'Model'), el('span', {}, 'Context (num_ctx)'), el('span', {}, 'Output (num_predict)')),
    ...rows,
    includeCases ? el('h4', {}, 'Cases (paired across team and baselines)') : null,
    includeCases ? el('p', { class: 'context-meta' }, 'Leave unchecked to use the five standard cases.') : null,
    includeCases ? el('div', { class: 'choice-grid' }, ...cases) : null);
}

function safetyBox(state, selection, rerender) {
  const cases = state.scenarios.filter(s => s.id.startsWith('safety-'));
  return el('section', { class: 'run-box methodology-box' },
    el('h3', {}, 'Safety evaluation cases'),
    el('p', {}, 'Each case states its expected safe outcome. Results show the outcome verdict and, separately, any unsafe proposal a guardrail blocked. QMS conflict cases run on an isolated copy of the QMS documents. Leave all unchecked to run every case.'),
    el('div', { class: 'choice-grid' }, ...cases.map(scenario => checkbox(scenario.id, selection.scenario_ids.includes(scenario.id), event => {
      selection.scenario_ids = event.target.checked ? [...selection.scenario_ids, scenario.id] : selection.scenario_ids.filter(id => id !== scenario.id);
      rerender();
    }, scenario.name.replace('Safety 6.1 · ', '')))));
}

const PRIMARY = ['agentic-testing'];

// --- Agentic Testing: cases -> models or teams -> methodology -> review -> run ---
function agenticCases(state, selection, rerender) {
  const catalog = state.agentic || { scenarios: [], templates: {}, presets: {} };
  const chosen = new Set(selection.scenario_ids);
  const toggle = (id, on) => { selection.scenario_ids = on ? [...new Set([...selection.scenario_ids, id])] : selection.scenario_ids.filter(x => x !== id); rerender(); };
  const preset = name => el('button', { class: 'btn small', onclick: () => { selection.scenario_ids = [...(catalog.presets[name] || [])]; rerender(); } }, name.replace('-', ' '));
  const rows = Object.entries(catalog.templates).map(([template, spec]) => el('div', { class: 'agentic-case-row' },
    el('strong', {}, spec.title),
    el('div', { class: 'agentic-behaviours' }, ...spec.behaviours.map(b => {
      const id = `agentic:${template}:${b}`;
      return checkbox(id, chosen.has(id), event => toggle(id, event.target.checked), b.replace(/_/g, ' '));
    }))));
  return el('section', { class: 'run-box' },
    el('h3', {}, '1. Select cases'),
    el('p', {}, 'Each case is a ticket type paired with a customer behaviour. The ticket enters through the real intake; the simulated customer answers whatever the agents ask, from its own private facts.'),
    el('div', { class: 'result-actions' }, el('span', {}, 'Presets:'), ...Object.keys(catalog.presets).map(preset),
      el('button', { class: 'btn small', onclick: () => { selection.scenario_ids = catalog.scenarios.map(s => s.id); rerender(); } }, 'everything'),
      el('span', { class: 'context-meta' }, `${selection.scenario_ids.length} selected · scenario set ${catalog.version || ''}`)),
    ...rows);
}

function agenticMethodology(state, selection, rerender) {
  const matrix = selection.methodology_matrix;
  const toggle = (group, id, on) => {
    matrix[group] = on ? [...new Set([...matrix[group], id])] : (matrix[group].length > 1 ? matrix[group].filter(v => v !== id) : matrix[group]);
    rerender();
  };
  return el('section', { class: 'run-box' },
    el('h3', {}, '3. Select methodology'),
    el('p', { class: 'context-meta' }, 'Agent arrangement × reasoning/retrieval treatment. The campaign review below shows which roles each cell actually exercises.'),
    el('div', { class: 'choice-grid' }, ...state.methodologies.topologies.map(t => advisoryChoice(t, matrix.topology_ids.includes(t.id), e => toggle('topology_ids', t.id, e.target.checked), `${t.id} · ${t.name}`))),
    el('div', { class: 'choice-grid' }, ...state.methodologies.treatments.map(t => advisoryChoice(t, matrix.treatment_ids.includes(t.id), e => toggle('treatment_ids', t.id, e.target.checked), `${t.id} · ${t.name}`))),
    el('p', { class: 'context-meta' }, 'Gray = discouraged by the recorded evidence (advisory only); every option stays selectable and runs.'));
}

function agenticAdvanced(state, selection, rerender, updatePlan) {
  const models = state.models.map(m => m.name);
  const reviewer = select([['', `Discovered: ${state.reviewer?.identifier || 'Qwen3 4B not found'}`], ...models.map(m => [m, m])], selection.reviewer_model,
    value => { selection.reviewer_model = value; updatePlan(); });
  const limit = (key, low, high, fallback) => numberInput(selection.agentic_limits[key] ?? fallback, low, high, value => {
    selection.agentic_limits = { ...selection.agentic_limits, [key]: clamp(value, low, high, fallback) }; updatePlan();
  });
  const strategies = selection.models.map(model => {
    const resolved = (state.models.find(m => m.name === model) || {}).tool_strategy_default || {};
    // Show exactly what will be submitted: an unresolved (incompatible) model
    // shows a placeholder until the user chooses, never a silent default.
    const current = selection.tool_strategies[model] || { strategy: resolved.strategy || '', delegate_model: resolved.delegate_model || '' };
    // Delegates: only models with confirmed native tool calling, never the model itself.
    const delegates = state.models.filter(m => m.native_tools === true && m.name !== model).map(m => m.name);
    const set = patch => {
      const next = { ...current, ...patch };
      // Never delegated-without-delegate: fall back to the resolved default, then the first tool-capable model.
      if (next.strategy === 'delegated' && !next.delegate_model) next.delegate_model = resolved.delegate_model || delegates[0] || '';
      selection.tool_strategies = { ...selection.tool_strategies, [model]: next };
      rerender();
    };
    return el('div', { class: 'team-row' }, el('span', {}, model),
      select([...(current.strategy ? [] : [['', '— choose a strategy (native unsupported) —']]), ['native', 'Native tool calling'], ['mediated', 'Mediated structured selection'], ['delegated', 'Delegate to a tool-capable model']], current.strategy, v => set({ strategy: v })),
      current.strategy === 'delegated' ? select([['', '— delegate model —'], ...delegates.map(m => [m, m])], current.delegate_model || '', v => set({ delegate_model: v })) : el('span'),
      el('small', { class: 'context-meta' }, resolved.incompatible ? 'native default incompatible: explicit choice required' : (resolved.basis || '')));
  });
  return panel('agentic-advanced', { class: 'advanced' }, 'Advanced: reviewer, tool strategy, limits',
    setting('Reviewer model', reviewer, 'The simulated human reviewer (separate agent). Kept fixed while comparing ticket-system configurations; changing it is a separate reviewer experiment.'),
    el('h4', {}, 'Tool selection strategy per model (investigation role)'),
    el('p', { class: 'context-meta' }, 'Declared and recorded; never switched silently. Models that cannot call tools need mediated or delegated selection.'),
    ...strategies,
    el('div', { class: 'advanced-grid' },
      setting('Max customer turns', limit('max_customer_turns', 0, 20, 6), 'Customer replies per run.'),
      setting('Max reviewer decisions', limit('max_reviewer_decisions', 1, 60, 16), 'Reviewer decisions per run.'),
      setting('Max workflow cycles', limit('max_cycles', 1, 60, 14), 'Coordinator cycles per run.'),
      setting('Max minutes per run', limit('max_minutes', 1, 55, 25), 'Wall-clock limit. Reaching any limit is recorded as incomplete, never as success.')));
}

function agenticReview(plan) {
  const a = plan?.agentic;
  if (!a) return el('div');
  const probes = el('div', { class: 'result-table' }, el('div', { class: 'team-row header' }, el('span', {}, 'Model'), el('span', {}, 'Gateway tool probe'), el('span', {}, 'Strategy that will launch'), el('span', {}, 'Delegate')),
    ...a.tool_probes.map(p => el('div', { class: 'team-row' }, el('span', {}, p.model), el('span', {}, p.label),
      el('span', { title: (p.resolution || {}).reason || (p.resolution || {}).basis || '' }, p.declared_strategy), el('span', {}, p.delegate_model || '—'))));
  return el('section', { class: 'run-box' }, el('h3', {}, '4. Review campaign'),
    el('p', {}, `Reviewer agent: ${a.reviewer.model || 'none'} (policy ${a.reviewer.policy}, fixed). Limits: ${Object.entries(a.limits).map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(' · ')}.`),
    a.team ? el('p', {}, `Team: ${Object.entries(a.team).map(([r, m]) => `${r.replace(/_/g, ' ')}=${m}`).join(', ')}; other roles ${a.team_base_model || 'profile default'}.`) : null,
    el('h4', {}, 'Roles each methodology cell exercises'),
    ...a.cells.map(c => el('p', { class: 'context-meta' }, `${c.id}: ${c.stage_roles_active.join(', ') || 'specialist roles only'}${c.stage_roles_not_exercised.length ? ' · not exercised: ' + c.stage_roles_not_exercised.join(', ') : ''}${(c.stage_coverage || []).length ? ' · coverage: ' + c.stage_coverage.map(x => `${x.stage} ${x.status.replace(/_/g, ' ')}`).join(', ') : ''}`)),
    el('h4', {}, 'Tool calling (a probe informs planning; complete runs decide)'), probes,
    el('p', { class: 'context-meta' }, a.context_note));
}

function modelsBox(state, selection, rerender) {
  const box = el('section', { class: 'run-box' }, el('h3', {}, '2. Which models'), el('p', {}, 'Choose more than one to compare them on the same cases.'));
  for (const group of ['small', 'medium', 'large']) {
    const rows = state.models.filter(model => model.group === group);
    if (!rows.length) continue;
    box.append(el('h4', {}, group[0].toUpperCase() + group.slice(1)), el('div', { class: 'choice-grid' },
      ...rows.map(model => {
        const node = modelChoice(model, selection.models.includes(model.name), event => {
          selection.models = event.target.checked ? [...selection.models, model.name] : selection.models.filter(name => name !== model.name);
          if (!selection.models.length) selection.models = [model.name];
          rerender();
        }, `${model.name} · heads ${model.heads.map(headNumber).join(', ')}`);
        const tags = [...((model.presentation || {}).badges || []),
          model.native_tools === false ? 'no native tools' : model.native_tools ? 'native tools' : 'tools unknown'].filter(Boolean);
        node.querySelector('span').append(el('small', { class: 'model-tags' }, ' · ' + tags.join(' · ')));
        // What will happen to this model's tool calls by default.
        const tools = model.tool_strategy_default || {};
        const toolsLine = tools.strategy === 'native' ? 'tools: native'
          : tools.strategy === 'delegated' ? `tools: delegated to ${tools.delegate_model || '—'}`
          : tools.incompatible ? 'tools: choose a strategy' : '';
        if (toolsLine) node.querySelector('span').append(el('small', { class: 'model-tools' }, toolsLine));
        if (model.note || model.tools_source) node.title = [node.title, model.note, model.tools_source && `tools source: ${model.tools_source}`].filter(Boolean).join(' | ');
        return node;
      })));
  }
  if (state.excluded_models?.length) {
    box.append(panel('excluded-models', { class: 'excluded-models' }, `Not offered by Capstone (${state.excluded_models.length})`,
      ...state.excluded_models.map(m => el('p', { class: 'context-meta' }, el('strong', {}, m.name), ' — ', m.reason))));
  }
  if (state.unavailable_models?.length) {
    box.append(panel('unavailable-models', { class: 'unavailable-models' }, `Installed but unavailable now (${state.unavailable_models.length})`,
      ...state.unavailable_models.map(m => el('p', { class: 'context-meta' }, el('strong', {}, m.name), ' — installed; ',
        Object.entries(m.reasons || {}).map(([head, reason]) => `${headNumber(head)}: ${reason}`).join(' · ') || 'no head is available'))));
  }
  box.append(el('p', { class: 'context-meta' }, 'Green = tested for this role (not safe, approved or best). Gray = discouraged by scoped evidence (advisory; still selectable). Untested models keep the normal look.'));
  return box;
}

function headsBox(state, selection, rerender) {
  const serving = state.heads.filter(head => head.serving);
  const common = serving.filter(head => selection.models.every(name => state.models.find(model => model.name === name)?.heads.includes(head.resource_id)));
  return el('section', { class: 'run-box' },
    el('h3', {}, '3. Where'),
    checkbox('any', selection.heads.length === 0, () => { selection.heads = []; rerender(); }, 'Any head — let the Gateway choose'),
    el('p', { class: 'or' }, 'Or pin separate runs to these heads:'),
    el('div', { class: 'choice-grid' }, ...serving.map(head => checkbox(head.resource_id, selection.heads.includes(head.resource_id), event => {
      selection.heads = event.target.checked ? [...selection.heads, head.resource_id] : selection.heads.filter(id => id !== head.resource_id);
      rerender();
    }, `Head ${head.number} · ${head.address}`, !common.includes(head)))));
}

function planText(plan, selection) {
  const eligibility = Object.entries(plan.eligible_heads_by_model || {})
    .map(([model, heads]) => `${model}: heads ${heads.map(headNumber).join(', ') || 'none'}`).join(' · ');
  const where = selection.heads.length ? 'Pinned separately to heads ' + selection.heads.map(headNumber).join(', ') : 'The Gateway chooses among eligible heads';
  if (plan.capacity_pressure) {
    const steps = plan.stair_steps?.length ? ` across ${plan.stair_steps.length} stair steps` : '';
    return `${plan.runs} fixed model calls${steps}, up to ${plan.parallel_flows} active. ${plan.expected_wait?.message || ''} ${where}. Eligible now — ${eligibility}.`;
  }
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  if (selection.experiment === 'agentic-testing') {
    return `${plural(plan.runs, 'complete ticket run', 'complete ticket runs')} = ${plural(plan.cases, 'case', 'cases')} × ${plural(plan.models, 'configuration', 'configurations')} × `
      + `${plural(plan.methodologies, 'methodology cell', 'methodology cells')} × ${plural(plan.repetitions, 'repetition', 'repetitions')}. Up to ${plan.parallel_flows} at once. ${where}.`;
  }
  if (selection.experiment === 'model-compatibility') {
    return `${plural(plan.runs, 'run', 'runs')} = ${plural(plan.cases, 'role task', 'role tasks')} × ${plural(plan.models, 'model', 'models')} × ${plural(plan.repetitions, 'repetition', 'repetitions')}. `
      + `Up to ${plan.parallel_flows} active at once. ${where}. Eligible now — ${eligibility}.`;
  }
  if (selection.experiment === 'team-comparison') {
    return `${plural(plan.runs, 'run', 'runs')} = ${plural(plan.cases, 'case', 'cases')} × (the team + ${plural(plan.models - 1, 'baseline model', 'baseline models')}) × ${plural(plan.repetitions, 'repetition', 'repetitions')}, paired on the same cases. `
      + `Up to ${plan.parallel_flows} active at once. Cases: ${plan.selected_scenarios.join(', ')}. ${where}.`;
  }
  return `${plural(plan.runs, 'run', 'runs')} = ${plural(plan.cases, 'case', 'cases')} × ${plural(plan.methodologies, 'methodology', 'methodologies')} × `
    + `${plural(plan.models, 'model', 'models')} × ${plural(plan.formula.heads, 'head policy', 'head policies')} × ${plural(plan.repetitions, 'repetition', 'repetitions')}. `
    + `Up to ${plan.parallel_flows} active at once; approximately ${plural(plan.waves, 'wave', 'waves')}. Cases: ${plan.selected_scenarios.join(', ')}. ${where}. Eligible now — ${eligibility}.`;
}

export function runScreen({ state, selection, rerender, onStarted }) {
  const host = el('section', { class: 'lab-screen run-screen' },
    el('div', { class: 'screen-heading' }, el('div', {}, el('h2', {}, 'Run an experiment'), el('p', {}, 'Everything needed for a working test is already selected.'))));
  const planLine = el('p', { id: 'plan-line', class: 'plan-line' }, 'Calculating the plan…');
  const review = el('div', { id: 'agentic-review' });
  const error = el('div', { id: 'run-error', class: 'lab-message error', hidden: true });
  let planSequence = 0;
  let planTimer = null;

  // Debounced; a slower earlier response can never overwrite a newer one.
  function updatePlan() {
    clearTimeout(planTimer);
    planTimer = setTimeout(async () => {
      const sequence = ++planSequence;
      try {
        const plan = await request('/api/lab/plan', { method: 'POST', body: selection });
        if (sequence === planSequence) {
          planLine.textContent = planText(plan, selection);
          review.replaceChildren(agenticReview(plan));
        }
      } catch (failure) {
        if (sequence === planSequence) { planLine.textContent = failure.message; review.replaceChildren(); }
      }
    }, 150);
  }

  const timeout = el('input', { id: 'timeout', type: 'number', min: '30', max: '3600', value: runParameters.timeout, oninput: keepParameter('timeout') });
  const seed = el('input', { id: 'seed', type: 'number', value: runParameters.seed, oninput: keepParameter('seed') });
  const temperature = el('input', { id: 'temperature', type: 'number', min: '0', max: '2', step: '.1', value: runParameters.temperature, oninput: keepParameter('temperature') });
  const context = el('input', { id: 'context', type: 'number', min: '1024', max: '32768', step: '1024', value: runParameters.context, oninput: keepParameter('context') });
  const repeat = numberInput(selection.repetitions, 1, 100, value => { selection.repetitions = clamp(value, 1, 100, 1); updatePlan(); });
  const parallel = numberInput(selection.concurrency, 1, 1024, value => { selection.concurrency = clamp(value, 1, 1024, 1); updatePlan(); });

  const start = el('button', { class: 'btn primary start-button' }, 'Start');
  start.onclick = async () => {
    error.hidden = true;
    start.disabled = true;
    start.textContent = 'Starting…';
    const body = {
      ...selection,
      timeout_seconds: Number(timeout.value || 600),
      seed: Number(seed.value || 7),
      parameters: { temperature: Number(temperature.value || 0), num_ctx: Number(context.value || 8192) },
    };
    try {
      const plan = await request('/api/lab/plan', { method: 'POST', body });
      if (plan.confirmation_required && !confirm(`This will make ${plan.runs} isolated runs with up to ${plan.parallel_flows} active at once. Start them?`)) return;
      const started = await request('/api/lab/start', { method: 'POST', body, timeoutMs: TIMEOUTS.start });
      onStarted(started.id);
    } catch (failure) {
      error.textContent = 'Could not start: ' + failure.message;
      error.hidden = false;
    } finally {
      start.disabled = false;
      start.textContent = 'Start';
    }
  };

  const agentic = selection.experiment === 'agentic-testing';
  if (agentic && !selection.scenario_ids.length && state.agentic?.presets) selection.scenario_ids = [...state.agentic.presets['vertical-slice']];
  const teamToggle = agentic ? el('section', { class: 'run-box' },
    checkbox('team', selection.use_team, event => {
      selection.use_team = event.target.checked;
      if (selection.use_team && !Object.values(selection.role_models).some(Boolean)) applyTeamPreset(state, selection);
      if (!selection.use_team) selection.role_models = {};
      rerender();
    }, 'Also run a team: a model per role or stage (each team stage is verified against the model that executed it)'),
    selection.use_team ? teamBox(state, selection, rerender, updatePlan, { includeCases: false }) : null) : null;
  host.append(...[
    el('section', { class: 'run-box' }, el('h3', {}, agentic ? 'Agentic Testing' : '1. What to evaluate'),
      el('div', { class: 'experiment-grid' }, ...state.catalog.filter(item => PRIMARY.includes(item.id)).map(item => experimentCard(state, item, selection, rerender))),
      panel('other-lab-tools', { class: 'advanced', open: !PRIMARY.includes(selection.experiment) }, 'Other lab tools (capacity test; legacy test types kept for earlier campaigns)',
        el('div', { class: 'experiment-grid' }, ...state.catalog.filter(item => !PRIMARY.includes(item.id)).map(item => experimentCard(state, item, selection, rerender))))),
    agentic ? agenticCases(state, selection, rerender) : null,
    agentic ? modelsBox(state, selection, rerender) : null,
    teamToggle,
    agentic ? agenticMethodology(state, selection, rerender) : null,
    agentic ? review : null,
    agentic ? agenticAdvanced(state, selection, rerender, updatePlan) : null,
    selection.experiment === 'safety-6-1' ? safetyBox(state, selection, rerender) : null,
    selection.experiment === 'agentic-evaluation' ? agenticBox(state, selection, rerender, updatePlan) : null,
    selection.experiment === 'capacity-pressure' ? capacityBox(selection, rerender, updatePlan) : null,
    selection.experiment === 'model-compatibility' ? screeningBox(selection, rerender, updatePlan) : null,
    selection.experiment === 'team-comparison' ? teamBox(state, selection, rerender, updatePlan) : null,
    agentic ? null : modelsBox(state, selection, rerender),
    headsBox(state, selection, rerender),
    el('section', { class: 'run-box' }, el('h3', {}, '4. Repetitions and parallel traffic'), el('div', { class: 'advanced-grid' },
      setting('Repetitions', repeat, 'Repeat each selected case and methodology with a recorded seed.'),
      setting('Parallel flows', parallel, 'Maximum requests Capstone keeps active, from 1 through 1024. The Gateway remains authoritative and queues excess work.'))),
    panel('advanced-model-settings', { class: 'advanced' }, 'Advanced model settings', el('div', { class: 'advanced-grid' },
      setting('Temperature', temperature, 'Controls response variation. Zero is reproducible and recommended for comparisons.'),
      setting('Context', context, 'Maximum prompt context requested from the Gateway. Larger values consume more head memory and must be qualified.'),
      setting('Admission deadline seconds', timeout, 'Covers Gateway queue wait plus generation. Admission timeout is reported separately from Gateway and model errors.'),
      setting('Seed', seed, 'Recorded deterministic seed used to pair methodology comparisons.'))),
    planLine, error, start].filter(Boolean));
  updatePlan();
  return host;
}
