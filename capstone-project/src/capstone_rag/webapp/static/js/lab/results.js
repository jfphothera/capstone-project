// "Results" screen. Built once, then updated in place on every poll:
//   * counters and progress change text only;
//   * tables are rebuilt only when their content signature changes;
//   * review panels are keyed by run id, so open panels and in-flight
//     buttons survive refreshes;
//   * one banner shows the latest connection problem instead of stacking.
import { request, TIMEOUTS } from './api.js';
import { el, replace, setText, message, headNumber } from './dom.js';

const STATE_LABEL = { paused: 'Action required', not_run: 'Not run' };
const seconds = ms => (ms ? Math.round(ms / 1000) + ' s' : '—');
const signature = value => JSON.stringify(value);

function section(builder) {
  // A container that rebuilds its children only when the signature changes.
  const node = el('div');
  let last = null;
  return {
    node,
    update(value) {
      const next = signature(value);
      if (next === last) return;
      last = next;
      replace(node, builder(value));
    },
  };
}

function runsTable(runs) {
  const table = el('div', { class: 'result-table' },
    el('div', { class: 'result-row header' }, 'Method', 'Case', 'Model / head', 'State', 'Calls', 'Time / outcome'));
  for (const run of runs) {
    const where = headNumber(run.head) || headNumber(run.requested_head) || 'Gateway';
    const flags = [
      run.proposal_verdict === 'unsafe proposal blocked' ? '⚠ unsafe proposal blocked' : null,
      run.proposal_verdict === 'unsafe proposal reached the customer' ? '✗ unsafe proposal reached customer' : null,
      run.routing_verified === false ? 'routing not verified' : null,
      run.failure_class ? run.failure_class.replaceAll('_', ' ') : null,
    ].filter(Boolean).join(' · ');
    const detail = (run.error || `${run.duration_ms ? seconds(run.duration_ms) + ' · ' : ''}${run.outcome || run.status}`) + (flags ? ' · ' + flags : '');
    table.append(el('div', { class: 'result-row', 'data-status': run.status },
      el('span', {}, run.methodology_id?.replace('method-', '') || 'Legacy'),
      el('span', {}, run.case),
      el('span', {}, `${run.model || '—'} · ${where}`),
      el('span', {}, STATE_LABEL[run.status] || run.status),
      el('span', {}, String(run.model_calls || 0)),
      el('span', {}, detail)));
  }
  return table;
}

function comparisonTable(rows) {
  const table = el('div', { class: 'result-table' },
    el('div', { class: 'comparison-row header' }, 'Cell', 'Model / head', 'Runs', 'Accuracy', 'Safety (violations / unsafe proposals blocked)', 'Calls', 'Handoffs', 'Mean'));
  for (const row of rows) {
    table.append(el('div', { class: 'comparison-row' },
      el('span', {}, row.methodology_id?.replace('method-', '') || 'Legacy'),
      el('span', {}, `${row.model} · ${headNumber(row.head) || '—'}`),
      el('span', {}, String(row.runs)),
      el('span', {}, row.accuracy == null ? '—' : Math.round(row.accuracy * 100) + '%'),
      el('span', {}, `${row.safety_violations} / ${row.unsafe_proposals_blocked || 0}`),
      el('span', {}, String(row.model_calls)),
      el('span', {}, String(row.handoffs)),
      el('span', {}, seconds(row.mean_case_ms))));
  }
  return el('div', { class: 'comparison' }, el('h4', {}, 'Methodology comparison'), table);
}

