import json
import unittest

import benchmark_views


def run(run_id, tokens_per_second, context):
    return {
        "id": run_id,
        "started_at": f"2026-11-07T00:00:0{run_id}+00:00",
        "model_id": f"model-{run_id}",
        "status": "completed",
        "system_json": json.dumps({"serving_config": {"--ctx-size": str(context)}}) if context else "{}",
        "results_json": json.dumps({"generation": {"end_to_end_tokens_per_second": tokens_per_second}}) if tokens_per_second else "{}",
    }


class BenchmarkTableTests(unittest.TestCase):
    def setUp(self):
        self.runs = [run("1", 80, 32768), run("2", 150, 16384), run("3", 90, 32768), run("4", None, None)]

    def ids(self, groups):
        return [[item[0]["id"] for item in rows] for _, rows in groups]

    def test_default_sort_is_tokens_per_second_descending_with_missing_last(self):
        self.assertEqual(self.ids(benchmark_views.sorted_rows(self.runs)), [["2", "3", "1", "4"]])
        page = benchmark_views.index(self.runs, "token")
        self.assertIn('aria-sort="descending"><a href="/benchmarks?sort=tokens_per_second', page)
        self.assertIn("Tokens/s ▼", page)
        self.assertNotIn('name="group" value="context" checked', page)
        self.assertIn('<body class="wide">', page)
        self.assertIn('class="results-table"', page)

    def test_clicking_heading_can_sort_ascending(self):
        self.assertEqual(self.ids(benchmark_views.sorted_rows(self.runs, "tokens_per_second", "asc")), [["1", "3", "2", "4"]])
        page = benchmark_views.index(self.runs, "token")
        self.assertIn("sort=tokens_per_second&amp;dir=asc", page)

    def test_context_grouping_orders_groups_then_sorts_within_each(self):
        groups = benchmark_views.sorted_rows(self.runs, group_context=True)
        self.assertEqual([size for size, _ in groups], [32768.0, 16384.0, None])
        self.assertEqual(self.ids(groups), [["3", "1"], ["2"], ["4"]])
        page = benchmark_views.index(self.runs, "token", group_context=True)
        self.assertIn("Context: 32,768 tokens", page)
        self.assertIn("Mixed or no context", page)
        self.assertIn('name="group" value="context" checked', page)
        self.assertIn("group=context", page)

    def test_repeated_model_runs_are_averaged_and_drill_down(self):
        repeated = [run("1", 80, 32768), run("2", 120, 32768)]
        repeated[1]["model_id"] = repeated[0]["model_id"]
        groups = benchmark_views.sorted_rows(repeated)
        self.assertEqual(len(groups[0][1]), 1)
        combined, metric = groups[0][1][0]
        self.assertEqual(combined["count"], 2)
        self.assertEqual(combined["started_at"], repeated[1]["started_at"])
        self.assertEqual(metric["tokens_per_second"], 100)
        page = benchmark_views.index(repeated, "token")
        self.assertIn("/benchmarks/model?model_id=model-1", page)
        self.assertIn('name="model_id" value="model-1"', page)
        self.assertEqual(page.count("Delete all"), 1)
        details = benchmark_views.model_detail("model-1", repeated[::-1], "token")
        self.assertEqual(details.count("Delete run"), 2)
        self.assertIn("/benchmarks/1", details)
        self.assertIn("/benchmarks/2", details)

    def test_image_not_applicable_displays_na(self):
        example = run("1", 80, 32768)
        example["results_json"] = json.dumps({"image_generation": {"status": "not_applicable"}})
        self.assertEqual(benchmark_views.summary(example)["image"], "NA")
        self.assertIn("<td>NA</td>", benchmark_views.index([example], "token"))


if __name__ == "__main__":
    unittest.main()
