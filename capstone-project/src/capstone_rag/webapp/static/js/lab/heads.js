// "Heads" screen: what each lab machine can run through the Gateway now.
import { el, message } from './dom.js';

export function headsScreen(state, { onRefresh }) {
  const host = el('section', { class: 'lab-screen' },
    el('div', { class: 'screen-heading' },
      el('div', {}, el('h2', {}, 'Lab heads'), el('p', {}, 'What each machine can run through the Gateway right now.')),
      el('button', { class: 'btn primary', onclick: onRefresh }, 'Refresh')));

  for (const head of state.heads) {
    const hardware = head.gpu ? `GPU · ${head.gpu}` : 'CPU';
    const body = el('div', { class: 'head-body' });
    host.append(el('details', { class: 'head-card' },
      el('summary', {},
        el('div', {},
          el('h3', {}, `Head ${head.number} · ${head.address || head.resource_id}`),
          el('p', {}, `${Math.round(head.ram_gb || 0)} GB RAM · ${hardware} · ${head.status}`))),
      body));

    if (!head.serving) {
      body.append(message('The administrator adds heads and allows models in the admin hub (Fleet).'));
      continue;
    }
    body.append(el('div', { class: 'model-filter' },
      el('button', { class: 'btn small', onclick: () => body.querySelectorAll('[data-usable=false]').forEach(row => { row.hidden = true; }) }, 'Usable now'),
      el('button', { class: 'btn small ghost', onclick: () => body.querySelectorAll('[data-usable]').forEach(row => { row.hidden = false; }) }, 'All')));
    for (const model of head.models) {
      const context = model.max_context ? `${Number(model.max_context).toLocaleString()} context · ` : '';
      body.append(el('div', { class: 'head-model', 'data-usable': String(model.usable) },
        el('strong', {}, model.name),
        el('span', {}, `${model.kind} · qualified ${model.qualified ? 'yes' : 'no'} · ${context}${model.usable ? 'usable now' : model.reason}`)));
    }
  }
  return host;
}
