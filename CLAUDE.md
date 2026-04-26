# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Stack

Prefect 3 orchestrates a code-modification pipeline. The LLM-driven phases run inside the Prefect worker process via the **OpenHands V1 SDK** (`openhands-sdk` + `openhands-tools`) — no per-task agent containers, no aider. Test gates run in separate ephemeral runner containers. An external Ollama server (typically on the Windows host in WSL2 setups) provides the LLM. A thin FastAPI orchestrator translates HTTP → `run_deployment("atelier/default", ...)`. README.md is the canonical overview; this file captures only the non-obvious bits.

## Common commands

```bash
# Bring up everything (server, worker, orchestrator, builds the runner images)
docker compose up -d --build

# Watch the worker (where the OpenHands sessions actually run)
docker compose logs -f prefect-worker

# Drop a target repo under ./projects/<repo-name> (must contain .git).
# Containers run as HOST_UID:HOST_GID, so files stay owned by you — no
# chown dance required.

# Fire a task (use the UI at :8000 or POST /tasks with a JSON body)
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "...", "repo_path": "<repo-name>", "base_branch": "main"}'
# Prefect UI:  http://localhost:4200
# Markdown log per task:    logs/task-<id>.md
# OpenHands event stream:   logs/task-<id>.events.jsonl
```

### Rebuilds after code changes

The compose file bakes code into images; edits on disk are NOT picked up live.

- Flow / orchestrator / agents code (`flows/`, `orchestrator/`, `agents/`) → `docker compose build prefect-worker orchestrator && docker compose up -d --force-recreate prefect-worker orchestrator`. The worker image is the one that actually runs OpenHands; it copies the full `agents/` tree and pip-installs `openhands-sdk` / `openhands-tools` from `docker/worker-requirements.txt`.
- Runner test images → `docker compose build runner-quick-builder runner-e2e-builder`

There are no unit tests in this repo; the "tests" are full pipeline runs against whatever target repo the user drops under `./projects/`.

## Architecture — the non-obvious parts

### Two-image pattern: worker vs. runners

`prefect-worker` runs the flow code AND embeds the OpenHands SDK so LLM phases execute in-process. The per-test work happens inside **separately-built images** (`atelier-runner-quick`, `atelier-runner-e2e`) that the worker launches via `/var/run/docker.sock` mounted from the host. The `*-builder` services in `docker-compose.yml` exist only to build these runner images at `compose up` — they run `entrypoint: ["true"]` and exit immediately; the worker's `depends_on: service_completed_successfully` gates on them. Do not add runtime commands to those builder services.

### Path-aliased mounts (no host/container duality)

`projects/`, `worktrees/`, `logs/` and `data/` are bind-mounted at the **same absolute path** inside every container as on the host (e.g. `/home/me/atelier/worktrees/task-42` resolves identically from the host shell, the worker, and the runner containers). This means:

- Anything `git worktree add` writes (the `.git` pointer files) resolves from both sides — the user can `cd worktrees/task-X && git log/diff/commit` from their host shell with no indirection.
- The worker passes the same path as both the volume source and the container target when spawning runner containers (see `orchestrator/runner.py`).
- The translator helpers in `orchestrator/config.py` (`container_path_for_worktree` / `host_path_for_worktree` / `host_path_for_project`) are kept as identity functions for code clarity at call sites that historically distinguished host vs container paths.

This setup assumes `docker compose` is run from the repo root so `${PWD}` substitution in `docker-compose.yml` produces the right paths. `scripts/setup.sh` already enforces this.

### The pipeline flow and its retry loop (`flows/pipeline.py`)

Linear V2 phases (default spec): `create-worktree → load-config → install-deps → agent-session → quick-tests → full-tests → e2e-setup → e2e-tests → e2e-teardown → preview`. The user can also opt in to a `simplify-pass` block (see the canvas in the UI). The three **deterministic test gates** (quick-tests, full-tests, e2e-tests) loop the whole attempt back to `agent-session` with the test output as `previous_feedback`. The for/else in `pipeline_flow` bounds retries at `MAX_RETRY_ATTEMPTS + 1` attempts; failures after the last attempt raise `RuntimeError` to mark the flow failed. `e2e-teardown` is wrapped in try/finally so it always runs.

There is no separate reviewer phase: the OpenHands session iterates internally to self-correct. The `changes_requested` verdict from V1 is gone.

### OpenHands wrapper (`agents/openhands_session.py`)

A single `run(task, mode, api_key)` function builds an OpenHands `LLM` + `Agent` (with `TerminalTool`, `FileEditorTool`, `TaskTrackerTool`) and runs a `Conversation` with `workspace=task.worktree_path`. The conversation runs in the WORKER PROCESS — `runtime=local`, no Docker-in-Docker. Sandboxing comes from `branch_guard.py` (refs snapshot before/after) and the path-aliased worktree.

