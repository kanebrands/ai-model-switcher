"""Browser upload and progress page for grounded object removal."""
import html

from benchmark_views import shell


def page(csrf, job_id=None):
    esc = lambda value: html.escape(str(value), quote=True)
    progress = (f'<section class="card" data-edit-job="{esc(job_id)}">'
                '<h2>Editing progress</h2><p id="edit-status" role="status">Checking job status…</p>'
                '<p id="edit-detail" class="note"></p>'
                '<img id="edit-preview" alt="Edited image preview" hidden style="max-width:100%;height:auto">'
                '<p><a id="edit-download" download="edited-image.png" hidden>Download edited image</a></p></section>'
                '<script src="/assets/image_edit_poll.js" defer></script>') if job_id else ""
    body = ('<section class="card"><div class="eyebrow">OBJECT REMOVAL</div><h1>Edit an image</h1>'
            '<p>Upload a photo and describe the object to remove. The switcher locates it, builds a mask, '
            'runs the installed inpainting checkpoint, and returns a PNG at the original dimensions. '
            'The first edit downloads Grounding DINO and SAM into the persistent model cache.</p>'
            '<form method="post" action="/image-edit/submit" enctype="multipart/form-data">'
            f'<input type="hidden" name="token" value="{esc(csrf)}">'
            '<label>Photo <input type="file" name="image" accept="image/jpeg,image/png,image/webp" required></label>'
            '<label>Instruction <input type="text" name="prompt" maxlength="1000" '
            'placeholder="Remove the trash can from the attached image" required></label>'
            '<button type="submit">Remove object</button></form>'
            '<p class="note">A Creative studio profile and an installed inpainting checkpoint are required. '
            'If several matching objects are present, the job stops and asks for a more precise selection.</p>'
            '<p><a href="/profiles">Task profiles</a></p></section>')
    return shell("Image editor", body + progress)
