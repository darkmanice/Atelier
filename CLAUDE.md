# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Stack

Prefect 3 orchestrates a multi-agent code-modification pipeline. Agents (implementer / reviewer / simplifier) run as ephemeral Docker containers launched by a Prefect worker. Test gates run in separate runner containers. An external Ollama server (on the Windows host) provides the LLM. A thin FastAPI orchestrator translates HTTP → `run_deployment("atelier/default", ...)`. README.md is the canonical overview; this file captures only the non-obvious bits.

## Common commands

```bash
# Bring up everything (server, worker, orchestrator, and builds the three satellite images)
docker compose up -d --build

# Watch the worker (where flows actually run)
docker compose logs -f prefect-worker

# Drop a target repo under ./projects/<repo-name> (must contain .git).
# Containers run as HOST_UID:HOST_GID, so files stay owned by you — no
# chown dance required.

# Fire a task (use the UI at :8000 or POST /tasks with a JSON body)
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "...", "repo_path": "<repo-name>", "base_branch": "main"}'
# Prefect UI:  http://localhost:4200
# Markdown log per task: logs/task-<id>.md
```

### Rebuilds after code changes

The compose file bakes code into images; edits on disk are NOT picked up live.

- Flow / orchestrator python code (`flows/`, `orchestrator/`) → `docker compose build prefect-worker orchestrator && docker compose up -d --force-recreate prefect-worker orchestrator`
- Agent prompts or `agents/` code → `docker compose build agent-builder` (the builder is a one-shot that just produces `atelier-agent:latest`; the worker `docker run`s that image per agent invocation)
- Runner test images → `docker compose build runner-quick-builder runner-e2e-builder`

There are no unit tests in this repo; the "tests" are full pipeline runs against whatever target repo the user drops under `./projects/`.

## Architecture — the non-obvious parts

### Two-image pattern: worker vs. agent/runner

`prefect-worker` runs the flow code, but the per-agent and per-test work happens inside **separately-built images** (`atelier-agent`, `atelier-runner-quick`, `atelier-runner-e2e`) that the worker launches via `/var/run/docker.sock` mounted from the host. The `*-builder` services in `docker-compose.yml` exist only to build these images at `compose up` — they run `entrypoint: ["true"]` and exit immediately; the worker's `depends_on: service_completed_successfully` gates on them. Do not add runtime commands to those builder services.

### Path-aliased mounts (no host/container duality)

`projects/`, `worktrees/`, `logs/` and `data/` are bind-mounted at the **same absolute path** inside every container as on the host (e.g. `/home/me/atelier/worktrees/task-42` resolves identically from the host shell, the worker, and the agent containers). This means:

- Anything `git worktree add` writes (the `.git` pointer files) resolves from both sides — the user can `cd worktrees/task-X && git log/diff/commit` from their host shell with no indirection.
- The worker passes the same path as both the volume source and the container target when spawning agent/runner containers (see `orchestrator/container.py` and `orchestrator/runner.py`).
- The translator helpers in `orchestrator/config.py` (`container_path_for_worktree` / `host_path_for_worktree` / `host_path_for_project`) are kept as identity functions for code clarity at call sites that historically distinguished host vs container paths.

This setup assumes `docker compose` is run from the repo root so `${PWD}` substitution in `docker-compose.yml` produces the right paths. `scripts/setup.sh` already enforces this.

### The pipeline flow and its retry loop (`flows/pipeline.py`)

Linear phases: `create-worktree → load-config → install-deps → implementer → quick-tests → reviewer → simplifier → full-tests → e2e-setup → e2e-tests → e2e-teardown`. Three **deterministic gates** (quick-tests, full-tests, e2e-tests) and the reviewer's `changes_requested` verdict can each loop the whole attempt back to the implementer with `previous_feedback`. The for/else in `pipeline_flow` bounds retries at `MAX_RETRY_ATTEMPTS + 1` attempts; failures after the last attempt raise `RuntimeError` to mark the flow failed. `e2e-teardown` is wrapped in try/finally so it always runs.

### Orchestrator ↔ agent contract

The worker does NOT stream anything into the agent container. It writes `TaskInput` JSON to `<worktree>/.task-input.json`, launches the container with `AGENT_ROLE` / `TASK_INPUT_FILE` env vars, and parses the **last `{`-starting line of stdout** as an `AgentResult` (see `_parse_agent_output` in `orchestrator/container.py`). `agents/entrypoint.py` deletes the input file before running the agent so it doesn't contaminate commits. If you change `agents/models.py` (the shared contract), rebuild BOTH `agent-builder` and `prefect-worker` — the worker has its own copy.

### Implementer is special

`agents/implementer.py` shells out to `aider` (with `--yes-always --no-auto-commits`) instead of using the ReAct loop in `agents/base.py`. Reviewer and simplifier use `BaseAgent` + `agents/tools/code_tools.py` tools + an Ollama chat client. Implementer then does its own `git add -A && git commit --allow-empty` so every attempt produces a commit even when Aider no-ops.

### E2E setup/teardown runs on the worker, NOT in the runner

`orchestrator/services.py` runs `docker compose up/down` for E2E services directly from the worker (which has the Docker socket), with `cwd=<host_worktree>`. Runners deliberately do not get the socket. If a user's `.atelier.yml` needs Docker-in-Docker for tests themselves, that's a new capability — don't "fix" it by mounting the socket into the runner.

### `.atelier.yml` lives in the TARGET repo

The config that drives test gates (`install`, `quick_tests`, `full_tests`, `e2e_tests`) is a file the user places at the root of THEIR repo — not in this pipeline repo. See `.atelier.yml.example`. Absence of the file is not an error: `load-config` returns `None` and all gates `skipped`. Absence of an individual section skips just that gate.

## Gotchas

- **Ollama host URL**: the pipeline talks to Ollama running on the Windows host, not in compose. `OLLAMA_EXTERNAL_URL` in `.env` must match the WSL→Windows gateway (`ip route show | grep -i default | awk '{ print $3 }'`) and can drift between reboots.
- **HOST_UID/HOST_GID**: all containers (worker, orchestrator, agent, runners) run with the host user's UID/GID (baked at build time via `ARG HOST_UID/HOST_GID`, and enforced at runtime via `user:` in compose and `user=` in `containers.create()`). `scripts/setup.sh` auto-fills these in `.env`. The worker gets the docker socket via `group_add: [${DOCKER_GID}]`. If you change `HOST_UID`/`HOST_GID`, you must rebuild the images.
- **Repo path sandbox**: `orchestrator/main.py::_resolve_repo_path` hard-requires the resolved path to be under `PROJECTS_ROOT` (host's `${PWD}/projects` by default) and contain `.git`. Don't add knobs to bypass this.
- **`runner-bundle/` directory**: this is a standalone "addon package" with its own README — a drop-in bundle showing what was added to bring test gates into the project. The canonical copies of those files already live in `orchestrator/`, `flows/`, `agents/`, `docker/`; edit those, not the bundle.
- **Empty branch reuse**: `create_worktree` force-deletes a pre-existing feature branch (`git branch -D`) before recreating the worktree. Tasks are treated as ephemeral — don't assume the branch from a previous run survives.
- **The `from prefect import flow` import at the very top of `orchestrator/main.py`** is a workaround for a Prefect 3 circular-import bug; don't "tidy" it down into the rest of the imports.
