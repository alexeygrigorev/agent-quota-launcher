import unittest

from launcher.tags import run_tag_for


class RunTag(unittest.TestCase):
    def test_bare_id_gets_prefix(self):
        self.assertEqual(run_tag_for("genuine-1"), "task-genuine-1")

    def test_prefixed_id_not_doubled(self):
        # Head finding: task ids that already carry task- must never produce
        # task-task-... tags.
        self.assertEqual(run_tag_for("task-genuine-r3-1"), "task-genuine-r3-1")
        self.assertEqual(run_tag_for("task-task-x"), "task-task-x")  # id is data

    def test_non_string_id(self):
        self.assertEqual(run_tag_for(42), "task-42")

if __name__ == '__main__':
    unittest.main()
