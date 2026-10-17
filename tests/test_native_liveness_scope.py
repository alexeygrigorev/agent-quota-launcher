import subprocess
import unittest
from unittest.mock import patch

from launcher.launch import native_status


class NativeLivenessTests(unittest.TestCase):
    def test_global_uuid_is_used_without_cwd_tag_resolution(self):
        with patch('launcher.launch.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0,
                '{"phase":"running","containment_empty":false}', '')
            self.assertEqual(native_status('20c23552-852c-406a-8009-25037f7b59ba')[0], 'alive')
            self.assertEqual(run.call_args.args[0][2], '20c23552-852c-406a-8009-25037f7b59ba')

    def test_missing_tag_or_transport_is_not_process_exit(self):
        for error in ('no session tagged task-hub-x', 'transport error', 'stale socket'):
            with self.subTest(error=error), patch('launcher.launch.subprocess.run') as run:
                run.return_value = subprocess.CompletedProcess([], 1, '', error)
                self.assertEqual(native_status('task-hub-x')[0], 'unknown')


if __name__ == '__main__':
    unittest.main()
