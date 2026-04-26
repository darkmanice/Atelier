"""
Agent pipeline as a Prefect flow, with test gates.

Phases:
  1. install (if there is a config)
  2. implementer
  3. quick_tests (gate — if it fails, feedback goes to the implementer)
  4. reviewer
  5. simplifier
  6. full_tests (gate — if it fails, feedback goes to the implementer)
  7. setup_services_e2e + e2e_tests + teardown_services_e2e (final gate)

Each deterministic gate can loop back to the implementer with feedback.
Total number of iterations limited by MAX_RETRY_ATTEMPTS.
"""
from __future__ import annotations

import os
from pathlib import Path
from string import Template
from typing import Optional

from prefect import flow, get_run_logger, task

from agents.models import AgentResult, AgentRole, TaskInput
from orchestrator import logger as tasklog
from orchestrator import preview as preview_state
from orchestrator.config import DEFAULT_MODEL
from orchestrator.container import cleanup_task_containers, run_agent
from orchestrator.llm_config import fetch_api_key
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


MAX_RETRY_ATTEMPTS = int(os.environ.get("MAX_RETRY_ATTEMPTS", "2"))

# pytest returns this exit code when it collects no tests. We treat it as
# "skip" in the gates so a repo without tests does not make the pipeline fail.
PYTEST_EXIT_NO_TESTS_COLLECTED = 5


# ---------------------------
# Tasks — agent phases
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
        # Repo path as the worker sees it (e.g. /projects/foo). Carried in
        # the dict so runner/agent containers can mount ONLY this project.
        "repo_container_path": str(repo_path),
        # Branch the task is allowed to modify. Used by the ref sandbox to
        # reject destructive operations on any other ref.
        "feature_branch": feature_branch,
    }


def _run_agent_task(
    role: AgentRole,
    task_id: int,
    prompt: str,
    base_branch: str,
    feature_branch: str,
    provider_label: str,
    base_url: str,
    model: str,
    api_key: str,
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
        provider_label=provider_label,
        base_url=base_url,
        model=model,
    )
    # api_key does NOT go into TaskInput. It is passed separately so that
    # run_agent injects it directly as container env, without touching
    # .task-input.json.
    result = run_agent(
        task_input,
        Path(worktree["host_path"]),
        Path(worktree["container_path"]),
        Path(worktree["repo_container_path"]),
        api_key=api_key,
    )
    tasklog.append_agent_result(task_id, role.value, result, model=model)
    return result.model_dump()


@task(name="implementer")
def task_implementer(**kwargs) -> dict:
    return _run_agent_task(AgentRole.IMPLEMENTER, **kwargs)


@task(name="reviewer")
def task_reviewer(**kwargs) -> dict:
    return _run_agent_task(AgentRole.REVIEWER, **kwargs)


@task(name="simplifier")
def task_simplifier(**kwargs) -> dict:
    return _run_agent_task(AgentRole.SIMPLIFIER, **kwargs)


# ---------------------------
# Tasks — test phases
# ---------------------------

@task(name="load-config")
def task_load_config(worktree: dict) -> dict | None:
    logger = get_run_logger()
    container_path = Path(worktree["container_path"])
    try:
        config = load_config(container_path)
    except ValueError as e:
        logger.error(f"Invalid .atelier.yml: {e}")
        return None

    if config is None:
        logger.warning(
            ".atelier.yml not found in repo root. "
            "No tests will be executed. Pipeline will continue without test gates."
        )
        return None

    logger.info("Loaded .atelier.yml")
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
        container_repo_path=Path(worktree["repo_container_path"]),
        feature_branch=worktree["feature_branch"],
        timeout_sec=cfg["timeout"],
    )
    if not result.success:
        logger.error(f"Install failed (exit {result.exit_code})")
    return result.__dict__


