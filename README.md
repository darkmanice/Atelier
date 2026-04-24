# Atelier

> This is a personal project to learn how to orchestrate multi-agent
> pipelines: agent roles, test gates as deterministic control, retry loops
> with feedback, and ephemeral compute per task.

Multi-agent code-modification pipeline orchestrated with Prefect 3. It takes a
task (prompt + target repo), launches specialized AI agents (`implementer` →
`reviewer` → `simplifier`) in ephemeral Docker containers, runs deterministic
test gates between phases, and produces a branch with the changes ready for
human review.

- **Prefect UI** at `http://localhost:4200` with per-run timeline, logs, and
  retries.
- **FastAPI** at `http://localhost:8000` with its own UI and REST API.
- **Per-task markdown log** at `logs/task-<id>.md`.
- **Automatic retries**: any failed test gate or a `changes_requested` verdict
  from the reviewer loops the pipeline back with feedback.

---

## Architecture

```
┌───────────────────────────────────────────────────────┐
│ docker compose (atelier_network)                      │
│                                                       │
│  ┌──────────────┐  ┌──────────────┐                   │
│  │ prefect-     │  │ orchestrator │  FastAPI :8000    │
│  │ server :4200 │◄─┤  (FastAPI)   │  UI + API         │
│  │  UI + API    │  └──────────────┘                   │
│  └──────▲───────┘                                     │
│         │                                             │
│  ┌──────┴───────┐                                     │
│  │ prefect-     │  ← runs the flows                   │
│  │ worker       │  ← launches ephemeral containers    │
│  │              │    via /var/run/docker.sock         │
│  └──────┬───────┘                                     │
│         │                                             │
│         ▼ docker run --rm                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ atelier-     │  │ atelier-     │  │ atelier-     │ │
│  │ agent        │  │ runner-quick │  │ runner-e2e   │ │
│  │ (ephemeral)  │  │ (tests)      │  │ (Playwright) │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└───────────────────────────────────────────────────────┘
              │                             ▲
              ▼                             │
      ┌──────────────┐               ┌──────────────┐
      │ LLM provider │               │ Target repo  │
      │ (Ollama,     │               │ (bind mount  │
      │  NIM, OAI…)  │               │  ./repos/)   │
      └──────────────┘               └──────────────┘
```

### Flow phases

`create-worktree → load-config → install-deps → implementer → quick-tests →
reviewer → simplifier → full-tests → e2e-setup → e2e-tests → e2e-teardown`

There are three **deterministic gates** (`quick-tests`, `full-tests`,
`e2e-tests`) plus the reviewer's verdict. Any of the four can loop the
pipeline back to the `implementer` with `previous_feedback`. The retry cap is
set by `MAX_RETRY_ATTEMPTS`.

---

## Prerequisites

- **Docker** + **Docker Compose v2** (`docker compose`, not `docker-compose`).
- An **OpenAI-compatible LLM provider**. Built-in presets: Ollama,
  NVIDIA NIM, OpenAI, OpenRouter.... Any endpoint that speaks
  `POST /v1/chat/completions` works.

---

## Getting started

```bash
# 1. Clone
git clone <url> atelier && cd atelier

# 2. Prepare .env
cp .env.example .env

# 3. Generate the internal token (required)
openssl rand -hex 32   # copy the value into INTERNAL_API_TOKEN in .env

# 4. (Optional) Enable stored LLM providers (encrypted on disk)
openssl rand -hex 32   # copy into MASTER_ENCRYPTION_KEY in .env
#   If you lose this key, all stored API keys become unreadable.

# 5. Set the host's docker group GID (for the socket)
getent group docker | cut -d: -f3   # copy the number into DOCKER_GID in .env

# 6. Bring everything up (first run pulls ~2 GB of images)
docker compose up -d --build

# 7. Verify
curl http://localhost:8000/health
# Orchestrator UI: http://localhost:8000
# Prefect UI:      http://localhost:4200
```

### Preparing a target repository

The worker only runs tasks against repos that live under `./repos/` (or the
path set in `REPOS_HOST_DIR`). Each one must be a valid git repo (it must
contain a `.git/` directory). Drop or clone yours in there.

```bash
# The worker runs as UID 1000 inside the container. If your host user has
# a different UID, fix ownership on the shared directories:
sudo chown -R 1000:1000 repos worktrees logs
```

### Firing a task

Use the UI at `http://localhost:8000`, or send a `POST /tasks` with a JSON
body. Minimum payload:

```json
{
  "prompt": "Describe what you want the agents to do.",
  "repo_path": "your-repo",
  "base_branch": "main"
}
```

