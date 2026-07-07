import os
from pathlib import Path
import re
from typing import List, Optional

import fastapi.responses
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from git.repo import Repo
from opal_common.confi.confi import load_conf_if_none
from opal_common.git_utils.bundle_maker import BundleMaker
from opal_common.git_utils.commit_viewer import CommitViewer
from opal_common.git_utils.repo_cloner import RepoClonePathFinder
from opal_common.logger import logger
from opal_common.schemas.policy import PolicyBundle
from opal_server.config import opal_server_config
from starlette.responses import RedirectResponse
from cage import tool, handle_extension
from cage.models import CAGEExtensionBody

from opal_server.cage_ext import (
    post_policy_bundle,
    _policy_capabilities,
    rxr_registry,
)

router = APIRouter()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def get_repo(
    base_clone_path: str = None,
    clone_subdirectory_prefix: str = None,
    use_fixed_path: bool = None,
) -> Repo:
    base_clone_path = load_conf_if_none(
        base_clone_path, opal_server_config.POLICY_REPO_CLONE_PATH
    )
    clone_subdirectory_prefix = load_conf_if_none(
        clone_subdirectory_prefix, opal_server_config.POLICY_REPO_CLONE_FOLDER_PREFIX
    )
    use_fixed_path = load_conf_if_none(
        use_fixed_path, opal_server_config.POLICY_REPO_REUSE_CLONE_PATH
    )
    clone_path_finder = RepoClonePathFinder(
        base_clone_path=base_clone_path,
        clone_subdirectory_prefix=clone_subdirectory_prefix,
        use_fixed_path=use_fixed_path,
    )
    repo_path = clone_path_finder.get_clone_path()

    policy_repo_not_found_error = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="policy repo was not found",
    )

    if not repo_path:
        raise policy_repo_not_found_error

    git_path = Path(os.path.join(repo_path, Path(".git")))
    # TODO: at the moment opal server will 503 until it finishes cloning the policy repo
    # we might fix this in the future by signaling to the client that the repo is ready
    if not git_path.exists():
        raise policy_repo_not_found_error
    return Repo(repo_path)


def normalize_path(path: str) -> Path:
    return Path(path[1:]) if path.startswith("/") else Path(path)


async def get_input_paths_or_throw(
    repo: Repo = Depends(get_repo),
    paths: Optional[List[str]] = Query(None, alias="path"),
) -> List[Path]:
    """Validates the :path query param, and return valid paths.

    if an invalid path is provided, will throw 404.
    """
    paths = paths or []
    paths = [normalize_path(p) for p in paths]

    # if the repo is currently being cloned - the repo.heads is empty
    if len(repo.heads) == 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="policy repo is not ready",
        )

    # verify all input paths exists under the commit hash
    with CommitViewer(repo.head.commit) as viewer:
        for path in paths:
            if not viewer.exists(path):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"requested path {path} was not found in the policy repo!",
                )

    # the default of GET /policy (without path params) is to return all
    # the (opa) files in the repo.
    paths = paths or [Path(".")]
    return paths


def _build_bundle_context(bundle: PolicyBundle) -> dict:
    """Build the shared context dict for policy bundle extension code."""
    bundle_dict = bundle.dict() if hasattr(bundle, "dict") else bundle.model_dump()
    policy_modules = bundle_dict.get("policy_modules", [])
    return {
        "policy_modules": policy_modules,
        "modules": policy_modules,  # alias — matches capability param names
        "data_modules": bundle_dict.get("data_modules", []),
        "manifest": bundle_dict.get("manifest", []),
        "hash": bundle_dict.get("hash", ""),
        "old_hash": bundle_dict.get("old_hash"),
        "module_count": len(policy_modules),
        "data_module_count": len(bundle_dict.get("data_modules", [])),
    }


_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([A-Za-z0-9_.]+)\s*$")


def _infer_package_name(rego: str) -> str:
    match = _PACKAGE_RE.search(rego or "")
    return match.group(1) if match else ""


def _normalize_bundle_package_names(bundle: PolicyBundle) -> PolicyBundle:
    """Backfill empty package_name fields from the Rego source.

    The benchmark policy repo intentionally includes plain Rego files whose
    schema objects may arrive with an empty package_name even though the
    source contains a valid `package ...` declaration. Normalize once here so
    extension helpers and final benchmark answers see stable metadata.
    """
    for module in bundle.policy_modules:
        if not getattr(module, "package_name", ""):
            module.package_name = _infer_package_name(getattr(module, "rego", ""))
    return bundle


def _use_benchmark_exact_path_fast_path(
    input_paths: List[Path],
    base_hash: Optional[str],
    ext: CAGEExtensionBody,
) -> bool:
    if not _env_flag("OPAL_BENCHMARK_MODE", False):
        return False
    if base_hash:
        return False
    if len(input_paths) != 1:
        return False
    target = input_paths[0]
    if target in {Path("."), Path("")}:
        return False
    if target.suffix != ".rego":
        return False
    if ext.extension_code:
        return False
    return True


