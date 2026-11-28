"""Prompt grounding, ambiguity handling, and bounded inpainting graph."""
import unittest

from image_edit import target_from_prompt, select_box, workflow, private_workflow
from image_edit_views import page
from multimodal_jobs import PrivateJobStore, GPU_KINDS
from multimodal_workers import render_workflow, sdxl_reference_workflow, image


class ImageEditTests(unittest.TestCase):
    def test_extracts_requested_object(self):
        self.assertEqual(target_from_prompt("Remove the trash can from the attached image."), "trash can")
        self.assertEqual(target_from_prompt("Please erase the trashcan from this photo!"), "trash can")
        with self.assertRaisesRegex(ValueError, "which object"):
            target_from_prompt("Make this picture better")

    def test_detection_requires_clear_match(self):
        self.assertEqual(select_box([[1, 2, 20, 30]], [0.8])[0], [1.0, 2.0, 20.0, 30.0])
        with self.assertRaisesRegex(ValueError, "not found"):
            select_box([[1, 2, 20, 30]], [0.1])
        with self.assertRaisesRegex(ValueError, "Several matching"):
            select_box([[1, 2, 20, 30], [40, 50, 80, 90]], [0.8, 0.78])

    def test_inpaint_graph_uses_source_mask_and_save_node(self):
        graph = workflow("inpaint.safetensors", "source.png", "mask.png", "trash can", 22)
        self.assertEqual(graph["5"]["class_type"], "LoadImage")
        self.assertEqual(graph["6"]["inputs"]["channel"], "red")
        self.assertEqual(graph["7"]["inputs"]["mask"], ["6", 0])
        self.assertEqual(graph["10"]["inputs"]["latent_image"], ["7", 0])
        self.assertEqual(graph["12"]["class_type"], "SaveImage")

    def test_page_has_simple_upload_and_safe_job_id(self):
        html = page("csrf", "a-test-id")
        self.assertIn('type="file"', html)
        self.assertIn('name="prompt"', html)
        self.assertIn('data-edit-job="a-test-id"', html)
        self.assertIn("image_edit_poll.js", html)
        self.assertIn('name="private"', html)
        self.assertIn('id="image-generate-form"', html)
        self.assertIn('name="reference"', html)
        self.assertIn('multiple', html)

    def test_reference_graph_encodes_uploaded_image_and_strength(self):
        graph = sdxl_reference_workflow("garden", "sdxl.safetensors", b"PNG", 512, 512, 10, 0.55)
        self.assertEqual(graph["5"]["class_type"], "AISwitcherReferenceImage")
        self.assertEqual(graph["11"]["class_type"], "VAEEncode")
        self.assertEqual(graph["8"]["inputs"]["latent_image"], ["11", 0])
        self.assertEqual(graph["8"]["inputs"]["denoise"], 0.55)

    def test_multiple_references_generate_separate_images_in_zip(self):
        import io
        import zipfile
        from unittest.mock import patch
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("reference-1.bin", b"image1")
            bundle.writestr("reference-2.bin", b"image2")
        job = {"id": "abc", "request": {"prompt": "garden", "checkpoint": "sdxl.safetensors",
                                       "width": None, "height": None, "steps": 4,
                                       "reference_strength": 0.55, "_private": True}}
        frames = [object(), object()]
        with patch("multimodal_workers.reference_images", return_value=frames), \
             patch("multimodal_workers._prepare_reference", side_effect=[(b"PNG1", 512, 512),
                                                                         (b"PNG2", 512, 512)]), \
             patch("multimodal_workers.Path.is_file", return_value=True), \
             patch("multimodal_workers.render_workflow", side_effect=[({}, b"out1", "image/png"),
                                                                      ({}, b"out2", "image/png")]) as render:
            result, payload, mime = image(job, archive.getvalue(), lambda *args, **kwargs: None)
        self.assertEqual((mime, result["output_count"], result["reference_count"]),
                         ("application/zip", 2, 2))
        with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
            self.assertEqual(bundle.read("generated-01.png"), b"out1")
            self.assertEqual(bundle.read("generated-02.png"), b"out2")
        self.assertEqual(render.call_count, 2)
        self.assertTrue(all(call.kwargs["private_key"] == "private_images" for call in render.call_args_list))

    def test_private_jobs_leave_no_database_or_artifact_files(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            store = PrivateJobStore()
            job = store.submit("owner", "image_edit", {"prompt": "remove a can"}, b"image bytes")
            self.assertEqual(store.claim(GPU_KINDS)["id"], job["id"])
            store.complete(job["id"], {"target": "can"}, b"PNG", "image/png")
            self.assertEqual(store.artifact(store.get(job["id"], "owner")), b"PNG")
            self.assertEqual(store.get(job["id"], "other"), None)
            self.assertEqual(list(Path(folder).iterdir()), [])
            self.assertTrue(store.delete(job["id"], "owner"))
            self.assertIsNone(store.get(job["id"], "owner"))

    def test_private_workflow_uses_ram_only_nodes(self):
        class MemoryImage:
            def save(self, buffer, format):
                buffer.write(b"PNG")
        source = mask = MemoryImage()
        graph = private_workflow("inpaint.safetensors", source, mask, "trash can", 22)
        self.assertEqual(graph["5"]["class_type"], "AISwitcherPrivateImageAndMask")
        self.assertEqual(graph["12"]["class_type"], "AISwitcherPrivateImageResult")
        self.assertNotIn("SaveImage", {node["class_type"] for node in graph.values()})

    def test_private_comfy_result_is_returned_and_history_deleted(self):
        import base64
        from unittest.mock import MagicMock, patch
        history = {"ref": {"outputs": {"12": {"private_images": [base64.b64encode(b"PNG").decode()]}}}}
        with patch("multimodal_workers.comfy_request", side_effect=[{"prompt_id": "ref"}, history]), \
             patch("multimodal_workers.urllib.request.urlopen") as open_result:
            response = MagicMock()
            response.__enter__.return_value = response
            open_result.return_value = response
            _, artifact, mime = render_workflow({"id": "job"}, {"12": {}}, lambda *a, **k: None,
                                                output_node="12", private_key="private_images")
        self.assertEqual((artifact, mime), (b"PNG", "image/png"))
        history_request = open_result.call_args.args[0]
        self.assertIn(b'"delete": ["ref"]', history_request.data)


if __name__ == "__main__":
    unittest.main()