Two modes:

- `mode="implement"`: drives the `agent-session` block. Requires the working tree to be dirty when the run finishes — empty diff is a hard failure.
- `mode="simplify"`: drives the (opt-in) `simplify-pass` block. A no-op (empty diff) is allowed and yields a success without a commit.

After the conversation, the wrapper does the `git add -A && git commit` itself (OpenHands doesn't auto-commit). Every attempt that produces a diff yields a commit on the feature branch.

The conversation `callbacks=[...]` subscribe a writer that streams every `Event` to `logs/task-<id>.events.jsonl` (append-only). Multiple sessions for the same task share that file — implement+simplify and any retried implement attempts produce a single chronological event stream. The events are persisted but **not yet rendered in the UI** (planned for v2.1).

The model **must support tool-calling** (qwen3.6, qwen2.5-coder, devstral, llama3.1+, mistral …). Models without tool support fail at the first action. The forms in the UI surface a reminder.

### Block catalog ↔ flow ↔ wrapper

The single source of truth for valid pipeline blocks is `BLOCK_CATALOG` in `orchestrator/main.py`. The flow's `_VALID_BLOCK_TYPES` and `_DEFAULT_PIPELINE_SPEC` in `flows/pipeline.py` must stay aligned with that catalog. The Drawflow canvas in the UI fetches the catalog from `/ui/pipeline/blocks` — there is no JS hardcoding of block types, so adding a new block means: add it to `BLOCK_CATALOG`, add a handler in `_run_step` in `flows/pipeline.py`, done.

### E2E setup/teardown runs on the worker, NOT in the runner

`orchestrator/services.py` runs `docker compose up/down` for E2E services directly from the worker (which has the Docker socket), with `cwd=<host_worktree>`. Runners deliberately do not get the socket. If a user's `.atelier.yml` needs Docker-in-Docker for tests themselves, that's a new capability — don't "fix" it by mounting the socket into the runner.

### `.atelier.yml` lives in the TARGET repo

The config that drives test gates (`install`, `quick_tests`, `full_tests`, `e2e_tests`, `preview`) is a file the user places at the root of THEIR repo — not in this pipeline repo. See `.atelier.yml.example`. Absence of the file is not an error: `load-config` returns `None` and all gates `skipped`. Absence of an individual section skips just that gate.

## Gotchas

- **Ollama host URL**: the pipeline talks to Ollama running on the Windows host, not in compose. `OLLAMA_EXTERNAL_URL` in `.env` must match the WSL→Windows gateway (`ip route show | grep -i default | awk '{ print $3 }'`) and can drift between reboots.
- **Tool-calling requirement**: OpenHands requires the model to support function/tool calling via the OpenAI-compatible endpoint. Local models that DON'T support it (some older LLaMA variants, gemma without the `:instruct` flavour, …) fail at the first action with a 400 from the LLM endpoint. Stick to `qwen3.6:latest`, `qwen2.5-coder:32b`, `devstral`, `llama3.1+` family.
- **HOST_UID/HOST_GID**: all containers (worker, orchestrator, runners) run with the host user's UID/GID (baked at build time via `ARG HOST_UID/HOST_GID`, and enforced at runtime via `user:` in compose and `user=` in `containers.create()`). `scripts/setup.sh` auto-fills these in `.env`. The worker gets the docker socket via `group_add: [${DOCKER_GID}]`. If you change `HOST_UID`/`HOST_GID`, you must rebuild the images.
- **Repo path sandbox**: `orchestrator/main.py::_resolve_repo_path` hard-requires the resolved path to be under `PROJECTS_ROOT` (host's `${PWD}/projects` by default) and contain `.git`. Don't add knobs to bypass this.
- **`runner-bundle/` directory**: this is a standalone "addon package" with its own README — a drop-in bundle showing what was added to bring test gates into the project. The canonical copies of those files already live in `orchestrator/`, `flows/`, `agents/`, `docker/`; edit those, not the bundle.
- **Empty branch reuse**: `create_worktree` force-deletes a pre-existing feature branch (`git branch -D`) before recreating the worktree. Tasks are treated as ephemeral — don't assume the branch from a previous run survives.
- **The `from prefect import flow` import at the very top of `orchestrator/main.py`** is a workaround for a Prefect 3 circular-import bug; don't "tidy" it down into the rest of the imports.
- **`OPENHANDS_MAX_ITERATIONS`** caps the iterations of a single session. Each iteration is roughly one LLM call + one tool execution; with a slow local model this caps wall-clock. Default is 30 (set in `docker-compose.yml`); raise via `.env` if your tasks are complex and the model is fast enough.
