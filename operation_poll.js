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
