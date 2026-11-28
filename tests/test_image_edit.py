"""Prompt grounding, ambiguity handling, and bounded inpainting graph."""
import unittest

from image_edit import target_from_prompt, select_box, workflow
from image_edit_views import page


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


if __name__ == "__main__":
    unittest.main()
