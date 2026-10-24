import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher.admission import _eval_task_fit
from launcher.launch import build_adapter_argv, build_managed_profile_argv, native_command_model, do_run
from launcher.store import Store


class ModelContractTests(unittest.TestCase):
    def test_deep_native_argv(self):
        with patch('launcher.launch.get_codex_wrapper', return_value='/protected/launch-codex.sh'):
            for provider, model in [('codex', 'gpt-6-astra'), ('antigravity', 'gemini-4-argon-high')]:
                self.assertEqual(_eval_task_fit(provider, {'model': model}), (1.0, model))
                self.assertEqual(native_command_model(build_adapter_argv(provider, 'goal', model)), model)
        with patch('launcher.launch.resolve_hub_path', return_value='/trusted/host'):
            for provider, model in [('codex', 'gpt-6-astra'), ('claude', 'opus'), ('gemini', 'gemini-4-argon-high')]:
                self.assertEqual(native_command_model(build_managed_profile_argv(provider, 'goal', 'test', model)), model)

    def test_no_unsupported_model_fallback_fit(self):
        self.assertEqual(_eval_task_fit('opencode', {'model': 'gemini-4-argon-high'})[0], 0.0)
        self.assertIsNone(native_command_model(['native', '--model', 'deep', '--model', 'daily']))
        self.assertIsNone(native_command_model('native --model deep'))

    def test_native_mismatch_never_runs_even_with_first_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'state.db')
            store = Store(db)
            store.submit_task('test', 'test', {'owner': 'test', 'cwd': tmp, 'timeout': 60}, [])
            with patch('launcher.launch.fetch_quse'), patch('launcher.launch.validate_quse', return_value=([{'provider': 'antigravity'}], {})), \
                 patch('launcher.launch.select_candidate', return_value=({'provider': 'antigravity', 'model': 'gemini-4-argon-high'}, {})), \
                 patch('launcher.launch.check_resources'), patch('launcher.launch.validate_first_action', return_value=True) as first, \
                 patch('launcher.launch.subprocess.Popen') as popen:
                popen.return_value.returncode = 0
                popen.return_value.communicate.return_value = (json.dumps(dict(id='native', command=['agy', '--model', 'gemini-3.8-flash-high'])), '')
                self.assertEqual(do_run(db, 'test', tmp, None, str(Path(tmp) / 'lock')), 1)
                first.assert_not_called()
            self.assertEqual(store.get_task('test')['state'], 'launch-uncertain')
            record = json.loads((Path(tmp) / '.local/launch-test.json').read_text())
            self.assertFalse(record['model_verified'])
            self.assertEqual(record['observed_model'], 'gemini-3.8-flash-high')


    def test_pure_model_capability_query(self):
        from argparse import Namespace
        from contextlib import redirect_stdout
        from io import StringIO
        from launcher.cli import model_support
        for provider, model, expected in [('antigravity', 'gemini-4-argon-high', True),
                                          ('codex', 'gpt-6-astra', True),
                                          ('opencode', 'opencode-go/deepseek-v4.1-flash', False)]:
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(model_support(Namespace(provider=provider, model=model)), 0)
            receipt = json.loads(out.getvalue())
            self.assertIs(receipt['supported'], expected)
            if not expected:
                self.assertEqual(receipt['reason'], 'model_unsupported')
