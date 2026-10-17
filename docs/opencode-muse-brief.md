# OpenCode Go Muse Spark route (platform-muse-brief slice)

Exact source/config contract for head integration. No production launches by worker.

## Source
- `launcher/admission.py`: `ADAPTER_ROUTES` includes `opencode`; `ADAPTER_MODELS["opencode"] = "opencode-go/muse-spark-1.3-contributor"`; `ADAPTER_SUPPORTED_MODELS["opencode"] = ("opencode-go/muse-spark-1.3-contributor",)`; `PROVIDER_TASK_KINDS["opencode"]` = general+root; `_validate_route` maps quse keys `go`, `opencode`, `opencode-go` to provider `opencode` (standard windows validation, fail closed; free `opencode/*-free` never mapped).
- `launcher/launch.py`: `OPENCODE_BIN = "/home/alexey/.nvm/versions/node/v24.13.1/bin/opencode"` (1.18.31); `OPENCODE_MODEL = "opencode-go/muse-spark-1.3-contributor"`; `ADAPTERS["opencode"] = {"argv": [OPENCODE_BIN, "run", "--pure", "--auto", "--model", OPENCODE_MODEL], "env": {}}`; `validate_opencode_runtime()` (exists+executable, fail closed); `build_adapter_argv("opencode", goal, model)` returns exact argv + `[goal]` as separate element (no shell interpolation), refuses any non-pinned model and any GLM on opencode; `do_run` adds `--engine opencode` for opencode provider only (genuine native identity; others unchanged).
- `launcher/cli.py` (narrow): `run --backend aplexer` on success (do_run rc 0 + launch record outcome running + first_action_validated_at) prints `{"backend":"aplexer","task_id":...,"receipt":{"session_id":<native UUID>,"state":"running","first_action_verified":true,"tag":...,"workspace":...,"engine":...,"phase":...}}` to stdout for Hub fleet `run_json` binding. Missing/invalid evidence prints nothing (caller treats as uncertain, never fake). Exit-code and launch-record semantics preserved; task-units path untouched.

## Config
- Hub fleet subscription for Muse brief: `service` e.g. `go` (or explicit), `engine: opencode`, `account_label` as configured, `admission_argv` = maintained fresh gate, `launcher_provider: opencode`, `launcher_model: opencode-go/muse-spark-1.3-contributor`, `launcher` = `{argv: [python3 -m launcher], root: <launcher checkout>, config_dir: <launcher config>}`.
- Hub fleet headless for opencode must use `--backend aplexer` (real UUID + first-action), NOT task-units (unit_name cannot bind/control M12). `plan` must show eligible provider opencode / model muse-spark-1.3-contributor with account/profile match; otherwise refused.
- Quota: fresh `quse go` windows (5h/7d/monthly, e.g. 54%/58%/55% on 2026-10-17) via `validate_quse`; resources via `check_resources` (1500M, 10G RAM retain, 50G disk, TMPDIR under repo .local/tmp); model via `opencode models` containing the exact model + `validate_opencode_runtime`; account/profile plan checks preserved (no fabricated health, no fallback to paid model).

## Usage (head-owned production)
```bash
python3 -m launcher --config-dir <cfg> submit --id <task> --key <key> \
  --payload '{"goal":"<arbitrary brief>","owner":"hub-fleet","cwd":"<ws>","timeout":1800,"model_requirements":{"providers":["opencode"],"models":["opencode-go/muse-spark-1.3-contributor"]}}'
python3 -m launcher --config-dir <cfg> plan --id <task>   # eligible opencode required
python3 -m launcher --config-dir <cfg> run --backend aplexer --id <task> --cwd <ws> --tmpdir <ws>/.local/tmp/<task>
# success stdout JSON receipt has session_id (native UUID), state running, first_action_verified true, engine opencode
```

## Tests
- `tests/test_opencode_route.py` (16 tests): admission mapping, model pin, exact argv + separate prompt, refusals, runtime checks, do_run --engine wiring, cli receipt positive/negative.
- Existing `tests/test_admission.py` narrow update (go removed from unsupported-blocked; opencode covered in new file), `tests/test_launch.py` untouched and green (108 tests).
