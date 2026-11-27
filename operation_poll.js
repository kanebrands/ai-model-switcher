/* Update operation progress without navigating or replacing forms in the page. */
(() => {
  const monitor = document.querySelector('[data-operation-monitor]');
  if (!monitor) return;
  const endpoint = monitor.dataset.operationMonitor;
  let timer;
  async function poll() {
    if (document.hidden) {
      timer = setTimeout(poll, 5000);
      return;
    }
    try {
      const response = await fetch(endpoint, {cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      monitor.replaceChildren(document.createTextNode(state.message || 'Checking operation status…'));
      if (state.active) {
        timer = setTimeout(poll, 5000);
      } else {
        const link = document.createElement('a');
        link.href = window.location.href;
        link.textContent = 'Show updated results';
        monitor.append(document.createTextNode(' · '), link);
      }
    } catch (error) {
      monitor.textContent = 'Status unavailable; retrying…';
      timer = setTimeout(poll, 10000);
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && timer) {
      clearTimeout(timer);
      timer = setTimeout(poll, 0);
    }
  });
  poll();
})();

/* Keep the hero honest while a selected model loads, exits, or becomes ready. */
(() => {
  const badge = document.querySelector('[data-workload-badge]');
  if (!badge) return;
  const label = document.querySelector('[data-workload-label]');
  const detail = document.querySelector('[data-workload-detail]');
  const labels = {ready: 'Ready to use', loading: 'Loading model', failed: 'Model failed',
                  idle: 'No active workload', unavailable: 'Status unavailable'};
  document.querySelectorAll('[data-copy-field]').forEach(field => {
    field.addEventListener('focus', () => field.select());
    field.addEventListener('click', () => field.select());
  });
  async function pollWorkload() {
    if (document.hidden) {
      setTimeout(pollWorkload, 5000);
      return;
    }
    let state = 'unavailable';
    let next = 10000;
    try {
      const response = await fetch('/workload/status', {cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      state = result.state in labels ? result.state : 'unavailable';
      badge.textContent = labels[state];
      label.textContent = result.label || 'No model running';
      detail.textContent = result.detail || '';
      if (state === 'loading') next = 3000;
    } catch (error) {
      badge.textContent = labels.unavailable;
      detail.textContent = 'Could not check model readiness; retrying…';
    }
    badge.classList.toggle('is-ready', state === 'ready');
    badge.classList.toggle('is-failed', state === 'failed');
    setTimeout(pollWorkload, next);
  }
  pollWorkload();
})();
