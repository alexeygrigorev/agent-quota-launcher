"""Offline status-parser regressions; fixtures are never admission evidence."""
import json
import subprocess
import unittest
from unittest.mock import patch

from launcher import native_lifecycle as FIXED

BINDING = dict(native_id='11111111-2222-4333-8444-555555555555', tag='offline-fixture',
               workspace='/fixture/native-status', engine='codex', incarnation=1234)

def document(**changes):
    row = dict(id=BINDING['native_id'], tag=BINDING['tag'], workspace=BINDING['workspace'],
               engine=BINDING['engine'], created_at_ms=BINDING['incarnation'], phase='exited',
               containment_empty=True, command=['fixture', 'transport stale socket'])
    row.update(changes)
    return row

def probe(module, info=None, *, rc=0, stdout=None, stderr=''):
    result = subprocess.CompletedProcess(['aplexer', 'status', BINDING['native_id'], '--json'],
                                         rc, json.dumps(info) if stdout is None else stdout, stderr)
    with patch.object(module.subprocess, 'run', return_value=result), \
         patch.object(module, 'read_tombstone', return_value=None):
        return module.probe_native_lifecycle(BINDING, live_info={})[0]

class NativeStatusDiagnosticsTests(unittest.TestCase):
    def test_successful_command_prose_does_not_become_transport_failure(self):
        for text in ('transport', 'stale socket', 'transport stale socket'):
            with self.subTest(text=text):
                info = document(command=['fixture', text])
                self.assertEqual(probe(FIXED, info), 'dead')

    def test_successful_other_prose_fields_are_not_error_output(self):
        info = document(command=['fixture'], note='transport', details={'goal': 'stale socket'})
        self.assertEqual(probe(FIXED, info), 'dead')

    def test_real_stderr_errors_stay_unknown_even_with_successful_json(self):
        for error in ('transport failed', 'stale socket'):
            with self.subTest(error=error):
                self.assertEqual(probe(FIXED, document(), stderr=error), 'unknown')

    def test_nonzero_stdout_errors_stay_unknown(self):
        for error in ('transport failed', 'stale socket'):
            with self.subTest(error=error):
                self.assertEqual(probe(FIXED, rc=1, stdout=error), 'unknown')

    def test_malformed_successful_json_stays_unknown(self):
        self.assertEqual(probe(FIXED, stdout='not JSON'), 'unknown')

    def test_all_identity_checks_remain_fail_closed(self):
        changes = dict(id='99999999-2222-4333-8444-555555555555', tag='other',
                       workspace='/fixture/other', engine='other', created_at_ms=1235)
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertEqual(probe(FIXED, document(**{field: value})), 'unknown')

    def test_terminal_requires_explicit_true_containment(self):
        for value in (False, None, 'true', 1):
            with self.subTest(value=value):
                self.assertEqual(probe(FIXED, document(containment_empty=value)), 'unknown')

    def test_active_phase_is_never_declared_dead(self):
        for phase in ('running', 'working', 'starting'):
            for empty in (False, True):
                with self.subTest(phase=phase, empty=empty):
                    self.assertEqual(probe(FIXED, document(phase=phase, containment_empty=empty)), 'alive')

    def test_absence_without_authoritative_tombstone_is_unknown(self):
        self.assertEqual(probe(FIXED, rc=1, stdout='session not found'), 'unknown')

    def test_unknown_phase_stays_unknown(self):
        self.assertEqual(probe(FIXED, document(phase='unobserved')), 'unknown')

    def test_windows_accepts_temp_workspace_in_status_and_tombstone(self):
        win_binding = dict(
            BINDING,
            workspace=r'C:\Users\User\work\agent-hub',
        )
        win_info = document(
            workspace=r'C:\Users\User\AppData\Local\Temp',
            phase='running',
            containment_empty=False,
        )
        result = subprocess.CompletedProcess(
            ['aplexer', 'status', BINDING['native_id'], '--json'],
            0, json.dumps(win_info), '',
        )
        with patch.object(FIXED, 'sys') as mock_sys, \
             patch.object(FIXED.subprocess, 'run', return_value=result), \
             patch.object(FIXED, 'read_tombstone', return_value=None):
            mock_sys.platform = 'win32'
            self.assertEqual(FIXED.probe_native_lifecycle(win_binding, live_info={})[0], 'alive')
            mock_sys.platform = 'linux'
            self.assertEqual(FIXED.probe_native_lifecycle(win_binding, live_info={})[0], 'unknown')

        tombstone = dict(
            id=BINDING['native_id'],
            tag=BINDING['tag'],
            workspace=r'C:\Users\User\AppData\Local\Temp',
            cause='finished',
            finished_at_ms=2000,
        )
        with patch.object(FIXED, 'sys') as mock_sys:
            mock_sys.platform = 'win32'
            ok, _ = FIXED.verify_tombstone(
                tombstone,
                expected_id=BINDING['native_id'],
                expected_tag=BINDING['tag'],
                expected_workspace=r'C:\Users\User\work\agent-hub',
                expected_incarnation=1234,
            )
            self.assertTrue(ok)
            mock_sys.platform = 'linux'
            ok_linux, _ = FIXED.verify_tombstone(
                tombstone,
                expected_id=BINDING['native_id'],
                expected_tag=BINDING['tag'],
                expected_workspace=r'C:\Users\User\work\agent-hub',
                expected_incarnation=1234,
            )
            self.assertFalse(ok_linux)

if __name__ == '__main__':
    unittest.main()
