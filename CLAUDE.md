# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Stack

Prefect 3 orchestrates a multi-agent code-modification pipeline. Agents (implementer / reviewer / simplifier) run as ephemeral Docker containers launched by a Prefect worker. Test gates run in separate runner containers. An external Ollama server (on the Windows host) provides the LLM. A thin FastAPI orchestrator translates HTTP → `run_deployment("pipeline/default", ...)`. README.md is the canonical overview; this file captures only the non-obvious bits.

## Common commands

```bash
# Bring up everything (server, worker, orchestrator, and builds the three satellite images)
docker compose up -d --build

# Watch the worker (where flows actually run)
docker compose logs -f prefect-worker

# Prepare a throwaway target repo under ./repos/target-repo
bash scripts/init-test-repo.sh
sudo chown -R 1000:1000 repos worktrees logs   # worker runs as UID 1000

# Fire a task
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d @tasks/example.json
# Prefect UI:  http://localhost:4200
# Markdown log per task: logs/task-<id>.md
```

### Rebuilds after code changes

The compose file bakes code into images; edits on disk are NOT picked up live.

- Flow / orchestrator python code (`flows/`, `orchestrator/`) → `docker compose build prefect-worker orchestrator && docker compose up -d --force-recreate prefect-worker orchestrator`
- Agent prompts or `agents/` code → `docker compose build agent-builder` (the builder is a one-shot that just produces `pipeline-agent:latest`; the worker `docker run`s that image per agent invocation)
- Runner test images → `docker compose build runner-quick-builder runner-e2e-builder`

There are no unit tests in this repo; the "tests" are full pipeline runs against `repos/target-repo/`.

## Architecture — the non-obvious parts

### Two-image pattern: worker vs. agent/runner

`prefect-worker` runs the flow code, but the per-agent and per-test work happens inside **separately-built images** (`pipeline-agent`, `pipeline-runner-quick`, `pipeline-runner-e2e`) that the worker launches via `/var/run/docker.sock` mounted from the host. The `*-builder` services in `docker-compose.yml` exist only to build these images at `compose up` — they run `entrypoint: ["true"]` and exit immediately; the worker's `depends_on: service_completed_successfully` gates on them. Do not add runtime commands to those builder services.

### Host/container path dualism

Because the worker launches containers on the host's Docker daemon, every bind-mounted path needs BOTH:
- the path as the worker sees it (e.g. `/app/worktrees/task-42`)
- the path as the host sees it (e.g. `/home/user/pipelines/.../worktrees/task-42`)

`orchestrator/config.py` exposes both via `container_path_for_worktree()` / `host_path_for_worktree()`. `WorktreeHandle` carries both. When launching an agent or runner, pass the **host** path as the volume source and the **container** path for any git/python operations the worker itself does. Passing the wrong one silently mounts nothing useful.

### The pipeline flow and its retry loop (`flows/pipeline.py`)

Linear phases: `create-worktree → load-config → install-deps → implementer → quick-tests → reviewer → simplifier → full-tests → e2e-setup → e2e-tests → e2e-teardown`. Three **deterministic gates** (quick-tests, full-tests, e2e-tests) and the reviewer's `changes_requested` verdict can each loop the whole attempt back to the implementer with `previous_feedback`. The for/else in `pipeline_flow` bounds retries at `PIPELINE_MAX_RETRY_ATTEMPTS + 1` attempts; failures after the last attempt raise `RuntimeError` to mark the flow failed. `e2e-teardown` is wrapped in try/finally so it always runs.

### Orchestrator ↔ agent contract

The worker does NOT stream anything into the agent container. It writes `TaskInput` JSON to `<worktree>/.task-input.json`, launches the container with `AGENT_ROLE` / `TASK_INPUT_FILE` env vars, and parses the **last `{`-starting line of stdout** as an `AgentResult` (see `_parse_agent_output` in `orchestrator/container.py`). `agents/entrypoint.py` deletes the input file before running the agent so it doesn't contaminate commits. If you change `agents/models.py` (the shared contract), rebuild BOTH `agent-builder` and `prefect-worker` — the worker has its own copy.

### Implementer is special

`agents/implementer.py` shells out to `aider` (with `--yes-always --no-auto-commits`) instead of using the ReAct loop in `agents/base.py`. Reviewer and simplifier use `BaseAgent` + `agents/tools/code_tools.py` tools + an Ollama chat client. Implementer then does its own `git add -A && git commit --allow-empty` so every attempt produces a commit even when Aider no-ops.

### E2E setup/teardown runs on the worker, NOT in the runner

`orchestrator/services.py` runs `docker compose up/down` for E2E services directly from the worker (which has the Docker socket), with `cwd=<host_worktree>`. Runners deliberately do not get the socket. If a user's `.pipeline-ia.yml` needs Docker-in-Docker for tests themselves, that's a new capability — don't "fix" it by mounting the socket into the runner.

### `.pipeline-ia.yml` lives in the TARGET repo

The config that drives test gates (`install`, `quick_tests`, `full_tests`, `e2e_tests`) is a file the user places at the root of THEIR repo — not in this pipeline repo. See `.pipeline-ia.yml.example`. Absence of the file is not an error: `load-config` returns `None` and all gates `skipped`. Absence of an individual section skips just that gate.

## Gotchas

- **Ollama host URL**: the pipeline talks to Ollama running on the Windows host, not in compose. `OLLAMA_EXTERNAL_URL` in `.env` must match the WSL→Windows gateway (`ip route show | grep -i default | awk '{ print $3 }'`) and can drift between reboots.
- **UID 1000**: the worker runs as 1000, so `repos/`, `worktrees/`, `logs/` must be owned by 1000 or git operations will fail with "dubious ownership". The worker image sets `safe.directory '*'` as a backstop but permissions still bite on writes.
- **Repo path sandbox**: `orchestrator/main.py::_resolve_repo_path` hard-requires the resolved path to be under `/repos` and contain `.git`. Don't add knobs to bypass this.
- **`pipeline-runner/` directory**: this is a standalone "addon package" with its own README — a drop-in bundle showing what was added to bring test gates into the project. The canonical copies of those files already live in `orchestrator/`, `flows/`, `agents/`, `docker/`; edit those, not the bundle.
- **Empty branch reuse**: `create_worktree` force-deletes a pre-existing feature branch (`git branch -D`) before recreating the worktree. Tasks are treated as ephemeral — don't assume the branch from a previous run survives.
- **The `from prefect import flow` import at the very top of `orchestrator/main.py`** is a workaround for a Prefect 3 circular-import bug; don't "tidy" it down into the rest of the imports.