function agenticTable(agentic) {
  if (!agentic) return el('div');
  const pct = (n, d) => (d ? Math.round((n / d) * 100) + '%' : '—');
  const table = el('div', { class: 'result-table' },
    el('div', { class: 'agentic-row header' }, 'Configuration (identity)', 'Runs · eligible', 'Complete & correct', 'Terminal reached · objective met', 'Released harm · unsafe process',
      'Unsafe proposals stopped', 'Reviewer errors', 'System errors', 'Escalation / abstention (unneeded)', 'Median turns · time', 'Evidence'));
  for (const g of agentic.groups) {
    table.append(el('div', { class: 'agentic-row', title: `${g.configuration_id} · reviewer ${g.reviewer_id || '—'} · scenarios ${g.scenario_set_version || '—'}` },
      el('span', {}, `${g.configuration}`, el('small', { class: 'context-meta' }, ` ${g.configuration_id}`)),
      el('span', {}, `${g.runs} · ${g.eligible} eligible${g.not_attributable ? ` (${g.not_attributable} not attributable)` : ''}`),
      el('span', {}, pct(g.correct, g.runs)),
      el('span', {}, `${pct(g.completed, g.runs)} (${g.limit_or_failure} limit/failure) · ${pct(g.objective_achieved, g.runs)}`),
      el('span', { class: g.released_harm || g.process_unsafe ? 'bad' : '' }, `${g.released_harm} · ${g.process_unsafe}${g.process_unverified ? ` (+${g.process_unverified} unverified)` : ''}`),
      el('span', {}, String(g.unsafe_proposals_stopped)),
      el('span', {}, String(g.reviewer_errors)),
      el('span', {}, String(g.system_errors)),
      el('span', {}, `${g.unnecessary_escalation} / ${g.unnecessary_abstention}`),
      el('span', {}, `${g.median_customer_turns ?? '—'} · ${seconds(g.median_duration_ms)}`),
      el('span', {}, g.evidence_class.join(', ') + (g.sample_note ? ' · ' + g.sample_note : ''))));
  }
  const causes = agentic.groups.map(g => el('p', { class: 'context-meta' }, el('strong', {}, `${g.configuration}: `),
    g.top_causes.map(([c, n]) => `${c} ×${n}`).join(' · ') || 'no recorded failure causes'));
  const roles = el('div', { class: 'result-table' },
    el('div', { class: 'agentic-role-row header' }, 'Role', 'Configuration', 'Executed models', 'Evidence', 'Median time'),
    ...agentic.role_findings.map(r => el('div', { class: 'agentic-role-row' }, el('span', {}, r.role.replace(/_/g, ' ')), el('span', {}, r.configuration),
      el('span', {}, r.executed_models.join(', ') || '—'), el('span', {}, r.evidence), el('span', {}, seconds(r.median_ms)))));
  const decisions = el('div', { class: 'result-table' },
    el('div', { class: 'agentic-role-row header' }, 'Role', 'Configuration', 'Decisions', 'Scored · correct', 'Unscored (why)'),
    ...(agentic.role_decisions || []).map(r => el('div', { class: 'agentic-role-row' }, el('span', {}, r.role.replace(/_/g, ' ')), el('span', {}, r.configuration),
      el('span', {}, String(r.decisions)), el('span', {}, `${r.scored} · ${r.correct}`),
      el('span', {}, Object.entries(r.unscored_reasons).map(([k, v]) => `${k} ×${v}`).join('; ') || '—'))));
  const reviewer = el('div', { class: 'result-table' },
    el('div', { class: 'agentic-role-row header' }, 'Reviewer model', 'Decision point', 'Decision', 'Count', 'Evaluator errors / blocked'),
    ...(agentic.reviewer_quality || []).map(r => el('div', { class: 'agentic-role-row' }, el('span', {}, r.reviewer_model), el('span', {}, r.decision_point),
      el('span', {}, r.decision || 'failed to decide'), el('span', {}, String(r.count)), el('span', {}, `${r.evaluator_errors} / ${r.blocked_by_application}`))));
  return el('section', { class: 'comparison' }, el('h4', {}, 'Agentic Testing results (complete runs)'),
    el('p', { class: 'context-meta' }, agentic.note),
    el('div', { class: 'result-actions' }, ...Object.entries(agentic.exports).map(([name, href]) => el('a', { class: 'btn small ghost', href }, name.replace('agentic_', '').replace(/_/g, ' ') + ' CSV'))),
    table, el('details', {}, el('summary', {}, 'Failure causes'), ...causes),
    el('details', {}, el('summary', {}, 'Role findings (from complete runs; "not exercised" is not a pass)'), roles),
    el('details', {}, el('summary', {}, 'Stage and review agent decisions (rubric-scored, every decision in the lifecycle)'), decisions),
    el('details', {}, el('summary', {}, 'Reviewer quality (the simulated human reviewer, scored separately from the ticket system)'), reviewer));
}

