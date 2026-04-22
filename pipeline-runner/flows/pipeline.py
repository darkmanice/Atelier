"""
Pipeline de agentes como Prefect flow, con gates de tests.

Fases:
  1. install (si hay config)
  2. implementer
  3. quick_tests (gate — si falla, feedback al implementer)
  4. reviewer
  5. simplifier
  6. full_tests (gate — si falla, feedback al implementer)
  7. setup_services_e2e + e2e_tests + teardown_services_e2e (gate final)

Cada gate determinista puede devolver al implementer con feedback.
Número total de iteraciones limitado por MAX_RETRY_ATTEMPTS.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from prefect import flow, get_run_logger, task
from prefect.tasks import exponential_backoff

from agents.models import AgentResult, AgentRole, TaskInput
from orchestrator import logger as tasklog
from orchestrator.config import DEFAULT_MODEL, OLLAMA_HOST_FROM_CONTAINER
from orchestrator.container import run_agent
from orchestrator.runner import (
    RUNNER_E2E_IMAGE,
    RUNNER_QUICK_IMAGE,
    RunnerResult,
    run_command_in_runner,
    skipped,
)
from orchestrator.services import run_service_command
from orchestrator.pipeline_config import PipelineConfig, load_config
from orchestrator.worktree import WorktreeHandle, create_worktree, get_diff_summary


MAX_RETRY_ATTEMPTS = int(os.environ.get("PIPELINE_MAX_RETRY_ATTEMPTS", "2"))


# ---------------------------
# Tasks — fases de agente
# ---------------------------

@task(name="create-worktree", retries=1)
def task_create_worktree(
    task_id: int, repo_path: str, base_branch: str, feature_branch: str
) -> dict:
    logger = get_run_logger()
    logger.info(f"Creating worktree for task {task_id}")
    handle = create_worktree(
        repo_path=Path(repo_path),
        task_id=task_id,
        base_branch=base_branch,
        feature_branch=feature_branch,
    )
    return {
        "container_path": str(handle.container_path),
        "host_path": str(handle.host_path),
    }


def _run_agent_task(
    role: AgentRole,
    task_id: int,
    prompt: str,
    base_branch: str,
    feature_branch: str,
    model: str,
    worktree: dict,
    previous_feedback: Optional[str] = None,
) -> dict:
    task_input = TaskInput(
        task_id=task_id,
        role=role,
        prompt=prompt,
        worktree_path="/workspace",
        base_branch=base_branch,
        feature_branch=feature_branch,
        previous_feedback=previous_feedback,
        model=model,
        ollama_host=OLLAMA_HOST_FROM_CONTAINER,
    )
    result = run_agent(
        task_input,
        Path(worktree["host_path"]),
        Path(worktree["container_path"]),
    )
    tasklog.append_agent_result(task_id, role.value, result)
    return result.model_dump()


@task(name="implementer", retries=1,
      retry_delay_seconds=exponential_backoff(backoff_factor=10))
def task_implementer(**kwargs) -> dict:
    return _run_agent_task(AgentRole.IMPLEMENTER, **kwargs)


@task(name="reviewer", retries=1)
def task_reviewer(task_id: int, prompt: str, base_branch: str, feature_branch: str,
                   model: str, worktree: dict) -> dict:
    return _run_agent_task(
        AgentRole.REVIEWER, task_id=task_id, prompt=prompt,
        base_branch=base_branch, feature_branch=feature_branch,
        model=model, worktree=worktree,
    )


@task(name="simplifier", retries=1)
def task_simplifier(task_id: int, prompt: str, base_branch: str, feature_branch: str,
                     model: str, worktree: dict) -> dict:
    return _run_agent_task(
        AgentRole.SIMPLIFIER, task_id=task_id, prompt=prompt,
        base_branch=base_branch, feature_branch=feature_branch,
        model=model, worktree=worktree,
    )


# ---------------------------
# Tasks — fases de tests
# ---------------------------

@task(name="load-config")
def task_load_config(worktree: dict) -> dict | None:
    logger = get_run_logger()
    container_path = Path(worktree["container_path"])
    try:
        config = load_config(container_path)
    except ValueError as e:
        logger.error(f"Invalid .pipeline-ia.yml: {e}")
        return None

    if config is None:
        logger.warning(
            ".pipeline-ia.yml not found in repo root. "
            "No tests will be executed. Pipeline will continue without test gates."
        )
        return None

    logger.info("Loaded .pipeline-ia.yml")
    return config.model_dump()


@task(name="install-deps")
def task_install(worktree: dict, config_dict: dict | None) -> dict:
    logger = get_run_logger()
    if not config_dict or not config_dict.get("install"):
        logger.info("No install config; skipping dep installation")
        return skipped("no install config").__dict__

    cfg = config_dict["install"]
    result = run_command_in_runner(
        image=RUNNER_QUICK_IMAGE,
        command=cfg["command"],
        host_worktree_path=Path(worktree["host_path"]),
        timeout_sec=cfg["timeout"],
    )
    if not result.success:
        logger.error(f"Install failed (exit {result.exit_code})")
    return result.__dict__


@task(name="quick-tests")
def task_quick_tests(worktree: dict, config_dict: dict | None) -> dict:
    logger = get_run_logger()
    if not config_dict or not config_dict.get("quick_tests"):
        logger.warning("No quick_tests section in config; skipping")
        return skipped("no quick_tests section").__dict__

    cfg = config_dict["quick_tests"]
    logger.info(f"Running quick tests: {cfg['command']}")
    result = run_command_in_runner(
        image=RUNNER_QUICK_IMAGE,
        command=cfg["command"],
        host_worktree_path=Path(worktree["host_path"]),
        timeout_sec=cfg["timeout"],
    )
    logger.info(f"Quick tests {'PASSED' if result.success else 'FAILED'} "
                f"(exit {result.exit_code}, {result.duration_seconds:.1f}s)")
    return result.__dict__


@task(name="full-tests")
def task_full_tests(worktree: dict, config_dict: dict | None) -> dict:
    logger = get_run_logger()
    if not config_dict or not config_dict.get("full_tests"):
        logger.warning("No full_tests section in config; skipping")
        return skipped("no full_tests section").__dict__

    cfg = config_dict["full_tests"]
    logger.info(f"Running full tests: {cfg['command']}")
    result = run_command_in_runner(
        image=RUNNER_QUICK_IMAGE,
        command=cfg["command"],
        host_worktree_path=Path(worktree["host_path"]),
        timeout_sec=cfg["timeout"],
    )
    logger.info(f"Full tests {'PASSED' if result.success else 'FAILED'}")
    return result.__dict__


@task(name="e2e-setup")
def task_e2e_setup(worktree: dict, config_dict: dict | None) -> dict:
    """Levanta los servicios E2E desde el worker (tiene docker socket)."""
    logger = get_run_logger()
    if not config_dict or not config_dict.get("e2e_tests"):
        return {"success": True, "skipped": True}
    cfg = config_dict["e2e_tests"]
    if not cfg.get("setup"):
        return {"success": True, "skipped": True}

    logger.info(f"E2E setup: {cfg['setup']}")
    res = run_service_command(
        cfg["setup"], Path(worktree["host_path"]), timeout_sec=cfg.get("timeout", 900) // 3
    )
    if not res.success:
        logger.error(f"E2E setup FAILED: {res.stderr[-500:]}")
    return {"success": res.success, "stdout": res.stdout, "stderr": res.stderr}


@task(name="e2e-tests")
def task_e2e_tests(worktree: dict, config_dict: dict | None) -> dict:
    logger = get_run_logger()
    if not config_dict or not config_dict.get("e2e_tests"):
        logger.warning("No e2e_tests section in config; skipping")
        return skipped("no e2e_tests section").__dict__

    cfg = config_dict["e2e_tests"]
    logger.info(f"Running E2E tests: {cfg['command']}")
    result = run_command_in_runner(
        image=RUNNER_E2E_IMAGE,
        command=cfg["command"],
        host_worktree_path=Path(worktree["host_path"]),
        timeout_sec=cfg["timeout"],
    )
    logger.info(f"E2E tests {'PASSED' if result.success else 'FAILED'}")
    return result.__dict__


@task(name="e2e-teardown")
def task_e2e_teardown(worktree: dict, config_dict: dict | None) -> dict:
    """Tira servicios E2E. Siempre se ejecuta (incluso si los tests fallaron)."""
    logger = get_run_logger()
    if not config_dict or not config_dict.get("e2e_tests"):
        return {"success": True, "skipped": True}
    cfg = config_dict["e2e_tests"]
    if not cfg.get("teardown"):
        return {"success": True, "skipped": True}

    logger.info(f"E2E teardown: {cfg['teardown']}")
    res = run_service_command(
        cfg["teardown"], Path(worktree["host_path"]), timeout_sec=300
    )
    if not res.success:
        logger.warning(f"E2E teardown had issues: {res.stderr[-500:]}")
    return {"success": res.success}


# ---------------------------
# Flow principal
# ---------------------------

def _fmt_test_feedback(phase_name: str, result_dict: dict) -> str:
    """Formatea el output de un test fallido para mandar al implementer."""
    result = RunnerResult(**{k: v for k, v in result_dict.items() if k in RunnerResult.__dataclass_fields__})
    return f"{phase_name} failed.\n{result.summary_for_feedback()}"


@flow(name="pipeline", log_prints=True)
def pipeline_flow(
    task_id: int,
    prompt: str,
    repo_path: str,
    base_branch: str = "main",
    feature_branch: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> dict:
    logger = get_run_logger()

    if not feature_branch:
        feature_branch = f"pipeline/task-{task_id}"

    tasklog.init_log(task_id, prompt, repo_path, feature_branch)
    tasklog.append_orchestrator(task_id, f"Flow started (task_id={task_id})")

    # 1. Worktree
    worktree = task_create_worktree(task_id, repo_path, base_branch, feature_branch)

    # 2. Cargar config de tests
    config_dict = task_load_config(worktree)

    # 3. Instalar deps una vez (si hay config de install)
    if config_dict:
        task_install(worktree, config_dict)

    # 4. Bucle implementer → quick → reviewer → simplifier → full → e2e
    feedback: Optional[str] = None
    total_commits: list[str] = []

    for attempt in range(MAX_RETRY_ATTEMPTS + 1):
        logger.info(f"=== Attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS + 1} ===")
        tasklog.append_orchestrator(task_id, f"Attempt {attempt + 1}")

        # Implementer
        impl = task_implementer(
            task_id=task_id, prompt=prompt, base_branch=base_branch,
            feature_branch=feature_branch, model=model, worktree=worktree,
            previous_feedback=feedback,
        )
        total_commits.extend(impl.get("commits", []))
        if not impl["success"]:
            msg = f"Implementer failed: {impl['summary']}"
            tasklog.append_final(task_id, "failed", msg, "(no diff)")
            raise RuntimeError(msg)

        # Gate 1: quick tests
        quick = task_quick_tests(worktree, config_dict)
        if not quick.get("skipped") and not quick["success"]:
            if attempt < MAX_RETRY_ATTEMPTS:
                feedback = _fmt_test_feedback("Quick tests", quick)
                tasklog.append_orchestrator(task_id, "Quick tests failed, looping back to implementer")
                continue
            else:
                msg = "Quick tests failed after max retries"
                tasklog.append_final(task_id, "failed", msg, "(no diff)")
                raise RuntimeError(msg)

        # Reviewer
        rev = task_reviewer(
            task_id=task_id, prompt=prompt, base_branch=base_branch,
            feature_branch=feature_branch, model=model, worktree=worktree,
        )
        if not rev["success"] and rev.get("verdict") != "changes_requested":
            raise RuntimeError(f"Reviewer failed: {rev['summary']}")

        if rev.get("verdict") == "changes_requested":
            if attempt < MAX_RETRY_ATTEMPTS:
                feedback = rev.get("review_comments") or rev["summary"]
                tasklog.append_orchestrator(task_id, "Reviewer requested changes")
                continue
            else:
                msg = "Reviewer kept requesting changes after max retries"
                tasklog.append_final(task_id, "failed", msg, "(no diff)")
                raise RuntimeError(msg)

        # Simplifier
        simp = task_simplifier(
            task_id=task_id, prompt=prompt, base_branch=base_branch,
            feature_branch=feature_branch, model=model, worktree=worktree,
        )
        total_commits.extend(simp.get("commits", []))
        if not simp["success"]:
            logger.warning("Simplifier failed but code is already approved; continuing")

        # Gate 2: full tests
        full = task_full_tests(worktree, config_dict)
        if not full.get("skipped") and not full["success"]:
            if attempt < MAX_RETRY_ATTEMPTS:
                feedback = _fmt_test_feedback("Full tests", full)
                tasklog.append_orchestrator(task_id, "Full tests failed, looping back")
                continue
            else:
                msg = "Full tests failed after max retries"
                tasklog.append_final(task_id, "failed", msg, "(no diff)")
                raise RuntimeError(msg)

        # Gate 3: E2E (con setup + teardown siempre)
        if config_dict and config_dict.get("e2e_tests"):
            setup_res = task_e2e_setup(worktree, config_dict)
            if not setup_res["success"]:
                # Aun así intentamos teardown por limpieza
                task_e2e_teardown(worktree, config_dict)
                raise RuntimeError(f"E2E setup failed: {setup_res.get('stderr','')[-300:]}")

            try:
                e2e = task_e2e_tests(worktree, config_dict)
            finally:
                task_e2e_teardown(worktree, config_dict)

            if not e2e.get("skipped") and not e2e["success"]:
                if attempt < MAX_RETRY_ATTEMPTS:
                    feedback = _fmt_test_feedback("E2E tests", e2e)
                    tasklog.append_orchestrator(task_id, "E2E failed, looping back")
                    continue
                else:
                    msg = "E2E tests failed after max retries"
                    tasklog.append_final(task_id, "failed", msg, "(no diff)")
                    raise RuntimeError(msg)

        # Todo pasó. Salir del bucle.
        break

    else:
        # Si el for se agota sin break (no debería llegar aquí por las RuntimeError de arriba)
        raise RuntimeError("Max retries exhausted")

    # 5. Done
    diff_stat = get_diff_summary(Path(worktree["container_path"]), base_branch)
    tasklog.append_final(task_id, "done", "All gates passed", diff_stat)

    return {
        "task_id": task_id,
        "state": "done",
        "commits": total_commits,
        "diff_stat": diff_stat,
    }
