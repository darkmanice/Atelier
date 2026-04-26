# Atelier

> This is a personal project to learn how to orchestrate autonomous coding
> agents end-to-end: a single iterating LLM session, deterministic test
> gates as control, retry loops with feedback, and ephemeral compute per
> task.

Code-modification pipeline orchestrated with Prefect 3. It takes a task
(prompt + target repo), runs an autonomous **OpenHands V1** session against
the worktree, runs deterministic test gates, optionally a focused
simplification pass, and produces a branch with the changes ready for human
review.

- **Prefect UI** at `http://localhost:4200` with per-run timeline, logs and
  retries.
- **FastAPI** at `http://localhost:8000` with its own UI and REST API.
- **Per-task markdown log** at `logs/task-<id>.md`.
- **Per-task event stream** at `logs/task-<id>.events.jsonl` (raw OpenHands
  events from every session of the task — useful for debugging).
- **Automatic retries**: any failed test gate loops the pipeline back to a
  fresh OpenHands session with the test output as feedback.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│ docker compose (atelier_network)                     │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐                  │
│  │ prefect-     │  │ orchestrator │  FastAPI :8000   │
│  │ server :4200 │◄─┤  (FastAPI)   │  UI + API        │
│  │  UI + API    │  └──────────────┘                  │
│  └──────▲───────┘                                    │
│         │                                            │
│  ┌──────┴──────────────────────────────────────┐     │
│  │ prefect-worker                              │     │
│  │   • runs the Prefect flow                   │     │
│  │   • embeds the OpenHands V1 SDK             │     │
│  │   • spawns ephemeral runner containers      │     │
│  │     via /var/run/docker.sock                │     │
│  └──────┬──────────────────────────────────────┘     │
│         │                                            │
│         ▼ docker run --rm                            │
│              ┌──────────────┐  ┌──────────────┐      │
│              │ atelier-     │  │ atelier-     │      │
│              │ runner-quick │  │ runner-e2e   │      │
│              │ (tests)      │  │ (Playwright) │      │
│              └──────────────┘  └──────────────┘      │
└──────────────────────────────────────────────────────┘
              │                            ▲
              ▼                            │
      ┌──────────────┐              ┌──────────────┐
      │ LLM provider │              │ Target repo  │
      │ (Ollama,     │              │ (bind mount  │
      │  NIM, OAI…)  │              │  ./projects/)│
      └──────────────┘              └──────────────┘
```

Note: there are **no per-task agent containers**. The OpenHands SDK runs
in-process inside the Prefect worker (`runtime=local`). Sandboxing comes from
`branch_guard` (refs snapshot before/after) plus the path-aliased worktree.

### Flow phases

Default linear flow:

```
create-worktree → load-config → install-deps →
  agent-session → quick-tests → full-tests →
  e2e-setup → e2e-tests → e2e-teardown → preview
```

Three **deterministic gates** (`quick-tests`, `full-tests`, `e2e-tests`) loop
the pipeline back to `agent-session` with `previous_feedback`. The retry cap
is set by `MAX_RETRY_ATTEMPTS`. The user can also opt in to a `simplify-pass`
block via the canvas in the UI for a focused cleanup run after the gates
pass.

There is no separate reviewer phase — the OpenHands session iterates
internally to self-correct.

---

## Prerequisites

- **Docker** + **Docker Compose v2** (`docker compose`, not `docker-compose`).
- An **OpenAI-compatible LLM provider** with **tool-calling support**.
  Built-in presets: Ollama (local), NVIDIA NIM, OpenAI, OpenRouter. Any
  endpoint that speaks `POST /v1/chat/completions` and supports tool calls
  works. For local-only setups, `qwen3.6:latest`, `qwen2.5-coder:32b` and
  `devstral` are good defaults.

---

## Getting started

```bash
# 1. Clone
git clone https://github.com/darkmanice/Atelier && cd atelier

# 2. One-shot bootstrap: auto-detects HOST_UID, HOST_GID, DOCKER_GID and
#    generates INTERNAL_API_TOKEN. Writes everything into .env.
bash scripts/setup.sh