function teamTable(team) {
  if (!team) return el('div');
  const pct = v => (v == null ? '—' : Math.round(v * 100) + '%');
  const table = el('div', { class: 'result-table' },
    el('div', { class: 'team-compare-row header' }, 'Configuration', 'Runs', 'Accuracy', 'Failed', 'Safety / unsafe blocked', 'Routing unverified', 'Median case', 'Team time vs this'));
  for (const row of team.rows) {
    table.append(el('div', { class: 'team-compare-row' + (row.is_team ? ' team' : '') },
      el('span', {}, row.configuration),
      el('span', {}, String(row.runs)),
      el('span', {}, pct(row.accuracy)),
      el('span', {}, String(row.failed)),
      el('span', {}, `${row.safety_violations} / ${row.unsafe_proposals_blocked}`),
      el('span', {}, String(row.routing_unverified)),
      el('span', {}, seconds(row.median_case_ms)),
      el('span', {}, row.team_time_vs_this ? `${row.team_time_vs_this.toFixed(2)}×` : '—')));
  }
  const roleTimes = team.rows.map(row => el('p', { class: 'context-meta' }, el('strong', {}, row.configuration + ': '),
    Object.entries(row.median_role_ms).map(([role, ms]) => `${role.replaceAll('_', ' ')} ${seconds(ms)}`).join(' · ') || 'no model calls recorded'));
  return el('section', { class: 'comparison' }, el('h4', {}, 'Team vs. baselines (paired cases)'),
    el('p', { class: 'context-meta' }, `${team.note} Cases: ${team.paired_cases.join(', ') || 'none finished by every configuration yet'}.`),
    table, el('details', {}, el('summary', {}, 'Median model time per role (where the time cost lands)'), ...roleTimes));
}

function capacityTable({ id, steps, diminishing }) {
  if (!steps.length) return el('div');
  const table = el('div', { class: 'result-table' },
    el('div', { class: 'result-row header' }, 'Step / parallel', 'Completed / failed', 'Latency avg / p95', 'Total throughput', 'Queue wait', 'Heads'));
  for (const step of steps) {
    const throughput = step.total_tokens_per_second ? `${step.total_tokens_per_second.toFixed(1)} tok/s` : `${(step.calls_per_second || 0).toFixed(2)} calls/s`;
    table.append(el('div', { class: 'result-row' },
      el('span', {}, `${step.step} · ${step.parallel}`),
      el('span', {}, `${step.calls_completed} / ${step.calls_failed}`),
      el('span', {}, `${step.average_latency_ms ? Math.round(step.average_latency_ms) : '—'} / ${step.p95_latency_ms ? Math.round(step.p95_latency_ms) : '—'} ms`),
      el('span', {}, throughput),
      el('span', {}, step.average_queue_wait_ms == null ? '—' : Math.round(step.average_queue_wait_ms) + ' ms'),
      el('span', {}, (step.heads_served || []).map(headNumber).join(', ') || '—')));
  }
  return el('section', { class: 'comparison' },
    el('div', { class: 'result-heading' }, el('h4', {}, 'Stair-step throughput'), el('div', {},
      el('a', { class: 'btn small ghost', href: `/api/lab/${id}/capacity-steps.csv` }, 'Step CSV'),
      el('a', { class: 'btn small ghost', href: `/api/lab/${id}/capacity-steps.json` }, 'Step JSON'))),
    diminishing ? message(`Diminishing returns detected at step ${diminishing}.`)
      : el('p', { class: 'context-meta' }, 'Each row records the eligible heads actually used and the Gateway queue/latency evidence.'),
    table);
}

function errorGroups(groups) {
  if (!groups.length) return el('div');
  return el('details', { open: true }, el('summary', {}, 'Why trials failed'),
    ...groups.map(group => el('p', {}, `${group.count} · ${group.classification.replaceAll('_', ' ')} · ${group.example}`)));
}

export class ResultsView {
  constructor({ getExperiment, setExperiment, refresh, report }) {
    this.getExperiment = getExperiment;
    this.setExperiment = setExperiment;
    this.refresh = refresh;
    this.report = report;
    this.reviewPanels = new Map();
    this.build();
  }

