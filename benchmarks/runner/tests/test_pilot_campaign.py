import unittest
from benchmarks.runner.pilot_campaign import ARMS, common_prompt, load_tasks, schedule, REPO


class PilotTests(unittest.TestCase):
    def test_schedule_is_balanced_and_paired(self):
        tasks = [{"id": str(index)} for index in range(6)]
        roster = schedule(tasks)
        self.assertEqual(len(roster), 18)
        for task in tasks:
            self.assertEqual({row["arm"] for row in roster if row["task"] is task}, set(ARMS))
        orders = [tuple(row["arm"] for row in roster[index:index+3]) for index in range(0, 18, 3)]
        self.assertEqual(len(set(orders)), 6)
        for position in range(3):
            self.assertEqual([order[position] for order in orders].count("codex_pure"), 2)

    def test_real_candidate_source_hashes_still_match(self):
        candidate = REPO / "benchmarks/task-candidates.json"
        if not candidate.exists(): self.skipTest("Dataset prepared separately")
        document = load_tasks(candidate)
        self.assertEqual(len(document["tasks"]), 6)

    def test_common_prompt_does_not_contain_hidden_paths_or_answers(self):
        prompt = common_prompt({"solution_files": ["solution.py"]})
        self.assertIn("INSTRUCTIONS.md", prompt)
        self.assertNotIn("reference/", prompt)
        self.assertNotIn("evaluators/", prompt)


if __name__ == "__main__": unittest.main()
