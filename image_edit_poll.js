(() => {
  const panel = document.querySelector('[data-edit-job]');
  if (!panel) return;
  const id = panel.dataset.editJob;
  const status = document.getElementById('edit-status');
  const detail = document.getElementById('edit-detail');
  const preview = document.getElementById('edit-preview');
  const download = document.getElementById('edit-download');
  const poll = async () => {
    try {
      const response = await fetch(`/image-edit/status/${encodeURIComponent(id)}`, {cache: 'no-store'});
      if (!response.ok) throw new Error(`Status request failed (${response.status})`);
      const job = await response.json();
      status.textContent = `${job.status}: ${job.stage}`;
      if (job.status === 'completed') {
        detail.textContent = `Removed ${job.result?.target || 'selected object'}. The original image size was preserved.`;
        const url = `/image-edit/result/${encodeURIComponent(id)}`;
        preview.src = url;
        preview.hidden = false;
        download.href = url;
        download.hidden = false;
      } else if (job.status === 'failed') {
        detail.textContent = job.error || 'The edit could not be completed.';
      } else {
        setTimeout(poll, 2000);
      }
    } catch (error) {
      status.textContent = error.message;
      setTimeout(poll, 5000);
    }
  };
  poll();
})();