def _finalize_test_result(result: RunnerResult, phase: str, logger) -> dict:
    """
    Logs and applies the skip-if-no-tests policy.

    pytest exit 5 = 'no tests collected'. We treat it as skip by explicit
    decision: if the user has not yet created tests for an endpoint, we do
    not want the gate to fail nor the implementer to start inventing tests;
    the user will write them when they see fit.
    """
    if result.exit_code == PYTEST_EXIT_NO_TESTS_COLLECTED:
        logger.warning(
            f"{phase}: no tests collected (pytest exit 5); treating as skip"
        )
        return skipped(f"no tests collected ({phase})").__dict__
    logger.info(
        f"{phase} {'PASSED' if result.success else 'FAILED'} "
        f"(exit {result.exit_code}, {result.duration_seconds:.1f}s)"
    )
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
        container_repo_path=Path(worktree["repo_container_path"]),
        feature_branch=worktree["feature_branch"],
        timeout_sec=cfg["timeout"],
    )
    return _finalize_test_result(result, "Quick tests", logger)


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
        container_repo_path=Path(worktree["repo_container_path"]),
        feature_branch=worktree["feature_branch"],
        timeout_sec=cfg["timeout"],
    )
    return _finalize_test_result(result, "Full tests", logger)


@task(name="e2e-setup")
def task_e2e_setup(worktree: dict, config_dict: dict | None) -> dict:
    """Brings up E2E services from the worker (which has the docker socket)."""
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
        container_repo_path=Path(worktree["repo_container_path"]),
        feature_branch=worktree["feature_branch"],
        timeout_sec=cfg["timeout"],
    )
    return _finalize_test_result(result, "E2E tests", logger)


@task(name="preview-up")
def task_preview_up(task_id: int, worktree: dict, config_dict: dict | None) -> dict:
    """
    Brings up the preview after all gates pass. Allocates a free port, runs
    `preview.up` with cwd=host_worktree and TASK_ID / PREVIEW_PORT as env.
    Persists a sidecar with what is needed for teardown.
    """
    logger = get_run_logger()
    if not config_dict or not config_dict.get("preview"):
        logger.info("No preview section; skipping")
        return {"started": False}

    cfg = config_dict["preview"]
    port = preview_state.allocate_port()
    env = {
        "TASK_ID": str(task_id),
        "PREVIEW_PORT": str(port),
    }
    logger.info(f"Preview up: {cfg['up']} (port {port})")

    res = run_service_command(
        cfg["up"],
        Path(worktree["host_path"]),
        timeout_sec=cfg.get("timeout", 180),
        env=env,
    )
    if not res.success:
        logger.error(f"Preview up FAILED: {(res.stderr or '')[-500:]}")
        tasklog.append_orchestrator(task_id, f"Preview up failed: {(res.stderr or '')[-200:]}")
        return {"started": False, "error": res.stderr[-500:] if res.stderr else ""}

    url = Template(cfg["url"]).safe_substitute(env)
    preview_state.save_sidecar(task_id, {
        "task_id": task_id,
        "port": port,
        "url": url,
        "down": cfg["down"],
        "host_worktree_path": str(worktree["host_path"]),
        "env": env,
    })

    logger.info(f"Preview running at {url}")
    tasklog.append_orchestrator(task_id, f"Preview available at {url}")
    return {"started": True, "url": url, "port": port}


@task(name="e2e-teardown")
def task_e2e_teardown(worktree: dict, config_dict: dict | None) -> dict:
    """Tears down E2E services. Always runs (even if the tests failed)."""
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
# Main flow
# ---------------------------

def _fmt_test_feedback(phase_name: str, result_dict: dict) -> str:
    """Formats the output of a failed test to send to the implementer."""
    result = RunnerResult(**{k: v for k, v in result_dict.items() if k in RunnerResult.__dataclass_fields__})
    return f"{phase_name} failed.\n{result.summary_for_feedback()}"


