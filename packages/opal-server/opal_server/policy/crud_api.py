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
from symphony import handle_extension, tool

from opal_server.policy.bundles.api import get_repo
from opal_server.policy.module_ops import (
    PolicyModulePathError,
    delete_policy_module as delete_policy_module_from_repo,
    module_exists as policy_module_exists,
    upsert_policy_module,
)
from opal_server.symphony_ext import (
    _policy_hotfix_capabilities,
    goex_registry,
    policy_hotfix,
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


class PolicyHotfixRequest(BaseModel):
    module_path: str = Field(
        ...,
        description="Repo-relative hotfix module path.",
    )
    commit_message: str = Field(
        "Apply policy hotfix",
        description="Git commit message for the hotfix mutation.",
    )
    package_name: str | None = Field(
        None,
        description="Expected package name for the hotfix module, if known.",
    )
    extension_level: str = Field(
        "L1",
        description="Symphony extension level for this predefined hotfix hook (L1-L3).",
    )
    extension_code: str | None = Field(
        None,
        description="Python extension code for L1, or CLI-generated code for L2/L3 fallback.",
    )
    task_description: str | None = Field(
        None,
        description="Natural-language task description for L2/L3 server-side generation.",
    )
    execution_mode: str = Field(
        "direct",
        description="Execution mode: direct or goex.",
    )
    reversal_code: str | None = Field(
        None,
        description="Undo code for GoEx mode.",
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


def _default_apply_policy_hotfix(body: PolicyHotfixRequest) -> dict:
    """Default hotfix hook source for L3 code-aware generation.

    The predefined hotfix endpoint exists to host extension code. Its baseline
    path intentionally does not mutate the policy repository.
    """
    return {
        "status": "requires_extension",
        "module_path": body.module_path,
        "commit_message": body.commit_message,
    }


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

    @tool(
        name="apply_policy_hotfix",
        method="POST",
        path="/policy/hotfix",
        levels=["L1", "L2", "L3"],
        level_params={
            "L1": [
                "module_path",
                "commit_message",
                "package_name",
                "extension_level",
                "extension_code",
                "execution_mode",
                "reversal_code",
            ],
            "L2": [
                "module_path",
                "commit_message",
                "package_name",
                "extension_level",
                "extension_code",
                "task_description",
                "execution_mode",
                "reversal_code",
            ],
            "L3": [
                "module_path",
                "commit_message",
                "package_name",
                "extension_level",
                "task_description",
                "execution_mode",
                "reversal_code",
            ],
        },
        level_overrides={
            "L1": {
                "description": (
                    "Predefined policy-hotfix extension hook. Provide extension_code that uses policy-hotfix "
                    "capabilities such as read_policy_module, module_exists, upsert_policy_module, and "
                    "delete_policy_module. Use execution_mode='goex' with reversal_code for reversible mutations."
                ),
            },
            "L2": {
                "description": (
                    "Predefined policy-hotfix extension hook with server-side codegen. Provide task_description "
                    "describing the policy module to create or update; include reversal_code when using GoEx."
                ),
            },
            "L3": {
                "description": (
                    "Source-aware policy-hotfix extension hook. Provide task_description; the code generator sees "
                    "this endpoint source and policy-hotfix capabilities."
                ),
            },
        },
    )
    @router.post("/policy/hotfix")
    async def apply_policy_hotfix(
        body: PolicyHotfixRequest = Body(...),
        repo: Repo = Depends(get_repo),
    ):
        """Run a predefined policy-hotfix extension hook for L1-L3."""
        if body.extension_level == "L4":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="L4 policy hotfixes must use /symphony/code_extension",
            )
        if body.extension_level not in {"L1", "L2", "L3"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="apply_policy_hotfix supports L1-L3 only",
            )

        provider = policy_hotfix.context_provider
        context = provider() if provider is not None else {}
        context.update(
            {
                "repo_path": repo.working_dir,
                "module_path": body.module_path,
                "commit_message": body.commit_message,
                "package_name": body.package_name,
            }
        )
        try:
            current_rego = None
            if policy_module_exists(repo, body.module_path):
                module_path = Path(repo.working_dir) / body.module_path
                current_rego = module_path.read_text(encoding="utf-8")
            context["current_rego"] = current_rego
            context["module_exists_before"] = current_rego is not None
        except Exception:
            context["current_rego"] = None
            context["module_exists_before"] = False

        outcome = await handle_extension(
            level=body.extension_level,
            extension_code=body.extension_code,
            task_description=body.task_description,
            execution_mode=body.execution_mode,
            reversal_code=body.reversal_code,
            extension_point=policy_hotfix,
            default_fn=lambda: _default_apply_policy_hotfix(body),
            context=context,
            all_capabilities=_policy_hotfix_capabilities,
            goex_registry=goex_registry,
            original_call=(
                "result = {"
                "'status': 'requires_extension', "
                "'module_path': context.get('module_path'), "
                "'commit_message': context.get('commit_message')"
                "}"
            ),
            default_source=_default_apply_policy_hotfix,
            endpoint_path="/policy/hotfix",
            trigger_condition=lambda res: bool(body.extension_code) or bool(body.task_description),
            default_mutates=True,
        )

        response: dict = {
            "results": outcome.results,
            "extension_triggered": outcome.ext_result.triggered,
            "generated_code": outcome.ext_result.generated_code,
            "endpoint_source": outcome.ext_result.endpoint_source,
            "needs_extension": outcome.needs_extension,
        }
        if outcome.ext_result.error:
            response["extension_error"] = outcome.ext_result.error
        if outcome.extension_context:
            response["extension_context"] = outcome.extension_context
        if outcome.ext_result.goex_record_id:
            response["goex_record_id"] = outcome.ext_result.goex_record_id
            response["goex_mode"] = body.execution_mode == "goex"
            response["goex_reversal_code"] = outcome.ext_result.goex_reversal_code

        await _notify_policy_change()
        return JSONResponse(response)

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