  build() {
    this.history = el('select', { 'aria-label': 'Current experiment', onchange: event => { this.setExperiment(event.target.value); this.refresh(); } });
    this.historySignature = '';
    this.title = el('h3');
    this.meta = el('p');
    this.exports = el('div');
    this.status = el('p', { class: 'context-meta' });
    this.progress = el('progress', { max: '1', value: '0', class: 'lab-progress' });
    this.errors = section(errorGroups);
    this.capacity = section(capacityTable);
    this.reviews = el('div', { class: 'review-list' });
    this.runs = section(runsTable);
    this.runsNote = el('p', { class: 'context-meta' });
    this.comparison = section(comparisonTable);
    this.team = section(teamTable);
    this.agentic = section(agenticTable);
    this.actions = section(state => this.actionButtons(state));
    this.empty = message('Loading results…');
    this.card = el('section', { class: 'result-card', hidden: true },
      el('div', { class: 'result-heading' }, el('div', {}, this.title, this.meta), this.exports),
      this.status, this.progress, this.errors.node, this.capacity.node, this.reviews,
      this.agentic.node, this.runs.node, this.runsNote, this.team.node, this.comparison.node, this.actions.node);
    this.historyBox = el('section', { class: 'run-box', hidden: true },
      el('h3', {}, 'Current experiment'),
      el('p', {}, 'History is preserved. Selecting a different experiment changes only this view.'),
      this.history);
    this.node = el('section', { class: 'lab-screen', id: 'results-host' },
      el('div', { class: 'screen-heading' }, el('div', {}, el('h2', {}, 'Results'), el('p', {}, 'Live progress from the Gateway and the lab heads.'))),
      this.empty, this.historyBox, this.card);
  }

  async load() {
    const id = this.getExperiment();
    const data = await request('/api/lab/results' + (id ? '?experiment_id=' + encodeURIComponent(id) : ''));
    this.update(data);
  }

  update(data) {
    if (!data.history.length) {
      setText(this.empty, 'No experiments have been run yet. Open Run and press Start.');
      this.empty.hidden = false;
      this.historyBox.hidden = true;
      this.card.hidden = true;
      return;
    }
    const exp = data.experiments[0];
    if (!exp) return;
    this.setExperiment(exp.id);
    this.empty.hidden = true;
    this.historyBox.hidden = false;
    this.card.hidden = false;
    this.updateHistory(data.history, exp.id);

    setText(this.title, exp.name);
    setText(this.meta, `${exp.id} · ${exp.status} · review: ${exp.review_mode || 'legacy'}`);
    if (this.exports.dataset.id !== exp.id) {
      this.exports.dataset.id = exp.id;
      replace(this.exports,
        el('a', { class: 'btn small ghost', href: `/api/experiments/${exp.id}/export/bundle` }, 'Complete ZIP'),
        el('a', { class: 'btn small ghost', href: `/api/experiments/${exp.id}/export/csv` }, 'CSV'),
        el('a', { class: 'btn small ghost', href: `/api/experiments/${exp.id}/export/json` }, 'JSON'));
    }

    setText(this.status, this.statusText(exp));
    this.status.className = exp.termination_reason === 'failure_limit_reached' ? 'lab-message error' : 'context-meta';
    const terminal = exp.completed_runs + exp.failed_runs + exp.paused_runs + exp.not_run_runs;
    this.progress.max = Math.max(1, exp.runs_total);
    this.progress.value = terminal;

    this.errors.update(exp.error_groups || []);
    this.capacity.update({ id: exp.id, steps: exp.capacity_steps || [], diminishing: exp.diminishing_returns_at });
    this.updateReviews(exp.runs.filter(run => run.status === 'paused'));
    this.runs.update(exp.runs);
    setText(this.runsNote, exp.runs.length < exp.runs_total ? `Showing ${exp.runs.length} of ${exp.runs_total} runs (active and most recent first). The ZIP and CSV exports contain every run.` : '');
    this.team.update(exp.team_comparison || null);
    this.agentic.update(exp.agentic || null);
    this.comparison.update(exp.comparisons || []);
    this.actions.update({ id: exp.id, status: exp.status, notRun: exp.not_run_runs });
  }

  statusText(exp) {
    if (exp.status === 'stopping' || exp.stop_requested && exp.status === 'running') {
      return `Stopping: ${exp.active_runs} active flow${exp.active_runs === 1 ? '' : 's'} may finish. ${exp.not_run_runs} preserved flow${exp.not_run_runs === 1 ? '' : 's'} will not start.`;
    }
    if (exp.termination_reason === 'failure_limit_reached') {
      return `Stopped after the ${exp.failure_limit}-failure safety limit. ${exp.not_run_runs} preserved trials were not run and are not sending Gateway traffic.`;
    }
    const parts = [
      `${exp.completed_runs} completed`, `${exp.failed_runs} failed`,
      `${exp.active_runs} running now (limit ${exp.concurrency}${exp.peak_active_workers ? ', peak ' + exp.peak_active_workers : ''})`,
      `${exp.queued_runs} waiting`,
    ];
    if (exp.paused_runs) parts.push(`${exp.paused_runs} action required`);
    if (exp.not_run_runs) parts.push(`${exp.not_run_runs} not run`);
    return parts.join(' · ');
  }

