"""Shared Git-backed policy module operations for CRUD and hotfix flows."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from git import Actor
from git.repo import Repo

_COMMIT_AUTHOR = Actor("OPAL CAGE", "cage@opal.local")


class PolicyModulePathError(ValueError):
    """Raised when a repo-relative policy module path is invalid."""


def normalize_rego_content(rego_content: str) -> str:
    """Normalize simple Rego v1 rule syntax for OPAL clients expecting v0 syntax."""
    rego_content = re.sub(
        r"(?m)^(\s*default\s+[A-Za-z_][A-Za-z0-9_]*)\s*:=\s*",
        r"\1 = ",
        rego_content,
    )
    rego_content = re.sub(
        r"(?m)^(\s*[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])?(?:\([^)\n]*\))?)\s+if\s*\{",
        r"\1 {",
        rego_content,
    )
    return rego_content


def validate_module_path(module_path: str, repo: Repo) -> Path:
    """Resolve *module_path* against the repo root, rejecting traversal."""
    if "," in module_path:
        raise PolicyModulePathError("module_path must not contain commas")
    normalized = PurePosixPath(module_path)
    if normalized.is_absolute():
        raise PolicyModulePathError("module_path must be relative")

    repo_root = Path(repo.working_dir).resolve()
    resolved = (repo_root / normalized).resolve()
    if not str(resolved).startswith(str(repo_root) + os.sep):
        raise PolicyModulePathError("path traversal not allowed")
    if not str(normalized).endswith(".rego"):
        raise PolicyModulePathError("module_path must end with .rego")
    return resolved


def delete_comma_named_rego_modules(repo: Repo, commit_message: str) -> dict:
    """Delete legacy malformed Rego modules whose repo paths contain commas."""
    tracked_paths = repo.git.ls_files("*.rego").splitlines()
    bad_paths = sorted(path for path in tracked_paths if "," in path)
    if not bad_paths:
        head_hash = repo.head.commit.hexsha
        return {
            "action": "noop",
            "module_paths": [],
            "old_hash": head_hash,
            "new_hash": head_hash,
        }

    repo.index.remove(bad_paths, working_tree=True)
    old_hash, new_hash = commit_staged(repo, commit_message)
    return {
        "action": "deleted",
        "module_paths": bad_paths,
        "old_hash": old_hash,
        "new_hash": new_hash,
    }


def commit_staged(repo: Repo, message: str) -> tuple[str, str]:
    """Commit already-staged changes. Returns ``(old_hash, new_hash)``."""
    old_hash = repo.head.commit.hexsha
    repo.index.commit(message, author=_COMMIT_AUTHOR, committer=_COMMIT_AUTHOR)
    return old_hash, repo.head.commit.hexsha


def read_policy_module(repo: Repo, module_path: str) -> str | None:
    """Return the current module contents, or ``None`` when absent."""
    file_path = validate_module_path(module_path, repo)
    if not file_path.exists():
        return None
    return file_path.read_text(encoding="utf-8")


def module_exists(repo: Repo, module_path: str) -> bool:
    """True when *module_path* exists in the tracked repo clone."""
    file_path = validate_module_path(module_path, repo)
    return file_path.exists()


def upsert_policy_module(
    repo: Repo,
    module_path: str,
    rego_content: str,
    commit_message: str,
) -> dict:
    """Create or replace a module and commit the change."""
    if rego_content is None:
        raise ValueError("rego_content is required")
    rego_content = normalize_rego_content(rego_content)

    file_path = validate_module_path(module_path, repo)
    existed_before = file_path.exists()
    previous_rego = file_path.read_text(encoding="utf-8") if existed_before else None

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(rego_content, encoding="utf-8")
    repo.index.add([str(PurePosixPath(module_path))])
    old_hash, new_hash = commit_staged(repo, commit_message)

    return {
        "action": "updated" if existed_before else "created",
        "module_path": module_path,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "module_exists_before": existed_before,
        "module_exists_after": True,
        "previous_rego": previous_rego,
        "rego_content": rego_content,
    }


def delete_policy_module(
    repo: Repo,
    module_path: str,
    commit_message: str,
    *,
    missing_ok: bool = False,
) -> dict:
    """Delete a module and commit the change."""
    file_path = validate_module_path(module_path, repo)
    if not file_path.exists():
        if missing_ok:
            head_hash = repo.head.commit.hexsha
            return {
                "action": "noop",
                "module_path": module_path,
                "old_hash": head_hash,
                "new_hash": head_hash,
                "module_exists_before": False,
                "module_exists_after": False,
                "previous_rego": None,
            }
        raise FileNotFoundError(module_path)

    previous_rego = file_path.read_text(encoding="utf-8")
    repo.index.remove([str(PurePosixPath(module_path))], working_tree=True)
    old_hash, new_hash = commit_staged(repo, commit_message)

    parent = file_path.parent
    repo_root = Path(repo.working_dir)
    while parent != repo_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent

    return {
        "action": "deleted",
        "module_path": module_path,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "module_exists_before": True,
        "module_exists_after": False,
        "previous_rego": previous_rego,
    }
