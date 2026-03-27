"""
OPAL MCP Server (FastMCP / Hand-Written)
=========================================

Exposes the Symphony-enhanced OPAL server API as MCP tools.
At startup, the tool descriptions are enriched with per-level extension
documentation built directly from the local Symphony app and registry.

Usage:
    uv run python mcp_server.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from opal_server.main import app
from opal_server.symphony_ext import extension_registry
from symphony.manifest import build_tool_descriptions_from_app

logger = logging.getLogger("opal.mcp_server")

API_URL = os.getenv("API_URL", "http://127.0.0.1:8000")

# ---------------------------------------------------------------------------
# HTTP client (300s timeout for L2 internal LLM calls)
# ---------------------------------------------------------------------------

_CLIENT: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.Client(timeout=300.0)
    return _CLIENT


def _raise_with_detail(resp: httpx.Response) -> None:
    """Raise an error that includes the response body (API error detail)."""
    if resp.is_success:
        return
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    raise RuntimeError(f"API error {resp.status_code}: {detail}")


def _get(path: str, params: dict | None = None) -> dict | list:
    resp = _get_client().get(f"{API_URL}{path}", params=params)
    _raise_with_detail(resp)
    return resp.json()


def _get_with_body(
    path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict | list:
    """GET request with a JSON body.

    Used for endpoints that accept large fields (e.g. extension_code) in the
    request body to avoid URL query-string length limits.
    """
    resp = _get_client().request(
        "GET",
        f"{API_URL}{path}",
        params=params,
        json=body or {},
    )
    _raise_with_detail(resp)
    return resp.json()


def _post(path: str, body: dict) -> dict:
    resp = _get_client().post(f"{API_URL}{path}", json=body)
    _raise_with_detail(resp)
    return resp.json()


def _put(path: str, body: dict) -> dict:
    resp = _get_client().put(f"{API_URL}{path}", json=body)
    _raise_with_detail(resp)
    return resp.json()


def _delete(path: str, body: dict) -> dict:
    resp = _get_client().request("DELETE", f"{API_URL}{path}", json=body)
    _raise_with_detail(resp)
    return resp.json()


# ---------------------------------------------------------------------------
# Try to build enriched descriptions directly from the Symphony app
# ---------------------------------------------------------------------------

_tool_descriptions: dict[str, str] = {}
try:
    _tool_descriptions = build_tool_descriptions_from_app(app, extension_registry)
except Exception:
    logger.debug("Could not build Symphony tool descriptions; using base descriptions")

# ---------------------------------------------------------------------------
# FastMCP server & tool definitions
# ---------------------------------------------------------------------------

mcp = FastMCP("opal-symphony-mcp")


@mcp.tool(description=_tool_descriptions.get("get_policy_bundle", ""))
def get_policy_bundle(
    path: str | None = None,
    base_hash: str | None = None,
    extension_level: str = "L0",
    extension_code: str | None = None,
    task_description: str | None = None,
    execution_mode: str = "direct",
    reversal_code: str | None = None,
) -> str:
    """Fetch policy bundle from the OPAL server's tracked Git repository.

    Args:
        path: Filter to specific file paths within the policy repo.
        base_hash: Base commit hash for differential bundle.
        extension_level: Extension level: L0 (vanilla), L1 (static), L2 (dynamic), L3 (source-aware).
        extension_code: Python extension code for L1/L2/L3.
        task_description: Natural-language task description for L2/L3.
        execution_mode: Execution mode: direct or goex.
        reversal_code: Undo code for GoEx mode.
    """
    query: dict[str, Any] = {}
    if path is not None:
        query["path"] = path
    if base_hash is not None:
        query["base_hash"] = base_hash
    body: dict[str, Any] = {"extension_level": extension_level, "execution_mode": execution_mode}
    if extension_code is not None:
        body["extension_code"] = extension_code
    if task_description is not None:
        body["task_description"] = task_description
    if reversal_code is not None:
        body["reversal_code"] = reversal_code
    data = _get_with_body("/policy", params=query, body=body)
    return json.dumps(data, indent=2)


@mcp.tool(description=_tool_descriptions.get("publish_data_update", ""))
def publish_data_update(
    entries: str,
    reason: str = "",
    topics: str | None = None,
    extension_level: str = "L0",
    extension_code: str | None = None,
    task_description: str | None = None,
    execution_mode: str = "direct",
    reversal_code: str | None = None,
) -> str:
    """Publish a data update to OPAL clients.

    Args:
        entries: JSON array of data source entries. Each entry has: url, dst_path, topics, save_method.
        reason: Human-readable reason for the update.
        topics: Comma-separated target topics (optional, uses entry topics if omitted).
        extension_level: Extension level: L0 (vanilla), L1 (static), L2 (dynamic), L3 (source-aware).
        extension_code: Python extension code for L1/L2.
        task_description: Natural-language task description for L2/L3.
        execution_mode: Execution mode: direct or goex.
        reversal_code: Undo code for GoEx mode.
    """
    try:
        parsed_entries = json.loads(entries)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON for entries parameter"})

    body: dict[str, Any] = {
        "entries": parsed_entries,
        "reason": reason,
        "extension_level": extension_level,
        "execution_mode": execution_mode,
    }
    if extension_code is not None:
        body["extension_code"] = extension_code
    if task_description is not None:
        body["task_description"] = task_description
    if reversal_code is not None:
        body["reversal_code"] = reversal_code

    resp = _get_client().post(f"{API_URL}/data/config", json=body)
    _raise_with_detail(resp)
    return json.dumps(resp.json(), indent=2)


@mcp.tool(description=_tool_descriptions.get("get_data_sources_config", ""))
def get_data_sources_config() -> str:
    """Get the base data source configuration for OPAL clients."""
    data = _get("/data/config")
    return json.dumps(data, indent=2)


@mcp.tool(description=_tool_descriptions.get("get_statistics", ""))
def get_statistics(
    extension_level: str = "L0",
    extension_code: str | None = None,
    task_description: str | None = None,
    execution_mode: str = "direct",
    reversal_code: str | None = None,
) -> str:
    """Get OPAL server statistics (connected clients, topics, replicas).

    Args:
        extension_level: Extension level: L0 (vanilla), L1 (static), L2 (dynamic), L3 (source-aware).
        extension_code: Python extension code for L1/L2.
        task_description: Natural-language task description for L2/L3.
        execution_mode: Execution mode: direct or goex.
        reversal_code: Undo code for GoEx mode.
    """
    body: dict[str, Any] = {"extension_level": extension_level, "execution_mode": execution_mode}
    if extension_code is not None:
        body["extension_code"] = extension_code
    if task_description is not None:
        body["task_description"] = task_description
    if reversal_code is not None:
        body["reversal_code"] = reversal_code
    data = _get_with_body("/statistics", body=body)
    return json.dumps(data, indent=2)


@mcp.tool(description=_tool_descriptions.get("get_stats_brief", ""))
def get_stats_brief() -> str:
    """Get brief OPAL server statistics (client count, server count only)."""
    data = _get("/stats")
    return json.dumps(data, indent=2)


@mcp.tool(description=_tool_descriptions.get("generate_access_token", ""))
def generate_access_token(
    peer_type: str = "client",
    ttl_days: int = 365,
    claims: str | None = None,
) -> str:
    """Generate a JWT access token for OPAL clients or data sources.

    Args:
        peer_type: Type of peer: 'client', 'datasource', or 'listener'.
        ttl_days: Token time-to-live in days (default: 365).
        claims: JSON object with custom JWT claims (optional).
    """
    body: dict[str, Any] = {
        "type": peer_type,
        "ttl": f"P{ttl_days}D",  # ISO 8601 duration
    }
    if claims is not None:
        try:
            body["claims"] = json.loads(claims)
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON for claims parameter"})
    data = _post("/token", body)
    return json.dumps(data, indent=2)


@mcp.tool()
def healthcheck() -> str:
    """Check if the OPAL server is healthy."""
    data = _get("/healthcheck")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Policy CRUD tools
# ---------------------------------------------------------------------------


@mcp.tool()
def create_policy_module(
    module_path: str,
    rego_content: str,
    commit_message: str = "Create policy module",
) -> str:
    """Create a new Rego policy module in the tracked Git repository.

    Writes the file and commits it locally. The new module is immediately
    visible in subsequent get_policy_bundle calls.

    Args:
        module_path: Repo-relative path (e.g. 'compliance/emergency_block.rego').
        rego_content: Raw Rego source code for the module.
        commit_message: Git commit message.
    """
    data = _post("/policy/modules", {
        "module_path": module_path,
        "rego_content": rego_content,
        "commit_message": commit_message,
    })
    return json.dumps(data, indent=2)


@mcp.tool()
def update_policy_module(
    module_path: str,
    rego_content: str,
    commit_message: str = "Update policy module",
) -> str:
    """Update an existing Rego policy module in the tracked Git repository.

    Overwrites the file and commits the change locally.

    Args:
        module_path: Repo-relative path of the module to update.
        rego_content: New Rego source code.
        commit_message: Git commit message.
    """
    data = _put("/policy/modules", {
        "module_path": module_path,
        "rego_content": rego_content,
        "commit_message": commit_message,
    })
    return json.dumps(data, indent=2)


@mcp.tool()
def delete_policy_module(
    module_path: str,
    commit_message: str = "Delete policy module",
) -> str:
    """Delete a Rego policy module from the tracked Git repository.

    Removes the file and commits the deletion. The module will no longer
    appear in bundle fetches and will show in deleted_files for diff bundles.

    Args:
        module_path: Repo-relative path of the module to delete.
        commit_message: Git commit message.
    """
    data = _delete("/policy/modules", {
        "module_path": module_path,
        "commit_message": commit_message,
    })
    return json.dumps(data, indent=2)


@mcp.tool()
def list_policy_modules() -> str:
    """List all Rego policy modules tracked in the Git repository."""
    data = _get("/policy/modules")
    return json.dumps(data, indent=2)


@mcp.tool(description=_tool_descriptions.get("code_extension", ""))
def code_extension(
    prompt: str,
    extension_point: str | None = None,
    code: str | None = None,
) -> str:
    """Submit a prompt (and optional pre-generated code) for L4 freeform code generation.

    Args:
        prompt: Natural-language task description for the LLM.
        extension_point: Optional Symphony extension point to scope capabilities/context
            (e.g. "post_policy_bundle", "post_data_update", "post_statistics").
        code: Pre-generated extension code (from CLI). If provided, executes directly.
    """
    body: dict[str, Any] = {"prompt": prompt}
    if extension_point is not None:
        body["extension_point"] = extension_point
    if code is not None:
        body["code"] = code
    data = _post("/symphony/code_extension", body)
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
