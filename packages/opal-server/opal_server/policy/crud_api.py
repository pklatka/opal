"""Policy module CRUD API — create, update, delete Rego files via Git commits.

Endpoints persist changes as local commits in the tracked policy clone so that
``GET /policy`` (including differential bundles via ``base_hash``) immediately
reflects the mutation.  When pubsub is available the webhook topic is published
to trigger the standard OPAL policy-watcher refresh flow.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from git import Actor
from git.repo import Repo
from opal_common.logger import logger
from pydantic import BaseModel, Field
from symphony import tool

from opal_server.policy.bundles.api import get_repo

_COMMIT_AUTHOR = Actor("OPAL Symphony", "symphony@opal.local")


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class PolicyModuleCreate(BaseModel):
    module_path: str = Field(
        ...,
        description=(
            "Repo-relative path for the new module "
            "(e.g. 'compliance/emergency_block.rego')"
        ),
    )
    rego_content: str = Field(..., description="Raw Rego source code")
    commit_message: str = Field(
        "Create policy module", description="Git commit message"
    )


class PolicyModuleUpdate(BaseModel):
    module_path: str = Field(
        ..., description="Repo-relative path of the module to update"
    )
    rego_content: str = Field(..., description="New Rego source code")
    commit_message: str = Field(
        "Update policy module", description="Git commit message"
    )


class PolicyModuleDelete(BaseModel):
    module_path: str = Field(
        ..., description="Repo-relative path of the module to delete"
    )
    commit_message: str = Field(
        "Delete policy module", description="Git commit message"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_module_path(module_path: str, repo: Repo) -> Path:
    """Resolve *module_path* against the repo root, rejecting traversal and non-.rego."""
    normalized = PurePosixPath(module_path)
    if normalized.is_absolute():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="module_path must be relative",
        )

    repo_root = Path(repo.working_dir).resolve()
    resolved = (repo_root / normalized).resolve()
    if not str(resolved).startswith(str(repo_root) + os.sep):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="path traversal not allowed",
        )

    if not str(normalized).endswith(".rego"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="module_path must end with .rego",
        )

    return resolved


def _commit_staged(repo: Repo, message: str) -> tuple[str, str]:
    """Commit already-staged changes.  Returns ``(old_hash, new_hash)``."""
    old_hash = repo.head.commit.hexsha
    repo.index.commit(message, author=_COMMIT_AUTHOR, committer=_COMMIT_AUTHOR)
    return old_hash, repo.head.commit.hexsha


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def init_policy_crud_router(pubsub_endpoint=None):
    """Build and return the policy CRUD router.

    Args:
        pubsub_endpoint: Optional ``PubSubEndpoint`` — when provided, a
            notification is published on the webhook topic after each
            mutation so the policy watcher can pick up the change.
    """
    router = APIRouter()

    async def _notify_policy_change():
        if pubsub_endpoint is None:
            return
        try:
            from opal_server.config import opal_server_config

            await pubsub_endpoint.publish(
                opal_server_config.POLICY_REPO_WEBHOOK_TOPIC
            )
            logger.info("Published policy change notification")
        except Exception:
            logger.warning(
                "Failed to publish policy change notification", exc_info=True
            )

    # -- CREATE -------------------------------------------------------------

    @tool(name="create_policy_module", method="POST", path="/policy/modules")
    @router.post("/policy/modules")
    async def create_policy_module(
        body: PolicyModuleCreate = Body(...),
        repo: Repo = Depends(get_repo),
    ):
        """Create a new Rego policy module in the tracked Git repository.

        Writes the file and commits it locally. The new module is immediately
        visible in subsequent GET /policy bundle fetches.
        """
        file_path = _validate_module_path(body.module_path, repo)

        if file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Module already exists: {body.module_path}. "
                    "Use update_policy_module instead."
                ),
            )

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(body.rego_content)
        repo.index.add([str(PurePosixPath(body.module_path))])
        old_hash, new_hash = _commit_staged(repo, body.commit_message)

        await _notify_policy_change()

        return JSONResponse({
            "action": "created",
            "module_path": body.module_path,
            "old_hash": old_hash,
            "new_hash": new_hash,
        })

    # -- UPDATE -------------------------------------------------------------

    @tool(name="update_policy_module", method="PUT", path="/policy/modules")
    @router.put("/policy/modules")
    async def update_policy_module(
        body: PolicyModuleUpdate = Body(...),
        repo: Repo = Depends(get_repo),
    ):
        """Update an existing Rego policy module in the tracked Git repository.

        Overwrites the file contents and commits the change locally. The update
        is immediately visible in subsequent GET /policy bundle fetches.
        """
        file_path = _validate_module_path(body.module_path, repo)

        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Module not found: {body.module_path}. "
                    "Use create_policy_module instead."
                ),
            )

        file_path.write_text(body.rego_content)
        repo.index.add([str(PurePosixPath(body.module_path))])
        old_hash, new_hash = _commit_staged(repo, body.commit_message)

        await _notify_policy_change()

        return JSONResponse({
            "action": "updated",
            "module_path": body.module_path,
            "old_hash": old_hash,
            "new_hash": new_hash,
        })

    # -- DELETE -------------------------------------------------------------

    @tool(name="delete_policy_module", method="DELETE", path="/policy/modules")
    @router.delete("/policy/modules")
    async def delete_policy_module(
        body: PolicyModuleDelete = Body(...),
        repo: Repo = Depends(get_repo),
    ):
        """Delete a Rego policy module from the tracked Git repository.

        Removes the file and commits the deletion locally. The module will no
        longer appear in subsequent GET /policy bundle fetches, and will show
        in deleted_files when fetching a differential bundle via base_hash.
        """
        file_path = _validate_module_path(body.module_path, repo)

        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Module not found: {body.module_path}",
            )

        repo.index.remove(
            [str(PurePosixPath(body.module_path))], working_tree=True
        )
        old_hash, new_hash = _commit_staged(repo, body.commit_message)

        parent = file_path.parent
        repo_root = Path(repo.working_dir)
        while parent != repo_root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

        await _notify_policy_change()

        return JSONResponse({
            "action": "deleted",
            "module_path": body.module_path,
            "old_hash": old_hash,
            "new_hash": new_hash,
        })

    # -- LIST ---------------------------------------------------------------

    @tool(name="list_policy_modules", method="GET", path="/policy/modules")
    @router.get("/policy/modules")
    async def list_policy_modules(repo: Repo = Depends(get_repo)):
        """List all Rego policy modules tracked in the Git repository.

        Returns file paths, sizes, and the current HEAD commit hash.
        """
        repo_root = Path(repo.working_dir)
        modules = []
        for rego_file in sorted(repo_root.rglob("*.rego")):
            rel_path = rego_file.relative_to(repo_root)
            modules.append({
                "path": str(PurePosixPath(rel_path)),
                "size": rego_file.stat().st_size,
            })
        return JSONResponse({
            "modules": modules,
            "count": len(modules),
            "hash": repo.head.commit.hexsha,
        })

    return router
