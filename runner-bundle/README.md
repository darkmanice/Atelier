# Test gates — atelier addon

This bundle extends the pipeline with the ability to run tests (unit, full,
E2E) as deterministic gates, with a feedback loop back to the implementer on
failure.

## Architecture

```
implementer → quick_tests ─┬─→ reviewer → simplifier → full_tests ─┬→ e2e_setup → e2e_tests → e2e_teardown → done
                 │          │                                       │                    │
                 │(fail)    │                                       │(fail)              │(fail)
                 ▼          │                                       ▼                    ▼
         feedback to        │                                feedback to          feedback to
         implementer        │                                implementer          implementer
                            │(changes requested)
                            ▼
                     feedback to implementer
```

At most `MAX_RETRY_ATTEMPTS` (default 2) full loops.

## Files included

| File | Destination | Description |
|---|---|---|
| `orchestrator/test_config.py` | `orchestrator/` | Loads `.atelier.yml` from the target repo |
| `orchestrator/runner.py` | `orchestrator/` | Runs commands inside ephemeral containers |
| `orchestrator/services.py` | `orchestrator/` | E2E service setup/teardown (from the worker) |
| `agents/reviewer.py` | `agents/` | **Replaces** the old reviewer (improved prompt) |
| `flows/pipeline.py` | `flows/` | **Replaces** the old flow (with gates) |
| `docker/runner-quick.Dockerfile` | `docker/` | Image for unit/full tests |
| `docker/runner-e2e.Dockerfile` | `docker/` | Image for E2E tests (Playwright) |
| `.atelier.yml.example` | repo root | Example users can copy into their own repos |
| `docker-compose.PATCH.yml` | - | Reference: two new services to add to the compose file |

## Applying it to the project

```bash
cd ~/pipelines/atelier

# 1. Copy in the new files (overwrites reviewer.py and pipeline.py)
cp -r <this-bundle>/orchestrator/* orchestrator/
cp -r <this-bundle>/agents/* agents/
cp -r <this-bundle>/flows/* flows/
cp -r <this-bundle>/docker/* docker/
cp <this-bundle>/.atelier.yml.example .

# 2. Edit docker-compose.yml to add the two runner-builders.
# See docker-compose.PATCH.yml for the exact snippet.
# Add both new blocks and the dependency on prefect-worker.
nano docker-compose.yml

# 3. Rebuild and redeploy
docker compose build
docker compose up -d --force-recreate
```

## Verifying it works

```bash
# Check the runner images exist
docker images | grep atelier-runner
# Should list: atelier-runner-quick:latest and atelier-runner-e2e:latest

# Copy the .atelier.yml.example into your target repo and adjust the
# commands to your project:
cp .atelier.yml.example repos/<your-repo>/.atelier.yml

# Fire a task (UI at :8000 or POST /tasks with a JSON body)
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "...", "repo_path": "<your-repo>", "base_branch": "main"}'

# Watch it in the Prefect UI: http://localhost:4200
# You will now see new task runs: load-config, install-deps, quick-tests,
# full-tests, etc.
```

## What it looks like in the Prefect UI

A typical flow run shows these task runs:

```
create-worktree       ✓ 0.5s
load-config           ✓ 0.1s
install-deps          ✓ 12.3s
implementer           ✓ 4m 12s
quick-tests           ✓ 8.2s       ← gate
reviewer              ✓ 2m 5s
simplifier            ✓ 1m 30s
full-tests            ✓ 24.1s      ← gate
e2e-setup             ✓ 15.2s      (only if e2e_tests is configured)
e2e-tests             ✓ 2m 18s     ← gate
e2e-teardown          ✓ 5.0s       (always at the end)
```

If a gate fails and the pipeline retries, you will see the implementer and
gates running again.

## Important notes

**On `install`**: it runs ONCE at start-up (not per implementer iteration).
If your tests need fresh deps every attempt, move the install command into
`quick_tests.command` instead.

**On E2E setup/teardown**: they run FROM the worker, not inside the runner.
That is why they need the host Docker socket. The teardown ALWAYS runs
(wrapped in try/finally) even if the tests fail, so no orphaned containers
are left behind.

**On the runners**: the images are built on the first `docker compose up`.
The E2E image (Playwright) is ~1.5 GB — be patient on the first build.
