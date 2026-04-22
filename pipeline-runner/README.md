# Tests gate — addon para pipeline-ia-prefect

Este paquete añade al pipeline la capacidad de ejecutar tests (unit, full, E2E)
como gates deterministas, con feedback loop al implementer si fallan.

## Arquitectura

```
implementer → quick_tests ─┬─→ reviewer → simplifier → full_tests ─┬→ e2e_setup → e2e_tests → e2e_teardown → done
                 │          │                                       │                    │
                 │(falla)   │                                       │(falla)             │(falla)
                 ▼          │                                       ▼                    ▼
          feedback al       │                                feedback al          feedback al
          implementer       │                                implementer          implementer
                            │(changes requested)
                            ▼
                      feedback al implementer
```

Máximo `MAX_RETRY_ATTEMPTS` (default 2) vueltas completas.

## Ficheros incluidos

| Fichero | Dónde va | Descripción |
|---|---|---|
| `orchestrator/test_config.py` | `orchestrator/` | Carga `.pipeline-ia.yml` del repo |
| `orchestrator/runner.py` | `orchestrator/` | Ejecuta comandos en contenedores efímeros |
| `orchestrator/services.py` | `orchestrator/` | Setup/teardown de services E2E (desde worker) |
| `agents/reviewer.py` | `agents/` | **Reemplaza** el reviewer viejo (prompt mejorado) |
| `flows/pipeline.py` | `flows/` | **Reemplaza** el flow viejo (con gates) |
| `docker/runner-quick.Dockerfile` | `docker/` | Imagen para tests unit/full |
| `docker/runner-e2e.Dockerfile` | `docker/` | Imagen para tests E2E (playwright) |
| `.pipeline-ia.yml.example` | raíz | Ejemplo para que los usuarios copien a sus repos |
| `docker-compose.PATCH.yml` | - | Referencia: dos servicios nuevos a añadir al compose |

## Aplicar al proyecto

```bash
cd ~/pipelines/pipeline-ia-prefect

# 1. Copiar ficheros nuevos (sobrescribe reviewer.py y pipeline.py)
cp -r <este-zip-descomprimido>/orchestrator/* orchestrator/
cp -r <este-zip-descomprimido>/agents/* agents/
cp -r <este-zip-descomprimido>/flows/* flows/
cp -r <este-zip-descomprimido>/docker/* docker/
cp <este-zip-descomprimido>/.pipeline-ia.yml.example .

# 2. Editar docker-compose.yml para añadir los dos runner-builders
# Ver docker-compose.PATCH.yml para el snippet exacto.
# Añadir los dos bloques nuevos y la dependencia en prefect-worker.
nano docker-compose.yml

# 3. Rebuild y relanzar
docker compose build
docker compose up -d --force-recreate
```

## Verificar que funciona

```bash
# Comprobar que las imágenes de runner existen
docker images | grep pipeline-runner
# Debería listar: pipeline-runner-quick:latest y pipeline-runner-e2e:latest

# Copiar el .pipeline-ia.yml.example a tu repo de prueba
cp .pipeline-ia.yml.example repos/target-repo/.pipeline-ia.yml
# Ajusta los comandos: para el target-repo Flask de prueba sería:
#   quick_tests.command: "pytest tests/ -x"
# (no hay e2e_tests ni install en ese repo de prueba)

# Lanzar tarea
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d @tasks/example.json

# Ver el flow en Prefect UI: http://localhost:4200
# Ahora verás tasks nuevos: load-config, install-deps, quick-tests, full-tests, etc.
```

## Cómo se ve en la UI de Prefect

Un flow típico tendrá estos task runs visibles:

```
create-worktree       ✓ 0.5s
load-config           ✓ 0.1s
install-deps          ✓ 12.3s
implementer           ✓ 4m 12s
quick-tests           ✓ 8.2s       ← gate
reviewer              ✓ 2m 5s
simplifier            ✓ 1m 30s
full-tests            ✓ 24.1s      ← gate
e2e-setup             ✓ 15.2s      (si hay e2e_tests)
e2e-tests             ✓ 2m 18s     ← gate
e2e-teardown          ✓ 5.0s       (siempre al final)
```

Si un gate falla y reintenta, verás el implementer y gates ejecutándose otra vez.

## Notas importantes

**Sobre install**: se ejecuta UNA VEZ al inicio (no por iteración del implementer).
Si los tests necesitan deps frescas por cada attempt, muévelo a `quick_tests.command`.

**Sobre E2E setup/teardown**: se ejecutan DESDE el worker, no dentro del runner.
Por eso necesitan el socket Docker del host. El teardown SIEMPRE se ejecuta
(con try/finally), aunque los tests fallen, para no dejar contenedores huérfanos.

**Sobre runners**: las imágenes se construyen al `docker compose up` la primera
vez. La de E2E (playwright) pesa ~1.5 GB — paciencia en el primer build.