# Default spec used when the caller does not supply one. Mirrors the legacy
# linear pipeline so the API stays backward-compatible (curl callers without
# a spec keep their behaviour).
_DEFAULT_PIPELINE_SPEC: list[dict] = [
    {"type": t, "id": f"default-{t}"}
    for t in (
        "implementer", "quick-tests", "reviewer", "simplifier",
        "full-tests", "e2e-tests", "preview",
    )
]

_VALID_BLOCK_TYPES = frozenset({
    "implementer", "reviewer", "simplifier",
    "quick-tests", "full-tests", "e2e-tests", "preview",
})


class _RestartAttempt(Exception):
    """Raised by a block runner to signal: restart from the beginning of the
    spec on the next attempt. Carries the feedback to seed the next loop."""
    def __init__(self, feedback: str, log_message: str):
        super().__init__(log_message)
        self.feedback = feedback
        self.log_message = log_message


class _AbortPipeline(Exception):
    """Raised when a block hits an unrecoverable failure (no retry possible)."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _normalize_spec(raw: Optional[list[dict]]) -> list[dict]:
    """Drops unknown block types and assigns a deterministic id when missing.
    Empty / None → the default sequence so old API callers keep working."""
    if not raw:
        return list(_DEFAULT_PIPELINE_SPEC)
    out: list[dict] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t not in _VALID_BLOCK_TYPES:
            continue
        rid = item.get("id")
        sid = rid if isinstance(rid, str) and rid else f"{t}-{idx}"
        out.append({"type": t, "id": sid})
    return out or list(_DEFAULT_PIPELINE_SPEC)


@flow(name="atelier", log_prints=True)
def pipeline_flow(
    task_id: int,
    prompt: str,
    repo_path: str,
    base_branch: str = "main",
    feature_branch: Optional[str] = None,
    provider_label: str = "custom",
    base_url: str = "",
    model_implementer: str = DEFAULT_MODEL,
    model_reviewer: str = DEFAULT_MODEL,
    model_simplifier: str = DEFAULT_MODEL,
    secret_token: Optional[str] = None,
    pipeline_spec: Optional[list[dict]] = None,
) -> dict:
    logger = get_run_logger()

    if not feature_branch:
        feature_branch = f"pipeline/task-{task_id}"

    spec = _normalize_spec(pipeline_spec)

    tasklog.init_log(task_id, prompt, repo_path, feature_branch)
    tasklog.append_llm_config(
        task_id,
        provider_label=provider_label,
        base_url=base_url,
        model_implementer=model_implementer,
        model_reviewer=model_reviewer,
        model_simplifier=model_simplifier,
    )
    tasklog.append_orchestrator(task_id, f"Flow started (task_id={task_id})")
    tasklog.append_orchestrator(
        task_id,
        "Pipeline spec: " + " → ".join(s["type"] for s in spec),
    )

    # Consume the orchestrator api_key once, at flow start. It lives only in
    # this flow's stack and is passed as an arg to each run_agent. It never
    # appears in Prefect parameters nor in .task-input.json.
    api_key = fetch_api_key(secret_token)

    # Connection args common to the 3 roles. The model travels separately, per role.
    _conn_kwargs = {
        "provider_label": provider_label,
        "base_url": base_url,
        "api_key": api_key,
    }

    # NOTE: agent containers (labeled `atelier.task=<task_id>`) deliberately
    # SURVIVE the end of this flow run. They are removed only when the user
    # calls `DELETE /tasks/{flow_run_id}`, which fires `cleanup-task/cleanup`
    # on the worker. This lets the user `docker exec -it atelier-agent-...`
    # against a finished task to inspect state, and reuse the warm runtime
    # if the same task is retried later.

    # 1. Worktree (always — structural, hidden from the canvas)
    worktree = task_create_worktree(task_id, repo_path, base_branch, feature_branch)

    # 2. Load test config
    config_dict = task_load_config(worktree)

    # 3. Install deps once (if there is an install config)
    if config_dict:
        task_install(worktree, config_dict)

    # 4. Run the user-defined spec inside the retry loop.
    feedback: Optional[str] = None
    total_commits: list[str] = []
    preview_info: dict = {"started": False}

    def _run_step(step: dict, attempt_n: int) -> None:
        """Execute one block. Mutates total_commits/preview_info; raises
        _RestartAttempt for soft failures and _AbortPipeline for hard ones."""
        nonlocal preview_info
        t = step["type"]
        sid = step["id"]
        suffix = f"{sid}-attempt-{attempt_n}"

        if t == "implementer":
            res = task_implementer.with_options(name=f"implementer-{suffix}")(
                task_id=task_id, prompt=prompt, base_branch=base_branch,
                feature_branch=feature_branch, worktree=worktree,
                previous_feedback=feedback, model=model_implementer, **_conn_kwargs,
            )
            total_commits.extend(res.get("commits", []))
            if not res["success"]:
                raise _RestartAttempt(
                    feedback=f"Implementer failed: {res['summary']}",
                    log_message=f"Implementer failed, looping back: {res['summary'][:200]}",
                )
            return

        if t == "reviewer":
            res = task_reviewer.with_options(name=f"reviewer-{suffix}")(
                task_id=task_id, prompt=prompt, base_branch=base_branch,
                feature_branch=feature_branch, worktree=worktree,
                model=model_reviewer, **_conn_kwargs,
            )
            if not res["success"] and res.get("verdict") != "changes_requested":
                raise _AbortPipeline(f"Reviewer failed: {res['summary']}")
            if res.get("verdict") == "changes_requested":
                raise _RestartAttempt(
                    feedback=res.get("review_comments") or res["summary"],
                    log_message="Reviewer requested changes",
                )
            return

        if t == "simplifier":
            res = task_simplifier.with_options(name=f"simplifier-{suffix}")(
                task_id=task_id, prompt=prompt, base_branch=base_branch,
                feature_branch=feature_branch, worktree=worktree,
                model=model_simplifier, **_conn_kwargs,
            )
            total_commits.extend(res.get("commits", []))
            if not res["success"]:
                # Same policy as the old flow: simplifier issues are non-fatal,
                # the diff was already approved by the reviewer (or there was
                # no reviewer in this spec).
                logger.warning("Simplifier failed; continuing")
            return

        if t == "quick-tests":
            res = task_quick_tests.with_options(name=f"quick-tests-{suffix}")(
                worktree, config_dict
            )
            if not res.get("skipped") and not res["success"]:
                raise _RestartAttempt(
                    feedback=_fmt_test_feedback("Quick tests", res),
                    log_message="Quick tests failed, restarting from spec start",
                )
            return

        if t == "full-tests":
            res = task_full_tests.with_options(name=f"full-tests-{suffix}")(
                worktree, config_dict
            )
            if not res.get("skipped") and not res["success"]:
                raise _RestartAttempt(
                    feedback=_fmt_test_feedback("Full tests", res),
                    log_message="Full tests failed, restarting from spec start",
                )
            return

        if t == "e2e-tests":
            # e2e setup/teardown are structural — they always wrap the gate
            # so the runner never sees a half-up environment. teardown runs
            # even if the gate raises.
            if not (config_dict and config_dict.get("e2e_tests")):
                # Nothing to set up; the gate task will report skipped.
                _ = task_e2e_tests.with_options(name=f"e2e-tests-{suffix}")(
                    worktree, config_dict
                )
                return

            setup_res = task_e2e_setup.with_options(name=f"e2e-setup-{suffix}")(
                worktree, config_dict
            )
            if not setup_res["success"]:
                task_e2e_teardown.with_options(name=f"e2e-teardown-{suffix}")(
                    worktree, config_dict
                )
                raise _AbortPipeline(
                    f"E2E setup failed: {setup_res.get('stderr','')[-300:]}"
                )

            try:
                res = task_e2e_tests.with_options(name=f"e2e-tests-{suffix}")(
                    worktree, config_dict
                )
            finally:
                task_e2e_teardown.with_options(name=f"e2e-teardown-{suffix}")(
                    worktree, config_dict
                )

            if not res.get("skipped") and not res["success"]:
                raise _RestartAttempt(
                    feedback=_fmt_test_feedback("E2E tests", res),
                    log_message="E2E failed, restarting from spec start",
                )
            return

        if t == "preview":
            preview_info = task_preview_up.with_options(name=f"preview-{suffix}")(
                task_id, worktree, config_dict
            )
            return

        # Should be unreachable thanks to _normalize_spec.
        raise _AbortPipeline(f"Unknown block type: {t}")

    completed = False
    for attempt in range(MAX_RETRY_ATTEMPTS + 1):
        n = attempt + 1
        logger.info(f"=== Attempt {n}/{MAX_RETRY_ATTEMPTS + 1} ===")
        tasklog.append_orchestrator(task_id, f"Attempt {n}")

        try:
            for step in spec:
                _run_step(step, n)
        except _RestartAttempt as restart:
            tasklog.append_orchestrator(task_id, restart.log_message)
            if attempt < MAX_RETRY_ATTEMPTS:
                feedback = restart.feedback
                continue
            msg = f"{restart.log_message} (max retries exhausted)"
            tasklog.append_final(task_id, "failed", msg, "(no diff)")
            raise RuntimeError(msg)
        except _AbortPipeline as abort:
            tasklog.append_final(task_id, "failed", abort.message, "(no diff)")
            raise RuntimeError(abort.message)

        # Spec ran clean.
        completed = True
        break

    if not completed:
        # Should not be reachable: the loop either breaks (success) or raises.
        raise RuntimeError("Max retries exhausted")

    # 5. Done
    diff_stat = get_diff_summary(Path(worktree["container_path"]), base_branch)
    tasklog.append_final(task_id, "done", "All gates passed", diff_stat)

    return {
        "task_id": task_id,
        "state": "done",
        "commits": total_commits,
        "diff_stat": diff_stat,
        "preview": preview_info,
    }


# ---------------------------
# Task cleanup flow (DELETE /tasks/{id})
# ---------------------------

@flow(name="cleanup-task", log_prints=True)
def cleanup_task_flow(task_id: int) -> dict:
    """
    Triggered by `DELETE /tasks/{flow_run_id}`. Runs on the worker (which is
    the one with the Docker socket and the worktree mount) and:
      1. Tears down the preview if one is active.
      2. Removes every container labeled `atelier.task=<task_id>` (agent
         containers from the pipeline run).

    The worktree directory and the markdown task log are intentionally kept;
    deletion here is about freeing live resources (containers, ports).
    """
    logger = get_run_logger()

    # 1. Preview (best-effort).
    preview_torn_down = False
    state = preview_state.load_sidecar(task_id)
    if state is not None:
        logger.info(f"Tearing down preview for task {task_id} (port {state['port']})")
        try:
            res = run_service_command(
                state["down"],
                Path(state["host_worktree_path"]),
                timeout_sec=300,
                env=state.get("env") or {},
            )
            if not res.success:
                logger.warning(f"Preview down had issues: {(res.stderr or '')[-500:]}")
            preview_torn_down = res.success
        except Exception as e:
            logger.warning(f"Preview teardown raised: {e}")
        finally:
            preview_state.delete_sidecar(task_id)

    # 2. Containers.
    removed = cleanup_task_containers(task_id)
    logger.info(f"Removed {removed} agent container(s) for task {task_id}")

    tasklog.append_orchestrator(
        task_id,
        f"Task deleted (containers removed={removed}, preview torn down={preview_torn_down})",
    )
    return {
        "task_id": task_id,
        "containers_removed": removed,
        "preview_torn_down": preview_torn_down,
    }
