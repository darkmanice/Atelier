"""Ref sandbox: snapshot branches/tags before a task runs, verify on exit
that only the feature branch changed. Catches destructive operations like
`git branch -D`, `git reset --hard`, `git tag -d`, or branch switches that
commit elsewhere.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def snapshot_refs(project_path: Path) -> dict[str, str]:
    """Return {refname: sha} for every ref in the project (branches + tags)."""
    result = subprocess.run(
        ["git", "-C", str(project_path), "for-each-ref",
         "--format=%(refname) %(objectname)"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    refs: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        refname, _, sha = line.partition(" ")
        if refname:
            refs[refname] = sha
    return refs


def diff_refs(
    pre: dict[str, str],
    post: dict[str, str],
    feature_branch: str,
) -> list[str]:
    """Compare two snapshots. Return a list of human-readable violations.

    The feature branch is allowed to change freely (that is the whole point
    of the task). Any other ref creation/deletion/move is a violation.
    """
    allowed = f"refs/heads/{feature_branch}"
    violations: list[str] = []

    for refname, before in pre.items():
        if refname == allowed:
            continue
        after = post.get(refname)
        if after is None:
            violations.append(f"ref deleted: {refname} (was {before[:8]})")
        elif after != before:
            violations.append(
                f"ref moved: {refname} ({before[:8]} -> {after[:8]})"
            )

    for refname, after in post.items():
        if refname == allowed or refname in pre:
            continue
        violations.append(f"ref created: {refname} ({after[:8]})")

    return violations
