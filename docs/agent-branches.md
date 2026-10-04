# Agent Branches development and ordinary Git recovery

The launcher has a real isolated local Agent Branches project. Its canonical Git remote is named `platform`; a task fork is registered as `launcher-v01` in the private `.local/platform-task-launcher-v01.json` file. GitHub is an independent private main mirror.

From `/home/alexey/git/agent-quota-launcher`, after committing only your owned accepted source under the `.local/git.lock` lock:

```sh
python3 scripts/platform.py push-task launcher-v01
python3 scripts/platform.py checkpoint
python3 scripts/backup.py
```

The first command pushes to the real task fork with its per-task token and verifies the coordinator's observed SHA. The second advances and verifies canonical main. The third requires a private GitHub repository, pushes main without force, checks its remote SHA, clones an independent checkout and runs `git fsck`. Private verification receipts are in `.local/platform-push-launcher-v01.json`, `.local/platform-checkpoint.json` and `.local/backup-evidence.json`. A clean command exit is insufficient if these SHA checks do not agree.

The prototype source is pinned at `f58227c7794751c26238b3988583d3c3e9273b47`. The owned snapshot in `.local/platform-source/prototype` was compiled using the existing TypeScript compiler without dependency installation or Rust build. Two services run through aplexer: `quota-platform-sidecar` on `127.0.0.1:8848` and `quota-platform-coordinator` on `127.0.0.1:8849`, each capped at 512MiB. They use separate private state, preserve the existing Agent Branches development instances, and introduce no public listener. Services are separate from model worker counts.

New deployment (only with a pinned compiled prototype and free ports):

```sh
python3 scripts/platform.py provision --prototype /absolute/pinned/prototype --source-commit PINNED_SHA --port 8848
python3 scripts/platform.py checkpoint
python3 scripts/platform.py task launcher-v01 'Implement bounded admission and native task execution'
```

The first canonical checkpoint imports only the exact recorded prototype seed (one known README placeholder) while preserving both histories. Any other unrelated canonical state is refused. No force push, reset or peer-worktree deletion is needed. Task creation writes a pending marker before its API call; an ambiguous result requires examining `/status`, not blindly retrying and creating a second fork. This helper currently does not automate ambiguous task reconciliation or expired-token reminting.

Credentials are generated locally, stored in `.local` with mode600, and excluded from Git. Never print those JSON documents. Git receives scoped authorization through process environment rather than a credential-bearing URL or command argument. A fresh restore intentionally has no platform credentials. Task tokens expire; preserve work and ask the project head to reconcile/remint the required route rather than turning authentication failure into an empty or successful result.

The private mirror is [alexeygrigorev/agent-quota-launcher](https://github.com/alexeygrigorev/agent-quota-launcher). If the experimental platform is unavailable, clone its GitHub main and continue in ordinary Git with existing task/file ownership. Verify `git rev-parse HEAD` against the saved independent checkpoint; the prototype's own second branch is not an independent backup. Do not point recovery at an unrelated existing checkout or overwrite uncommitted work. `scripts/backup.py` retains each small disposable restore for inspection.

Initial recovery was actually exercised at `003ade0c500eeabf491e12892006141263ea8542`, before launcher core delivery. Repeat the three-command sequence after final review so the canonical main, GitHub main and independently restored source all match the accepted implementation. Unit tests and dry-run admission do not prove a provider launched; preserve the separate native first-action and outcome evidence.
