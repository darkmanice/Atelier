# pipeline-ia-prefect

Pipeline de agentes IA con **Prefect 3** como orquestador. Mantiene el Patrón A:
contenedores efímeros por agente, Ollama en el host Windows, FastAPI como
entrada HTTP. Pero la FSM y la cola las hace Prefect.

## Ganancia respecto al proyecto anterior

- **UI visual** en `http://localhost:4200` con timeline de cada tarea, logs por task,
  reintentos, tiempos, parámetros.
- **Reintentos automáticos** configurables por task.
- **Persistencia de estado gratis** (no mantenemos SQLite).
- Menos código custom (≈40% menos).

## Arquitectura

```
┌───────────────────────────────────────────────────────┐
│ docker compose                                        │
│                                                       │
│  ┌──────────────┐  ┌──────────────┐                   │
│  │ prefect-     │  │ orchestrator │  FastAPI :8000    │
│  │ server :4200 │  │  (FastAPI)   │  → Prefect API    │
│  │  UI + API    │  └──────────────┘                   │
│  └──────────────┘                                     │
│         ▲                                             │
│         │                                             │
│  ┌──────┴───────┐                                     │
│  │ prefect-     │  ← ejecuta flows                    │
│  │ worker       │  ← lanza contenedores-agente        │
│  │              │    via /var/run/docker.sock         │
│  └──────┬───────┘                                     │
│         │                                             │
│         ▼ docker run --rm                             │
│  ┌──────────────┐  ┌──────────────┐                   │
│  │ implementer  │  │   reviewer   │   simplifier...   │
│  │ (efímero)    │  │  (efímero)   │                   │
│  └──────────────┘  └──────────────┘                   │
│                                                       │
└───────────────────────────────────────────────────────┘
              │
              ▼
       Ollama (Windows host)
```

## Puesta en marcha

```bash
# 1. Copia los ficheros de agents/ desde el proyecto anterior que ya funciona:
cd ~/pipelines/pipeline-ia-prefect
cp ~/pipelines/pipeline-ia/agents/entrypoint.py agents/
cp ~/pipelines/pipeline-ia/agents/llm.py agents/
cp ~/pipelines/pipeline-ia/agents/base.py agents/
cp ~/pipelines/pipeline-ia/agents/implementer.py agents/
cp ~/pipelines/pipeline-ia/agents/reviewer.py agents/
cp ~/pipelines/pipeline-ia/agents/simplifier.py agents/
cp ~/pipelines/pipeline-ia/agents/tools/__init__.py agents/tools/
cp ~/pipelines/pipeline-ia/agents/tools/code_tools.py agents/tools/
cp ~/pipelines/pipeline-ia/scripts/init-test-repo.sh scripts/
chmod +x scripts/init-test-repo.sh

# 2. Configurar .env
cp .env.example .env
# Edita .env: ajusta OLLAMA_EXTERNAL_URL con la IP actual del host Windows
#   ip route show | grep -i default | awk '{ print $3 }'
# Y el DOCKER_GID del host:
#   getent group docker | cut -d: -f3
nano .env

# 3. Levantar todo
docker compose up -d --build

# 4. Seguir logs del worker para ver que se registra
docker compose logs -f prefect-worker
# Cuando veas "Worker 'PipelineWorker' started!" está listo.

# 5. Preparar repo de prueba (si no lo tienes ya)
bash scripts/init-test-repo.sh
sudo chown -R 1000:1000 repos worktrees logs

# 6. Lanzar una tarea
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Add a comment saying hello at the top of app.py",
    "repo_path": "target-repo",
    "base_branch": "main"
  }'

# 7. Ver el progreso
# - UI visual: http://localhost:4200
# - Log markdown local: tail -f logs/task-*.md
```

## Endpoints

| Método | Endpoint | Descripción |
|---|---|---|
| GET | `/health` | Estado del orquestador y Prefect |
| POST | `/tasks` | Crea un flow run con los parámetros dados |
| GET | `/tasks` | Lista flow runs recientes |
| GET | `/tasks/{flow_run_id}` | Detalle de un flow run |
| GET | `/tasks/{task_id}/log` | Log markdown local |

Para lo visual/interactivo, usa **`http://localhost:4200`** — la UI de Prefect.

## Configuración

Todo configurable via `.env`:

- `OLLAMA_EXTERNAL_URL` — dónde está Ollama (Windows host).
- `PIPELINE_MODEL` — modelo Ollama a usar (default `gemma4:26b`).
- `PIPELINE_MAX_CONCURRENT` — tareas paralelas máximas (default 2).
- `PIPELINE_AGENT_MEM` — memoria por contenedor-agente (default 4g).
- `PIPELINE_AGENT_TIMEOUT` — segundos (default 2400).
- `DOCKER_GID` — GID del grupo docker del host.

## Debug

```bash
# Estado de los servicios
docker compose ps

# Logs del flow (cuando hay uno corriendo)
docker compose logs -f prefect-worker

# Logs del orchestrator (FastAPI)
docker compose logs -f orchestrator

# Entrar al worker para debuggear
docker compose exec prefect-worker bash

# Recrear imágenes tras cambios de código
docker compose build --no-cache prefect-worker
docker compose up -d --force-recreate prefect-worker

# Si cambias prompts del agente
docker compose build agent-builder
```

## Estructura

```
pipeline-ia-prefect/
├── docker-compose.yml        ← 4 servicios
├── .env.example
├── prefect.yaml              ← deployment declarativo
├── docker/
│   ├── worker.Dockerfile     ← extiende prefecthq/prefect:3-latest
│   ├── orchestrator.Dockerfile
│   ├── agent.Dockerfile      ← mismo que pipeline-ia
│   ├── worker-requirements.txt
│   ├── agent-requirements.txt
│   └── (orchestrator requirements en orchestrator/)
├── flows/
│   └── pipeline.py           ← EL FLOW (reemplaza fsm.py antiguo)
├── orchestrator/
│   ├── main.py               ← FastAPI delgado → Prefect API
│   ├── config.py
│   ├── worktree.py
│   ├── container.py          ← idéntico al probado
│   ├── logger.py
│   └── requirements.txt
├── agents/                   ← COPIAR desde pipeline-ia/ que ya funciona
│   ├── models.py
│   ├── entrypoint.py
│   ├── llm.py
│   ├── base.py
│   ├── implementer.py
│   ├── reviewer.py
│   ├── simplifier.py
│   └── tools/code_tools.py
├── scripts/
│   └── init-test-repo.sh     ← COPIAR desde pipeline-ia/
├── tasks/
│   └── example.json
├── repos/                    ← (runtime) tus repos git
├── worktrees/                ← (runtime) uno por tarea
└── logs/                     ← (runtime) markdown por tarea
```

## Qué cambia respecto al proyecto anterior

**Borrado**:
- `orchestrator/fsm.py` → `flows/pipeline.py`
- `orchestrator/scheduler.py` → Prefect gestiona la concurrencia
- `orchestrator/db.py` → Prefect gestiona el estado

**Modificado**:
- `orchestrator/main.py` → mucho más corto, solo traduce HTTP → Prefect

**Intacto**:
- Todo `agents/`
- `orchestrator/container.py`, `worktree.py`, `logger.py`, `config.py`
- `docker/agent.Dockerfile`
