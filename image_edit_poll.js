(() => {
  const form = document.getElementById('image-edit-form');
  const panel = document.querySelector('[data-edit-panel]');
  if (!form || !panel) return;
  const status = document.getElementById('edit-status');
  const detail = document.getElementById('edit-detail');
  const preview = document.getElementById('edit-preview');
  const download = document.getElementById('edit-download');
  const privateNote = document.getElementById('edit-private-note');
  const discard = document.getElementById('edit-discard');
  let objectURL = null;

  const clearResult = () => {
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null;
    preview.removeAttribute('src');
    preview.hidden = true;
    download.removeAttribute('href');
    download.hidden = true;
    privateNote.hidden = true;
    discard.hidden = true;
  };
  window.addEventListener('pagehide', clearResult);
  discard.addEventListener('click', clearResult);

  const poll = async (id, privateMode) => {
    const base = privateMode ? '/image-edit/private/' : '/image-edit/';
    try {
      const response = await fetch(`${base}status/${encodeURIComponent(id)}`,
        {cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) throw new Error(`Status request failed (${response.status})`);
      const job = await response.json();
      status.textContent = `${job.status}: ${job.stage}`;
      if (job.status === 'completed') {
        detail.textContent = `Removed ${job.result?.target || 'selected object'}. The original image size was preserved.`;
        const url = `${base}result/${encodeURIComponent(id)}`;
        if (privateMode) {
          const result = await fetch(url, {cache: 'no-store', credentials: 'same-origin'});
          if (!result.ok) throw new Error(`Private image retrieval failed (${result.status})`);
          objectURL = URL.createObjectURL(await result.blob());
          preview.src = objectURL;
          download.href = objectURL;
          privateNote.hidden = false;
          discard.hidden = false;
          const token = form.elements.namedItem('token').value;
          await fetch(`/image-edit/private/${encodeURIComponent(id)}`,
            {method: 'DELETE', cache: 'no-store', credentials: 'same-origin', headers: {'X-CSRF-Token': token}});
        } else {
          preview.src = url;
          download.href = url;
        }
        preview.hidden = false;
        download.hidden = false;
      } else if (job.status === 'failed') {
        detail.textContent = job.error || 'The edit could not be completed.';
        if (privateMode) {
          const token = form.elements.namedItem('token').value;
          await fetch(`/image-edit/private/${encodeURIComponent(id)}`,
            {method: 'DELETE', cache: 'no-store', credentials: 'same-origin', headers: {'X-CSRF-Token': token}});
        }
      } else {
        setTimeout(() => poll(id, privateMode), 2000);
      }
    } catch (error) {
      status.textContent = error.message;
      setTimeout(() => poll(id, privateMode), 5000);
    }
  };

  form.addEventListener('submit', async event => {
    if (!form.elements.namedItem('private').checked) return;
    event.preventDefault();
    clearResult();
    panel.hidden = false;
    status.textContent = 'Submitting private edit…';
    detail.textContent = '';
    try {
      const response = await fetch(form.action,
        {method: 'POST', body: new FormData(form), cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) throw new Error(`Private edit was rejected (${response.status})`);
      const job = await response.json();
      form.reset();
      poll(job.id, true);
    } catch (error) {
      status.textContent = error.message;
    }
  });

  if (panel.dataset.editJob) poll(panel.dataset.editJob, false);
})();