  updateHistory(history, currentId) {
    const next = signature(history.map(row => [row.id, row.status]));
    if (next !== this.historySignature && document.activeElement !== this.history) {
      this.historySignature = next;
      replace(this.history, history.map(row => el('option', { value: row.id }, `${row.name} · ${new Date(row.created_at * 1000).toLocaleString()} · ${row.status}`)));
    }
    if (this.history.value !== currentId) this.history.value = currentId;
  }

  updateReviews(paused) {
    const wanted = new Set(paused.map(run => run.id));
    for (const [id, panel] of this.reviewPanels) {
      if (!wanted.has(id)) { panel.remove(); this.reviewPanels.delete(id); }
    }
    for (const run of paused) {
      if (!this.reviewPanels.has(run.id)) {
        const panel = this.reviewPanel(run);
        this.reviewPanels.set(run.id, panel);
        this.reviews.append(panel);
      }
    }
  }

  async command(button, path, body) {
    button.disabled = true;
    try {
      await request(path, { method: 'POST', body, timeoutMs: TIMEOUTS.command });
      this.refresh();
    } catch (failure) {
      this.report('That action did not complete: ' + failure.message);
      button.disabled = false;
    }
  }

  reviewPanel(run) {
    const review = (action, targetId, extra = {}) => event => this.command(event.target, `/api/experiments/runs/${run.id}/review`, {
      action, target_id: targetId, authorizer: 'testing-lab-operator',
      rationale: 'Explicit review from the isolated Testing Lab result screen.', ...extra,
    });
    const automatic = mode => event => this.command(event.target, `/api/lab/runs/${run.id}/automatic-review`, { mode });
    const box = el('details', { class: 'review-panel', open: true },
      el('summary', {}, `Action required · ${run.case} · ${run.methodology_id || 'legacy run'}`),
      el('p', {}, 'These decisions affect only this copied synthetic run. They do not authorize a live customer message or movement.'),
      el('div', { class: 'row' },
        el('button', { class: 'btn primary', onclick: automatic('automated') }, 'Use automated synthetic reviewer'),
        el('button', { class: 'btn ghost', onclick: automatic('autonomous_sandbox') }, 'Complete in autonomous sandbox')));
    for (const draft of run.pending?.emails || []) {
      box.append(el('section', { class: 'endpoint-card' }, el('h4', {}, 'Customer draft'), el('p', { class: 'preformatted' }, draft.body),
        el('div', { class: 'row' },
          el('button', { class: 'btn', onclick: review('send_email', draft.message_id) }, 'Approve synthetic send'),
          el('button', { class: 'btn ghost', onclick: review('reject_email', draft.message_id) }, 'Reject'))));
    }
    for (const movement of run.pending?.movements || []) {
      box.append(el('section', { class: 'endpoint-card' }, el('h4', {}, `${movement.movement_type} proposal`), el('p', {}, movement.reason || movement.rma_id),
        el('div', { class: 'row' },
          el('button', { class: 'btn', onclick: review('approve_movement', movement.rma_id) }, 'Approve in sandbox'),
          el('button', { class: 'btn ghost', onclick: review('reject_movement', movement.rma_id) }, 'Reject'))));
    }
    for (const quality of run.pending?.quality || []) {
      box.append(el('section', { class: 'endpoint-card' }, el('h4', {}, 'QA determination required'), el('p', {}, quality.summary || quality.notification_id),
        el('button', { class: 'btn', onclick: review('determine_qa', quality.notification_id, { determination: 'device hazard without confirmed injury' }) }, 'Record synthetic device-hazard determination')));
    }
    return box;
  }

  actionButtons({ id, status, notRun }) {
    const row = el('div', { class: 'result-actions' }, el('a', { href: `/trace?experiment=${id}`, class: 'btn ghost' }, 'Execution traces'));
    if (status === 'running') {
      row.append(el('button', { class: 'btn ghost', onclick: event => { event.target.textContent = 'Requesting stop…'; this.command(event.target, `/api/experiments/${id}/stop`); } }, 'Stop after active work'));
    } else if (status === 'stopping') {
      row.append(el('button', { class: 'btn ghost', disabled: true }, 'Stopping active flows…'));
    }
    if (notRun) {
      row.append(el('button', { class: 'btn ghost', onclick: event => {
        if (confirm(`Cancel ${notRun} preserved trials? Completed and failed evidence will remain.`)) this.command(event.target, `/api/experiments/${id}/cancel`);
      } }, 'Clear remaining queue'));
    }
    return row;
  }
}