See `TaskCreate` in `orchestrator/main.py` for the full schema (LLM provider
overrides, per-role model selection, etc.).

---

## Environment variables

All settings live in `.env` (start from `.env.example`).

### Required

| Variable | Description |
|---|---|
| `INTERNAL_API_TOKEN` | Shared secret between orchestrator and worker. Generate with `openssl rand -hex 32`. |
| `DEFAULT_MODEL` | Default LLM model id shown in the UI (can be overridden per task). Any model id your provider exposes — e.g. `meta/llama-3.3-70b-instruct`, `gpt-4o-mini`, `qwen2.5-coder:32b`. |
| `REPOS_HOST_DIR` | Absolute path on the host holding your target repos. Mounted as `/repos` in the worker and orchestrator; every subdirectory under it with `.git/` becomes a selectable target. |

### Optional

| Variable | Default | Description |
|---|---|---|
| `MASTER_ENCRYPTION_KEY` | *(empty)* | 32 hex bytes (64 chars). When set, enables stored LLM providers (API keys encrypted at `data/providers.json`). When empty, only the one-shot flow works. **Losing this key = losing every stored API key.** |
| `MAX_CONCURRENT_TASKS` | `2` | Parallel flow runs allowed in the pool. |
| `ORCHESTRATOR_PORT` | `8000` | Host port for the FastAPI service. |
| `AGENT_MEM_LIMIT` | `4g` | Max memory per agent container. |
| `AGENT_CPU_LIMIT` | `2.0` | CPUs per agent container (decimals allowed). |
| `AGENT_TIMEOUT_SEC` | `2400` | Per-invocation timeout for the agent container. |
| `DOCKER_GID` | `988` | GID of the host's `docker` group. Obtain with `getent group docker \| cut -d: -f3`. |

---

## Target-repo configuration: `.atelier.yml`

Place a `.atelier.yml` at the root of your target repo (NOT in the atelier
repo itself) describing how to install dependencies and run your tests. Every
section is optional — omitting one just skips that gate with a warning. If
the file is missing entirely, the pipeline still runs but executes no tests.

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
| `flows/` or `orchestrator/` | `docker compose build prefect-worker orchestrator && docker compose up -d --force-recreate prefect-worker orchestrator` |
| `agents/` or prompts | `docker compose build agent-builder` (it is a one-shot service that produces `atelier-agent:latest` and exits; the worker `docker run`s that image for each invocation) |
| Runner Dockerfiles | `docker compose build runner-quick-builder runner-e2e-builder` |

---

## Gotchas / Troubleshooting

**"dubious ownership" on git operations.** The worker runs as UID 1000. If
your host user has a different UID, the mounted directories (`repos/`,
`worktrees/`, `logs/`) may end up with wrong permissions. Fix:

```bash
sudo chown -R 1000:1000 repos worktrees logs
```

The worker image sets `git config --system safe.directory '*'` as a backstop,
but write permissions are still yours to sort out.

**Container/host path dualism.** The worker launches containers on the host's
Docker daemon. Any bind mount needs:

- the path as the worker sees it (e.g. `/app/worktrees/task-42`)
- the path as the host sees it (e.g. `/home/you/atelier/worktrees/task-42`)

That is why `WORKTREES_HOST_DIR` is bind-mounted to itself (same path inside
and outside the container). Don't change this without understanding what
breaks.

**Repo-path sandbox.** `orchestrator/main.py::_resolve_repo_path` requires
the target repo to live under `/repos` and contain `.git`. There is no knob
to bypass this.

**Same-name branch reuse.** `create_worktree` force-deletes a pre-existing
feature branch (`git branch -D`) before recreating the worktree. Tasks are
ephemeral — do not assume the branch from a previous run survives.

**LLM reachable from the worker.** The agent container must be able to reach
your LLM provider. If you run Ollama on Windows under WSL2, the gateway IP
can drift between reboots:

```bash
ip route show | grep -i default | awk '{ print $3 }'
```

Update the preset from the UI (`/providers`) if you use it.

**E2E with no Docker-in-Docker.** `e2e-setup`/`e2e-teardown` run on the
worker (which has the Docker socket). Test runners deliberately do NOT get
the socket. If your tests need to launch containers on their own, that is a
new capability — not a bug.

**Useful logs.**

```bash
docker compose logs -f prefect-worker     # where the flows run
docker compose logs -f orchestrator       # FastAPI
cat logs/task-<id>.md                     # per-task markdown log
```

**Prefect UI.** `http://localhost:4200` — per-run timeline, per-task logs,
retries, timings, parameters.
