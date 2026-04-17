"""Policy module CRUD API — create, update, delete Rego files via Git commits.

Endpoints persist changes as local commits in the tracked policy clone so that
``GET /policy`` (including differential bundles via ``base_hash``) immediately
reflects the mutation.  When pubsub is available the webhook topic is published
to trigger the standard OPAL policy-watcher refresh flow.
"""

from __future__ import annotations
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from git.repo import Repo
from opal_common.logger import logger
from pydantic import BaseModel, Field
from symphony import tool

from opal_server.policy.bundles.api import get_repo
from opal_server.policy.module_ops import (
    PolicyModulePathError,
    delete_policy_module as delete_policy_module_from_repo,
    module_exists as policy_module_exists,
    upsert_policy_module,
)


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

def _to_http_exception(exc: Exception) -> HTTPException:
    """Normalize module operation errors into API-friendly HTTP exceptions."""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, PolicyModulePathError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Module not found: {exc.args[0]}",
        )
    if isinstance(exc, ValueError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=str(exc),
    )


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
        if policy_module_exists(repo, body.module_path):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Module already exists: {body.module_path}. "
                    "Use update_policy_module instead."
                ),
            )
        try:
            result = upsert_policy_module(
                repo,
                body.module_path,
                body.rego_content,
                body.commit_message,
            )
        except Exception as exc:
            raise _to_http_exception(exc) from exc

        await _notify_policy_change()

        return JSONResponse(
            {
                "action": result["action"],
                "module_path": result["module_path"],
                "old_hash": result["old_hash"],
                "new_hash": result["new_hash"],
            }
        )

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
        if not policy_module_exists(repo, body.module_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Module not found: {body.module_path}. "
                    "Use create_policy_module instead."
                ),
            )
        try:
            result = upsert_policy_module(
                repo,
                body.module_path,
                body.rego_content,
                body.commit_message,
            )
        except Exception as exc:
            raise _to_http_exception(exc) from exc

        await _notify_policy_change()

        return JSONResponse(
            {
                "action": result["action"],
                "module_path": result["module_path"],
                "old_hash": result["old_hash"],
                "new_hash": result["new_hash"],
            }
        )

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
        try:
            result = delete_policy_module_from_repo(
                repo,
                body.module_path,
                body.commit_message,
            )
        except Exception as exc:
            raise _to_http_exception(exc) from exc

        await _notify_policy_change()

        return JSONResponse(
            {
                "action": result["action"],
                "module_path": result["module_path"],
                "old_hash": result["old_hash"],
                "new_hash": result["new_hash"],
            }
        )

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