# 3. Edit .env and set DEFAULT_MODEL (model id exposed by your LLM provider —
#    must support tool-calling). Optionally set MASTER_ENCRYPTION_KEY to
#    persist saved providers:
#      openssl rand -hex 32   # copy into .env

# 4. Drop or clone any git repo you want the pipeline to work on under
#    ./projects/.

# 5. Bring everything up (first run pulls ~2 GB of images)
docker compose up -d --build

# 6. Verify
curl http://localhost:8000/health
# Orchestrator UI: http://localhost:8000
# Prefect UI:      http://localhost:4200
```

`scripts/setup.sh` is idempotent: re-running it only fills in empty or
placeholder values, so it never clobbers anything you have already set.

### Preparing a target repository

Drop or clone any git repo under `./projects/` (inside the atelier checkout).
Every subdirectory with a `.git/` folder becomes a selectable target from the
UI. No chown dance needed — the containers run with your host user's UID/GID,
so files created by the pipeline stay owned by you.

### Inspecting a task's worktree from your host

Atelier mounts `worktrees/` at the **same absolute path** inside every
container as on the host, so once a task creates a worktree you can run git
on it directly:

```bash
cd worktrees/task-<id>
git log --oneline -10
git diff master..HEAD
```

### Firing a task

Use the UI at `http://localhost:8000`, or send a `POST /tasks` with a JSON
body. Minimum payload:

```json
{
  "prompt": "Describe what you want the agent to do.",
  "repo_path": "your-repo",
  "base_branch": "main"
}
```

See `TaskCreate` in `orchestrator/main.py` for the full schema (LLM provider
overrides, per-phase model selection, custom pipeline spec, etc.).

---

## Environment variables

All settings live in `.env`. The ones marked **auto** are filled in by
`scripts/setup.sh` — you shouldn't need to touch them by hand.

### Required

| Variable | Description |
|---|---|
| `INTERNAL_API_TOKEN` | **auto** · Shared secret between orchestrator and worker. Generated by `setup.sh`. |
| `DEFAULT_MODEL` | Default LLM model id shown in the UI (can be overridden per task). MUST support tool-calling. Examples: `qwen3.6:latest`, `qwen2.5-coder:32b`, `devstral`, `meta/llama-3.3-70b-instruct`, `gpt-4o-mini`. |
| `HOST_UID` / `HOST_GID` | **auto** · UID/GID of your host user. Containers run with these so files stay owned by you. Generated by `setup.sh` (`id -u` / `id -g`). |
| `DOCKER_GID` | **auto** · GID of the host's `docker` group. The worker uses it as a supplementary group to access `/var/run/docker.sock`. Generated by `setup.sh`. |

### Optional

| Variable | Default | Description |
|---|---|---|
| `MASTER_ENCRYPTION_KEY` | *(empty)* | 32 hex bytes (64 chars). When set, enables stored LLM providers (API keys encrypted at `data/providers.json`). When empty, only the one-shot flow works. **Losing this key = losing every stored API key.** |
| `MAX_CONCURRENT_TASKS` | `2` | Parallel flow runs allowed in the pool. |
| `MAX_RETRY_ATTEMPTS` | `2` | After this many failed test-gate retries the flow gives up. |
| `OPENHANDS_MAX_ITERATIONS` | `30` | Cap of agent loop iterations per session. Each iteration ≈ one LLM call + one tool execution. Bound the wall-clock of a single phase with slow local models. |
| `ORCHESTRATOR_PORT` | `8000` | Host port for the FastAPI service. |
| `AGENT_MEM_LIMIT` | `4g` | Max memory per ephemeral runner container. |
| `AGENT_CPU_LIMIT` | `2.0` | CPUs per runner container (decimals allowed). |

---

## Target-repo configuration: `.atelier.yml`

Place a `.atelier.yml` at the root of your target repo (NOT in the atelier
repo itself) describing how to install dependencies, run your tests, and
optionally bring up a preview after success. Every section is optional —
omitting one just skips that phase with a warning. If the file is missing
entirely, the pipeline still runs but executes no tests.

See `.atelier.yml.example` for a full template.

---

## Orchestrator API

