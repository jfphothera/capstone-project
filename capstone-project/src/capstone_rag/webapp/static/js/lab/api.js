// Testing Lab HTTP helpers.
//
// Rules this module enforces so the page cannot flood itself:
//   * every request has an explicit, per-call timeout (AbortController);
//   * a poller never starts a request while the previous one is in flight;
//   * failures back off exponentially instead of retrying every interval;
//   * polling pauses while the browser tab is hidden.

export const TIMEOUTS = {
  quick: 15000,    // state, plan, results
  command: 30000,  // review commands, stop, cancel
  start: 90000,    // start may run a Gateway availability preflight
};

export class LabApiError extends Error {
  constructor(message, { status = 0, timedOut = false } = {}) {
    super(message);
    this.name = 'LabApiError';
    this.status = status;
    this.timedOut = timedOut;
  }
}

function detailText(detail) {
  if (detail == null) return '';
  return typeof detail === 'string' ? detail : JSON.stringify(detail);
}

export async function request(path, { method = 'GET', body, timeoutMs = TIMEOUTS.quick } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      method,
      signal: controller.signal,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const value = await response.json().catch(() => ({ detail: response.statusText }));
    if (!response.ok) {
      throw new LabApiError(detailText(value.detail) || `HTTP ${response.status}`, { status: response.status });
    }
    return value;
  } catch (error) {
    if (error.name === 'AbortError') {
      throw new LabApiError(`No answer from the Testing Lab after ${Math.round(timeoutMs / 1000)} s.`, { timedOut: true });
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

// Single-flight polling loop. `task` is awaited; the next tick is scheduled
// only after it settles. refresh() runs a tick now unless one is in flight.
export function createPoller(task, { intervalMs = 3000, maxIntervalMs = 30000, onError = () => {}, onRecover = () => {} } = {}) {
  let timer = null;
  let running = false;
  let stopped = true;
  let failures = 0;

  function schedule() {
    if (stopped) return;
    const delay = failures ? Math.min(maxIntervalMs, intervalMs * 2 ** Math.min(failures, 4)) : intervalMs;
    timer = setTimeout(tick, delay);
  }

  async function tick() {
    timer = null;
    if (stopped) return;
    if (document.hidden) { schedule(); return; }
    running = true;
    try {
      await task();
      if (failures) { failures = 0; onRecover(); }
    } catch (error) {
      failures += 1;
      onError(error, failures);
    } finally {
      running = false;
      schedule();
    }
  }

  return {
    start() { if (!stopped) return; stopped = false; tick(); },
    stop() { stopped = true; clearTimeout(timer); timer = null; },
    refresh() { if (stopped || running) return; clearTimeout(timer); tick(); },
  };
}
