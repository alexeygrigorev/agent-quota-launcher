import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from launcher.launch import build_adapter_argv
from launcher.task_units import (
    execute_transient_task_unit,
    extract_telemetry_events,
    parse_tool_events,
)


class TestStructuredTelemetry(unittest.TestCase):
    def test_antigravity_adapter_uses_stream_json(self):
        argv = build_adapter_argv("antigravity", "test goal")
        self.assertIn("--output-format", argv)
        fmt_idx = argv.index("--output-format")
        self.assertEqual(argv[fmt_idx + 1], "stream-json")
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "test goal")

    def test_extract_and_parse_genuine_tool_events(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout_path = Path(tmp_dir) / "test-stdout.log"
            raw_lines = [
                json.dumps({"event": "init", "conversation_id": "c-123", "init": {"tools": ["run_command", "write_to_file"]}}),
                json.dumps({"event": "step_update", "step_update": {"conversation_id": "c-123", "step_index": 0, "state": "DONE", "step_type": "user_input"}}),
                json.dumps({"event": "step_update", "step_update": {"conversation_id": "c-123", "step_index": 1, "state": "DONE", "step_type": "agent_response", "duration_seconds": 1.5}}),
                json.dumps({
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "c-123",
                        "step_index": 2,
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "write_to_file",
                        "duration_seconds": 0.05,
                        "tool_info": {"name": "write_to_file", "parameters": {"TargetFile": "/path/to/artifact.md", "CodeContent": "hello"}},
                    },
                }),
                json.dumps({
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "c-123",
                        "step_index": 3,
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "duration_seconds": 0.2,
                        "tool_info": {"name": "run_command", "parameters": {"CommandLine": "python3 -c 'print(1)'"}},
                    },
                }),
                json.dumps({
                    "event": "result",
                    "result": {
                        "conversation_id": "c-123",
                        "status": "SUCCESS",
                        "response": "done",
                        "duration_seconds": 2.0,
                        "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                    },
                }),
            ]
            stdout_path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

            events = extract_telemetry_events(stdout_path)
            self.assertEqual(len(events), 6)

            tools, raw_count = parse_tool_events(events)
            self.assertEqual(len(tools), 2)
            self.assertEqual(raw_count, 2)
            self.assertEqual(tools[0]["tool_name"], "write_to_file")
            self.assertEqual(tools[0]["state"], "DONE")
            self.assertEqual(tools[0]["duration_seconds"], 0.05)
            self.assertEqual(tools[0]["tool_info"]["parameters"]["TargetFile"], "/path/to/artifact.md")

            self.assertEqual(tools[1]["tool_name"], "run_command")
            self.assertEqual(tools[1]["duration_seconds"], 0.2)

    def test_no_synthesized_events_on_plain_text(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout_path = Path(tmp_dir) / "plain-stdout.log"
            stdout_path.write_text(
                "Running task unit...\nWriting output file\nFinished with exit 0\n",
                encoding="utf-8",
            )
            events = extract_telemetry_events(stdout_path)
            self.assertEqual(events, [])
            tools, raw_count = parse_tool_events(events)
            self.assertEqual(tools, [])
            self.assertEqual(raw_count, 0)

    def test_execute_transient_task_unit_preserves_telemetry_alongside_artifact(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir) / "workspace"
            workspace.mkdir()
            tmpdir = workspace / ".local" / "tmp"
            tmpdir.mkdir(parents=True)
            log_dir = Path(tmp_dir) / "logs"
            log_dir.mkdir()

            task_id = "scale50-sample"
            stdout_log = log_dir / f"{task_id}-stdout.log"
            stream_events = [
                {"event": "init", "conversation_id": "c-sample", "init": {"tools": ["write_to_file"]}},
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "c-sample",
                        "step_index": 1,
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "write_to_file",
                        "duration_seconds": 0.12,
                        "tool_info": {"name": "write_to_file", "parameters": {"TargetFile": "artifact.md"}},
                    },
                },
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "c-sample",
                        "status": "SUCCESS",
                        "response": "completed",
                        "duration_seconds": 1.2,
                        "usage": {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
                    },
                },
            ]

            # Mock systemd execution to write stdout log and simulate clean exit 0
            def fake_popen(*args, **kwargs):
                stdout_log.write_text(
                    "\n".join(json.dumps(e) for e in stream_events) + "\n",
                    encoding="utf-8",
                )
                mock_proc = MagicMock()
                mock_proc.communicate.return_value = ("Started unit agent-task-scale50-sample.service.\n", "")
                mock_proc.returncode = 0
                return mock_proc

            from datetime import datetime, timezone, timedelta
            future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            quse = {
                "antigravity": {
                    "status": "ok",
                    "windows": {"7d": {"percent_remaining": 90, "reset_at": future}},
                    "details": {"has_grok_code_access": True},
                }
            }

            with patch("launcher.task_units.subprocess.Popen", side_effect=fake_popen), \
                 patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {"ControlGroup": "/app.slice/x"})), \
                 patch("launcher.task_units.check_resources", return_value=True):
                receipt = execute_transient_task_unit(
                    task_id=task_id,
                    command_argv=["/bin/true"],
                    memory_mb=768,
                    workspace=str(workspace),
                    tmpdir=str(tmpdir),
                    timeout_sec=30,
                    quse_json=quse,
                    provider="antigravity",
                    log_dir=str(log_dir),
                )

            self.assertEqual(receipt["exit_code"], 0)
            self.assertEqual(receipt["tool_calls_count"], 1)
            self.assertIsNotNone(receipt["first_tool"])
            self.assertEqual(receipt["first_tool"]["tool_name"], "write_to_file")
            self.assertEqual(receipt["model_status"], "SUCCESS")
            self.assertEqual(receipt["usage"]["total_tokens"], 600)

            # Verify telemetry file is preserved directly in workspace alongside final artifact
            workspace_telemetry = workspace / f"{task_id}-telemetry.jsonl"
            self.assertTrue(workspace_telemetry.is_file())
            saved_lines = [json.loads(l) for l in workspace_telemetry.read_text().splitlines() if l.strip()]
            self.assertEqual(len(saved_lines), 3)
            self.assertEqual(saved_lines[1]["step_update"]["tool_name"], "write_to_file")


if __name__ == "__main__":
    unittest.main()
