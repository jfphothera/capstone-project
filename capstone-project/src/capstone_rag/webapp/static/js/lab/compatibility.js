// "Compatibility" screen: model x role matrix from recorded screening attempts.
// Every cell is derived from immutable attempts; click a cell for the evidence.
import { request, TIMEOUTS } from './api.js';
import { el, replace, message } from './dom.js';

const STATUS_LABEL = {
  passed: 'Passed', failed: 'Failed', inconclusive_blocked: 'Inconclusive / blocked', not_tested: 'Not tested',
};
const ROLE_LABEL = role => role.replace(/_/g, ' ');
const ms = value => (value == null ? '—' : value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`);
const DIAGNOSABLE = new Set(['context_overflow', 'insufficient_output_budget', 'invalid_structured_output']);

export class CompatibilityView {
  constructor({ onStarted, report }) {
    this.onStarted = onStarted;
    this.report = report;
    this.node = el('section', { class: 'lab-screen compat-screen' });
    this.detail = el('div', { class: 'compat-detail' });
    this.inventory = null;
    this.cells = [];
  }

  async load() {
    replace(this.node, message('Loading compatibility results…'));
    try {
      const [inventory, matrix] = await Promise.all([
        request('/api/lab/screening/inventory'), request('/api/lab/screening/matrix')]);
      this.inventory = inventory;
      this.cells = matrix.cells;
      this.render();
    } catch (failure) {
      replace(this.node, message('Could not load compatibility results: ' + failure.message, 'error'),
        el('button', { class: 'btn primary', onclick: () => this.load() }, 'Retry'));
    }
  }

  render() {
    const models = [...new Set(this.cells.map(c => c.model))];
    const roles = this.inventory.model_roles.map(r => r.role);
    const heading = el('div', { class: 'screen-heading' },
      el('div', {}, el('h2', {}, 'Model compatibility'),
        el('p', {}, `Each cell: can this model do this role? ${this.inventory.model_roles[0]?.tasks.length || 3} frozen tasks per role (normal, tool/evidence, boundary), task set ${this.inventory.task_set_version}. Start a check from Run → Model compatibility check.`)),
      el('div', { class: 'result-actions' },
        el('a', { class: 'btn', href: '/api/lab/screening/export.csv?kind=summary' }, 'Export summary CSV'),
        el('a', { class: 'btn', href: '/api/lab/screening/export.csv?kind=attempts' }, 'Export attempts CSV'),
        el('button', { class: 'btn', onclick: () => this.load() }, 'Refresh')));
    if (!models.length) {
      replace(this.node, heading, message('No screening evidence recorded yet. Run a Model compatibility check to fill this matrix.'), this.inventoryBox());
      return;
    }
    const table = el('table', { class: 'compat-matrix' },
      el('thead', {}, el('tr', {}, el('th', {}, 'Model'), ...roles.map(r => el('th', { title: r }, ROLE_LABEL(r))))),
      el('tbody', {}, ...models.map(model => el('tr', {}, el('th', { scope: 'row' }, model),
        ...roles.map(role => this.cell(this.cells.find(c => c.model === model && c.role === role)))))));
    const legend = el('p', { class: 'compat-legend' }, ...Object.entries(STATUS_LABEL).map(([k, v]) => el('span', { class: 'compat-chip ' + k }, v)),
      el('span', { class: 'compat-chip stale' }, 'Stale evidence (prompt, task or model changed)'));
    replace(this.node, heading, legend, el('div', { class: 'compat-scroll' }, table), this.detail, this.inventoryBox());
  }

  cell(cell) {
    if (!cell) return el('td', { class: 'compat-cell not_tested' }, '—');
    const passed = Object.values(cell.tasks).filter(t => t.status === 'passed').length;
    const label = cell.status === 'not_tested' && cell.stale_attempts ? 'stale' : `${passed}/${cell.tasks_required}`;
    return el('td', {
      class: `compat-cell ${cell.status}${cell.stale_attempts && !cell.attempts ? ' stale' : ''}`,
      title: `${STATUS_LABEL[cell.status]} · ${cell.attempts} current attempts${cell.stale_attempts ? ` · ${cell.stale_attempts} stale` : ''}`,
    }, el('button', { class: 'compat-open', onclick: () => this.open(cell) }, label,
      cell.median_passed_ms ? el('small', {}, ms(cell.median_passed_ms)) : null,
      cell.routing_unverified ? el('small', { class: 'warn' }, 'routing ?') : null));
  }

  async open(cell) {
    replace(this.detail, message(`Loading ${cell.model} · ${ROLE_LABEL(cell.role)}…`));
    try {
      const { attempts } = await request(`/api/lab/screening/attempts?model=${encodeURIComponent(cell.model)}&role=${encodeURIComponent(cell.role)}`);
      const tasks = this.inventory.model_roles.find(r => r.role === cell.role)?.tasks || [];
      replace(this.detail, el('section', { class: 'run-box' },
        el('h3', {}, `${cell.model} · ${ROLE_LABEL(cell.role)}: ${STATUS_LABEL[cell.status]}`),
        el('p', { class: 'context-meta' }, `Contract ${cell.contract_hash.slice(0, 12)} · ${cell.attempts} current attempts · ${cell.stale_attempts} stale · median passing time ${ms(cell.median_passed_ms)}${cell.required_num_ctx ? ` · passed at num_ctx ${cell.required_num_ctx}` : ''}`),
        el('div', { class: 'compat-tasks' }, ...tasks.map(task => {
          const summary = cell.tasks[task.id] || { status: 'not_tested', attempts: 0, failure_classes: [] };
          return el('div', { class: 'compat-task ' + summary.status },
            el('strong', {}, `${task.id} · ${task.kind}`), el('span', {}, task.title),
            el('small', {}, `${STATUS_LABEL[summary.status]} · ${summary.attempts} attempt(s)${summary.failure_classes.length ? ' · ' + summary.failure_classes.join(', ') : ''}`));
        })),
        el('h4', {}, 'Attempts (newest last; stale ones included and marked)'),
        el('div', { class: 'compat-attempts' }, ...attempts.map(a => this.attempt(a, cell)))));
      this.detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (failure) {
      replace(this.detail, message('Could not load attempts: ' + failure.message, 'error'));
    }
  }

  attempt(a, cell) {
    const stale = a.contract_hash !== cell.contract_hash;
    const checks = Object.entries(a.payload.checks || {});
    const diagnose = DIAGNOSABLE.has(a.failure_class)
      ? el('button', { class: 'btn', onclick: event => this.diagnose(a, event.target) }, 'Re-run with larger budget') : null;
    return el('details', { class: 'compat-attempt ' + a.outcome },
      el('summary', {}, `${a.task_id} · ${a.outcome}${a.failure_class ? ' · ' + a.failure_class : ''} · ${ms(a.duration_ms)} · ctx ${a.num_ctx ?? 'default'} / out ${a.num_predict ?? 'default'}${stale ? ' · STALE' : ''}${a.diagnoses_attempt_id ? ' · diagnostic for ' + a.diagnoses_attempt_id : ''}`),
      el('p', {}, `Routing: requested ${a.model}, executed ${JSON.parse(a.executed_models || '[]').join(', ') || 'none recorded'} · ${a.routing_verified === 1 ? 'verified' : a.routing_verified === 0 ? 'NOT verified (does not count)' : 'no calls'}`),
      checks.length ? el('ul', {}, ...checks.map(([name, ok]) => el('li', { class: ok ? 'ok' : 'bad' }, `${ok ? '✓' : '✗'} ${name.replace(/_/g, ' ')}`))) : null,
      a.payload.error ? el('p', { class: 'bad' }, 'Error: ' + a.payload.error) : null,
      el('p', { class: 'context-meta' }, `Budget: input ≈${a.payload.budget?.max_input_tokens_estimate ?? '?'} tokens (estimate), prompt_eval ${a.payload.budget?.max_prompt_eval_count ?? '?'}, done ${JSON.stringify(a.payload.budget?.done_reasons || [])}, truncated calls ${a.payload.truncated_calls}${a.payload.mechanism ? ' · tool mechanism ' + a.payload.mechanism : ''}`),
      el('details', {}, el('summary', {}, 'Model output'), el('pre', {}, typeof a.payload.output === 'string' ? a.payload.output : JSON.stringify(a.payload.output, null, 2))),
      diagnose);
  }

  async diagnose(attempt, button) {
    button.disabled = true;
    try {
      const spec = await request('/api/lab/screening/diagnose', { method: 'POST', body: { attempt_id: attempt.attempt_id } });
      if (!confirm(spec.note + '\n\nStart this one diagnostic run?')) return;
      const started = await request('/api/lab/start', {
        method: 'POST', timeoutMs: TIMEOUTS.start,
        body: { experiment: 'model-compatibility', models: spec.gateway_models, screening_task_ids: spec.screening_task_ids,
          role_budgets: spec.role_budgets, diagnoses_attempt_id: spec.diagnoses_attempt_id, repetitions: 1, concurrency: 1 },
      });
      this.onStarted(started.id);
    } catch (failure) {
      this.report('Diagnostic could not start: ' + failure.message);
    } finally {
      button.disabled = false;
    }
  }

  inventoryBox() {
    return el('details', { class: 'advanced' }, el('summary', {}, 'Role inventory and screening tasks'),
      el('div', { class: 'choice-grid' }, ...this.inventory.model_roles.map(r => el('div', { class: 'run-box' },
        el('strong', {}, ROLE_LABEL(r.role)), el('small', { class: 'context-meta' }, ` contract ${r.contract_hash}`),
        el('ul', {}, ...r.tasks.map(t => el('li', {}, `${t.id} v${t.version} · ${t.kind} · ${t.title}`)))))),
      el('h4', {}, 'Deterministic workers (no model; not screened)'),
      el('ul', {}, ...this.inventory.deterministic_workers.map(w => el('li', {}, `${w.name}: ${w.implementation}`))));
  }
}
