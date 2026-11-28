(() => {
  const forms = [...document.querySelectorAll('#image-edit-form, #image-generate-form')];
  const panel = document.querySelector('[data-edit-panel]');
  if (!forms.length || !panel) return;
  const status = document.getElementById('edit-status');
  const detail = document.getElementById('edit-detail');
  const gallery = document.getElementById('edit-gallery');
  const download = document.getElementById('edit-download');
  const privateNote = document.getElementById('edit-private-note');
  const discard = document.getElementById('edit-discard');
  const objectURLs = [];
  let generation = 0;
  function clearResult() {
    generation++;
    objectURLs.forEach(url => URL.revokeObjectURL(url));
    objectURLs.length = 0;
    gallery.replaceChildren();
    download.removeAttribute('href');
    download.hidden = true;
    privateNote.hidden = true;
    discard.hidden = true;
  }
  window.addEventListener('pagehide', clearResult);
  discard.addEventListener('click', () => { clearResult(); panel.hidden = true; });
  async function getBlob(url) {
    const response = await fetch(url, {cache: 'no-store', credentials: 'same-origin'});
    if (!response.ok) throw new Error(`Result request failed (${response.status})`);
    return response.blob();
  }
  async function poll(id, privateMode, task, run) {
    const base = privateMode ? '/image-edit/private/' : task === 'generate' ? '/image-edit/generation/' : '/image-edit/';
    const encoded = encodeURIComponent(id);
    try {
      const response = await fetch(`${base}status/${encoded}`, {cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) throw new Error(`Status request failed (${response.status})`);
      const job = await response.json();
      if (run !== generation) return;
      status.textContent = `${job.status}: ${job.stage}`;
      if (job.status === 'completed') {
        const count = task === 'generate' ? Math.min(3, job.result?.output_count || 1) : 1;
        detail.textContent = task === 'generate' ? `Generated ${count} image${count === 1 ? '' : 's'}.` :
          `Removed ${job.result?.target || 'selected object'}. The original image size was preserved.`;
        const resultURL = `${base}result/${encoded}`;
        for (let index = 1; index <= count; index++) {
          const imageURL = task === 'generate' ? (privateMode ?
            `/image-edit/private/preview/${encoded}?index=${index}` : `${base}preview/${encoded}?index=${index}`) : resultURL;
          const src = privateMode ? URL.createObjectURL(await getBlob(imageURL)) : imageURL;
          if (privateMode) objectURLs.push(src);
          const img = document.createElement('img');
          img.src = src;
          img.alt = `Result image ${index}`;
          img.style.cssText = 'max-width:min(100%, 420px);height:auto;object-fit:contain';
          gallery.appendChild(img);
        }
        const result = privateMode ? URL.createObjectURL(await getBlob(resultURL)) : resultURL;
        if (privateMode) objectURLs.push(result);
        download.href = result;
        download.download = task === 'generate' ? (count > 1 ? 'generated-images.zip' : 'generated-image.png') : 'edited-image.png';
        download.textContent = count > 1 ? 'Download images ZIP' : 'Download image';
        download.hidden = false;
        if (privateMode) {
          privateNote.hidden = false;
          discard.hidden = false;
          await fetch(`/image-edit/private/${encoded}`, {method: 'DELETE', cache: 'no-store',
            credentials: 'same-origin', headers: {'X-CSRF-Token': forms[0].elements.namedItem('token').value}});
        }
      } else if (job.status === 'failed') {
        detail.textContent = job.error || 'The image could not be created.';
        if (privateMode) await fetch(`/image-edit/private/${encoded}`, {method: 'DELETE', cache: 'no-store',
          credentials: 'same-origin', headers: {'X-CSRF-Token': forms[0].elements.namedItem('token').value}});
      } else setTimeout(() => poll(id, privateMode, task, run), 2000);
    } catch (error) {
      if (run !== generation) return;
      status.textContent = error.message;
      setTimeout(() => poll(id, privateMode, task, run), 5000);
    }
  }
  forms.forEach(form => form.addEventListener('submit', async event => {
    const isGenerate = form.id === 'image-generate-form';
    if (isGenerate) {
      const refs = form.elements.namedItem('reference').files;
      if (refs.length > 3 || [...refs].some(file => file.size > 8 * 1024 * 1024) ||
          [...refs].reduce((sum, file) => sum + file.size, 0) > 16 * 1024 * 1024) {
        event.preventDefault();
        status.textContent = 'Choose at most three images, each under 8 MiB and 16 MiB total.';
        panel.hidden = false;
        return;
      }
    }
    if (!form.elements.namedItem('private').checked) return;
    event.preventDefault();
    clearResult();
    panel.hidden = false;
    const task = isGenerate ? 'generate' : 'edit';
    status.textContent = 'Submitting private image job…';
    detail.textContent = '';
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form),
        cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) throw new Error(`Private image request rejected (${response.status})`);
      const job = await response.json();
      form.reset();
      poll(job.id, true, task, generation);
    } catch (error) { status.textContent = error.message; }
  }));
  if (panel.dataset.editJob) poll(panel.dataset.editJob, false, panel.dataset.editTask, generation);
})();