Main endpoints (all under `http://localhost:${ORCHESTRATOR_PORT}`):

| Method | Path | What it does |
|---|---|---|
| `GET` | `/health` | Health check. |
| `POST` | `/tasks` | Creates a flow run. Body: `TaskCreate` (see `orchestrator/main.py`). |
| `GET` | `/tasks` | Lists flow runs. |
| `GET` | `/tasks/{id}` | State of a single flow run. |
| `GET` | `/tasks/{id}/log` | Per-task markdown log (`text/plain`). |
| `GET` | `/tasks/{id}/preview` | Preview URL if it is up. |
| `DELETE` | `/tasks/{id}/preview` | Tears the preview down. |
| `GET` | `/providers` | Lists stored LLM providers (when `MASTER_ENCRYPTION_KEY` is set). |
| `POST` | `/providers` | Stores a provider (API key encrypted). |
| `DELETE` | `/providers/{id}` | Deletes a provider. |

There are also `/ui/*` routes returning HTML fragments for the (htmx-friendly)
UI.

---

## Rebuilds after code changes

The compose file bakes code INTO the images — edits on disk are NOT picked
up live.

| What changed | What to rebuild |
|---|---|
| `flows/`, `orchestrator/` or `agents/` | `docker compose build prefect-worker orchestrator && docker compose up -d --force-recreate prefect-worker orchestrator`. The worker image now embeds `openhands-sdk` and `openhands-tools`, and copies the full `agents/` tree. |
| Runner Dockerfiles | `docker compose build runner-quick-builder runner-e2e-builder` |

---

## Gotchas / Troubleshooting

**Tool-calling is required.** OpenHands drives the LLM via function calls
through the OpenAI-compatible endpoint. Models that don't support tool
calling (some older LLaMA variants, gemma without the `:instruct` flavour,
…) will fail at the first action. Stick to `qwen3.6`, `qwen2.5-coder`,
`devstral`, `llama3.1+`, `mistral` family, or any frontier-API model.

**Permissions on bind-mounts.** Every container runs with your host user's
UID/GID (`HOST_UID` / `HOST_GID` in `.env`, auto-filled by `setup.sh`), so
files created by the pipeline on bind-mounted directories stay owned by you.
If you rebuild after changing `HOST_UID`/`HOST_GID`, you'll need to pass
`--build` to pick up the new values baked into the images.

**Path-aliased mounts.** `projects/`, `worktrees/`, `logs/` and `data/` are
bind-mounted at the same absolute path inside every container as on the
host, so anything `git worktree add` writes resolves from both sides
(`cd worktrees/task-X && git log` works directly). This assumes you run
`docker compose` from the repo root so `${PWD}` substitution resolves
correctly. `scripts/setup.sh` enforces that.

**Repo-path sandbox.** `orchestrator/main.py::_resolve_repo_path` requires
the target repo to live under `PROJECTS_ROOT` (the host's `${PWD}/projects`
by default) and contain `.git`. There is no knob to bypass this.

**Same-name branch reuse.** `create_worktree` force-deletes a pre-existing
feature branch (`git branch -D`) before recreating the worktree. Tasks are
ephemeral — do not assume the branch from a previous run survives.

**LLM reachable from the worker.** The worker process must be able to reach
your LLM provider. If you run Ollama on Windows under WSL2, the gateway IP
can drift between reboots:

```bash
ip route show | grep -i default | awk '{ print $3 }'
```

Update the preset from the UI (`/providers-ui`) if you use it.

**E2E with no Docker-in-Docker.** `e2e-setup`/`e2e-teardown` run on the
worker (which has the Docker socket). Test runners deliberately do NOT get
the socket. If your tests need to launch containers on their own, that is a
new capability — not a bug.

**Useful logs.**

```bash
docker compose logs -f prefect-worker     # where the flows AND OpenHands run
docker compose logs -f orchestrator       # FastAPI
cat logs/task-<id>.md                     # human-readable task log
cat logs/task-<id>.events.jsonl           # raw OpenHands event stream
```

**Prefect UI.** `http://localhost:4200` — per-run timeline, per-task logs,
retries, timings, parameters.
