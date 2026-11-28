"""Browser upload and progress page for image generation and object removal."""
import html

from benchmark_views import shell


def page(csrf, job_id=None, task="edit"):
    esc = lambda value: html.escape(str(value), quote=True)
    progress = (f'<section class="card" data-edit-panel data-edit-job="{esc(job_id or "")}" data-edit-task="{esc(task)}"'
                f'{"" if job_id else " hidden"}>'
                '<h2>Editing progress</h2><p id="edit-status" role="status">Checking job status…</p>'
                '<p id="edit-detail" class="note"></p>'
                '<div id="edit-gallery" style="display:flex;flex-wrap:wrap;gap:1rem"></div>'
                '<p><a id="edit-download" download="edited-image.png" hidden>Download edited image</a></p>'
                '<p id="edit-private-note" class="note" hidden>Private result stays only in this browser tab until you close it.</p>'
                '<button type="button" id="edit-discard" hidden>Discard private result</button></section>'
                '<script src="/assets/image_edit_poll.js" defer></script>')
    generation = ('<section class="card"><div class="eyebrow">IMAGE GENERATION</div><h1>Generate an image</h1>'
                  '<p>Describe the image you want. Optionally upload up to three reference photos. '
                  'Each reference produces a separate image guided by its composition; without references, '
                  'the model creates a new image from the prompt. Multiple results download as a ZIP.</p>'
                  '<form id="image-generate-form" method="post" action="/image-edit/generate" '
                  'enctype="multipart/form-data" autocomplete="off">'
                  f'<input type="hidden" name="token" value="{esc(csrf)}">'
                  '<label>Prompt <input type="text" name="prompt" autocomplete="off" maxlength="1000" '
                  'placeholder="A sunlit garden in watercolor" required></label>'
                  '<label>Reference images (optional, up to three) '
                  '<input type="file" name="reference" accept="image/jpeg,image/png,image/webp" multiple></label>'
                  '<label>How much to change each reference '
                  '<select name="reference_strength"><option value="0.35">Subtle</option>'
                  '<option value="0.55" selected>Balanced</option><option value="0.75">Strong</option></select></label>'
                  '<p class="note">The reference keeps some composition and colors. This is image-to-image '
                  'generation; it does not guarantee a person’s identity or exact details are preserved. '
                  'Each image must be 8 MiB or less; all uploads together must be 16 MiB or less.</p>'
                  '<label><input type="checkbox" name="private"> Private mode: do not retain uploads, prompt, '
                  'job record, or results on the server. Results stay in this tab until closed.</label>'
                  '<button type="submit">Generate image</button></form>'
                  '<p class="note">Requires a Creative studio profile with an installed SDXL-style image checkpoint.</p>'
                  '<p><a href="/profiles">Task profiles</a></p></section>')
    body = ('<section class="card"><div class="eyebrow">OBJECT REMOVAL</div><h1>Edit an image</h1>'
            '<p>Upload a photo and describe the object to remove. The switcher locates it, builds a mask, '
            'runs the installed inpainting checkpoint, and returns a PNG at the original dimensions. '
            'The first edit downloads Grounding DINO and SAM into the persistent model cache.</p>'
            '<form id="image-edit-form" method="post" action="/image-edit/submit" '
            'enctype="multipart/form-data" autocomplete="off">'
            f'<input type="hidden" name="token" value="{esc(csrf)}">'
            '<label>Photo <input type="file" name="image" accept="image/jpeg,image/png,image/webp" required></label>'
            '<label>Instruction <input type="text" name="prompt" autocomplete="off" maxlength="1000" '
            'placeholder="Remove the trash can from the attached image" required></label>'
            '<label><input type="checkbox" name="private"> Private mode: do not retain the upload, prompt, '
            'job record, or result on the server. Progress and the edited image stay in this tab only. '
            'Closing the tab loses access to the result.</label>'
            '<button type="submit">Remove object</button></form>'
            '<p class="note">A Creative studio profile and an installed inpainting checkpoint are required. '
            'If several matching objects are present, the job stops and asks for a more precise selection.</p>'
            '<p><a href="/profiles">Task profiles</a></p></section>')
    return shell("Image editor", generation + body + progress)