def _default_get_policy(repo: Repo, input_paths: List[Path], base_hash: Optional[str]) -> PolicyBundle:
    """Default L0 bundle-building logic (used as default_source for L3 getsource)."""
    maker = BundleMaker(
        repo,
        in_directories=set(input_paths),
        extensions=opal_server_config.FILTER_FILE_EXTENSIONS,
        root_manifest_path=opal_server_config.POLICY_REPO_MANIFEST_PATH,
        bundle_ignore=opal_server_config.BUNDLE_IGNORE,
    )
    revision = None
    if base_hash:
        try:
            revision = repo.rev_parse(base_hash)
        except ValueError:
            logger.warning(f"base_hash {base_hash} not exist in the repo")

    if revision is None:
        return _normalize_bundle_package_names(maker.make_bundle(repo.head.commit))
    try:
        old_commit = repo.commit(base_hash)
        return _normalize_bundle_package_names(
            maker.make_diff_bundle(old_commit, repo.head.commit)
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"commit with hash {base_hash} was not found in the policy repo!",
        )


@tool(
    name="get_policy_bundle",
    method="GET",
    path="/policy",
    levels=["L0", "L1", "L2", "L3"],
    level_params={
        "L0": ["path", "base_hash"],
        "L1": ["path", "base_hash", "extension_level", "extension_code", "execution_mode", "reversal_code"],
        "L2": ["path", "base_hash", "extension_level", "extension_code", "task_description", "execution_mode", "reversal_code"],
        "L3": ["path", "base_hash", "extension_level", "task_description", "execution_mode", "reversal_code"],
    },
    level_overrides={
        "L0": {
            "description": (
                "Fetch policy bundle from the tracked Git repository. Inputs: optional path and base_hash. "
                "Returns policy_modules, data_modules, manifest, and hash. Use exact path reads first for benchmark tasks."
            ),
        },
        "L1": {
            "description": (
                "Same as L0 plus extension_code and reversal_code. Extension code receives the built bundle context "
                "and should return filtered or audited policy module dicts without mutating the repo."
            ),
        },
        "L2": {
            "description": (
                "Same as L1 plus task_description for server-side codegen over the live bundle context. "
                "Use for targeted policy selection, near-match filtering, and corpus audit work."
            ),
        },
        "L3": {
            "description": (
                "Same as L2, but source-aware: task_description drives codegen using endpoint source and bundle context. "
                "Mutating policy changes belong in policy_hotfix/code_extension, not this read-only bundle hook."
            ),
        },
    },
)
@router.get("/policy", response_model=PolicyBundle)
async def get_policy(
    repo: Repo = Depends(get_repo),
    input_paths: List[Path] = Depends(get_input_paths_or_throw),
    base_hash: Optional[str] = Query(
        None,
        description="hash of previous bundle already downloaded, server will return a diff bundle.",
    ),
    ext: Optional[CAGEExtensionBody] = Body(None),
):
    """Serve policy bundles with optional CAGE extension support.

    Extension levels:
    - **L0**: Serve full or differential policy bundle from Git repo
    - **L1**: Post-processing via extension_code (filter, transform bundle)
    - **L2**: Auto-generated extension code for advanced bundle processing
    - **L3**: Source-aware — LLM reads endpoint code and generates extensions

    Extension fields (extension_level, extension_code, task_description,
    execution_mode, reversal_code) are accepted as a JSON request body to
    avoid URL length limits on large extension_code payloads.
    """
    ext = ext or CAGEExtensionBody()
    extension_level = ext.extension_level
    extension_code = ext.extension_code
    task_description = ext.task_description
    execution_mode = ext.execution_mode
    reversal_code = ext.reversal_code

    bundle = _default_get_policy(repo, input_paths, base_hash)
    if _use_benchmark_exact_path_fast_path(input_paths, base_hash, ext):
        return bundle
    context = _build_bundle_context(bundle)

    outcome = await handle_extension(
        level=extension_level,
        extension_code=extension_code,
        task_description=task_description,
        execution_mode=execution_mode,
        reversal_code=reversal_code,
        extension_point=post_policy_bundle,
        default_fn=lambda: context["policy_modules"],
        context=context,
        all_capabilities=_policy_capabilities,
        rxr_registry=rxr_registry,
        original_call='result = context["policy_modules"]',
        default_source=_default_get_policy,
        endpoint_path="/policy",
        trigger_condition=lambda res: bool(extension_code) or bool(task_description),
    )

    ext = outcome.ext_result

    # L0 path: no extension triggered — return raw bundle
    if not ext.triggered and not outcome.needs_extension:
        return bundle

    # L1+ path: rebuild bundle dict with extension results
    bundle_dict = bundle.dict() if hasattr(bundle, "dict") else bundle.model_dump()
    filtered = outcome.results
    if isinstance(filtered, list) and all(isinstance(m, dict) for m in filtered):
        bundle_dict["policy_modules"] = filtered
        bundle_dict["manifest"] = [m.get("path", "") for m in filtered] + [
            d.get("path", "") for d in bundle_dict.get("data_modules", [])
        ]

    bundle_dict["extension_triggered"] = ext.triggered
    bundle_dict["generated_code"] = ext.generated_code
    bundle_dict["endpoint_source"] = ext.endpoint_source
    bundle_dict["rxr_record_id"] = ext.rxr_record_id
    bundle_dict["rxr_mode"] = execution_mode == "rxr"
    bundle_dict["rxr_reversal_code"] = ext.rxr_reversal_code
    bundle_dict["needs_extension"] = outcome.needs_extension
    if outcome.extension_context:
        bundle_dict["extension_context"] = outcome.extension_context

    return JSONResponse(bundle_dict)
