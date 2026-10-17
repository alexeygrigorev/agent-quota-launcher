import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SubmitJsonTests(unittest.TestCase):
    def test_real_cli_receipt_is_one_json_document_and_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, '-m', 'launcher', '--config-dir', directory,
                       'submit', '--id', 'hub-contract', '--key', 'contract-key',
                       '--payload', json.dumps({'goal': 'Read-only contract check',
                                                 'owner': 'test', 'cwd': directory,
                                                 'timeout': 60}), '--json']
            for _ in range(2):
                result = subprocess.run(command, capture_output=True, text=True, check=True,
                                        cwd=Path(__file__).resolve().parents[1])
                self.assertEqual(json.loads(result.stdout),
                                 {'task_id': 'hub-contract', 'state': 'queued'})


if __name__ == '__main__':
    unittest.main()
