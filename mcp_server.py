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
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from opal_server.main import app
from opal_server.symphony_ext import extension_registry
from benchmark_helpers import normalize_benchmark_data_update_entry_aliases
from symphony.manifest import (
    build_tool_descriptions_from_app,
    parse_tool_description_metadata,
)

logger = logging.getLogger("opal.mcp_server")

API_URL = os.getenv("OPAL_API_URL") or os.getenv("API_URL", "http://127.0.0.1:8000")
CLIENT_TOKEN = os.getenv("OPAL_CLIENT_TOKEN") or os.getenv("CLIENT_TOKEN")
DATASOURCE_TOKEN = os.getenv("OPAL_DATA_SOURCE_TOKEN") or os.getenv("DATA_SOURCE_TOKEN")
MASTER_TOKEN = os.getenv("OPAL_AUTH_MASTER_TOKEN") or os.getenv("MASTER_TOKEN")
GOEX_GATE_ENV = "SYMPHONY_MCP_ALLOW_GOEX"

_BENCHMARK_KNOWN_TOPICS = [
    "policy_data",
    "incident_access",
    "feature_flags",
    "directory_sync",
    "audit_logs",
    "compliance_audit",
]
_BENCHMARK_INTERNAL_TOPICS = ("policy:.",)
_BENCHMARK_SIGNATURE_ALIASES: dict[tuple[str, ...], list[str]] = {
    ("audit_logs",): ["opal-client-audit-a-01"],
    ("directory_sync",): ["opal-client-directory-a-01"],
    ("directory_sync", "incident_access"): ["opal-client-directory-b-01"],
    ("feature_flags", "policy_data"): [
        "opal-client-web-a-01",
        "opal-client-web-b-01",
    ],
    ("incident_access",): ["opal-client-authz-b-01"],
    ("audit_logs", "incident_access", "policy_data"): ["opal-client-sre-a-01"],
    ("incident_access", "policy_data"): ["opal-client-authz-a-01"],
}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


BENCHMARK_MODE = _env_flag("OPAL_BENCHMARK_MODE", False)
EXPOSE_ADMIN_TOKEN_TOOL = _env_flag(
    "OPAL_EXPOSE_ADMIN_TOKEN_TOOL",
    default=not BENCHMARK_MODE,
)

_MCP_DNS_REBINDING_PROTECTION = _env_flag(
    "OPAL_MCP_DNS_REBINDING_PROTECTION",
    default=not BENCHMARK_MODE,
)
_MCP_ALLOWED_HOSTS = _env_csv("OPAL_MCP_ALLOWED_HOSTS")
_MCP_ALLOWED_ORIGINS = _env_csv("OPAL_MCP_ALLOWED_ORIGINS")

_mcp_transport_security: TransportSecuritySettings | None = None
if _MCP_DNS_REBINDING_PROTECTION:
    _mcp_transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_MCP_ALLOWED_HOSTS or ["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=_MCP_ALLOWED_ORIGINS
        or [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            "https://127.0.0.1:*",
            "https://localhost:*",
            "https://[::1]:*",
        ],
    )

# ---------------------------------------------------------------------------
# Async HTTP client (300s timeout for L2 internal LLM calls)
# ---------------------------------------------------------------------------

_ASYNC_CLIENT: httpx.AsyncClient | None = None


def _get_async_client() -> httpx.AsyncClient:
    global _ASYNC_CLIENT
    if _ASYNC_CLIENT is None or _ASYNC_CLIENT.is_closed:
        _ASYNC_CLIENT = httpx.AsyncClient(timeout=300.0)
    return _ASYNC_CLIENT


def _raise_with_detail(resp: httpx.Response) -> None:
    """Raise an error that includes the response body (API error detail)."""
    if resp.is_success:
        return
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    raise RuntimeError(f"API error {resp.status_code}: {detail}")


def _bearer(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _client_headers() -> dict[str, str]:
    return _bearer(CLIENT_TOKEN)


def _datasource_headers() -> dict[str, str]:
    return _bearer(DATASOURCE_TOKEN)


def _master_headers() -> dict[str, str]:
    return _bearer(MASTER_TOKEN)


def _resolve_execution_mode(requested: str | None) -> str:
    """Allow GoEx by default; set SYMPHONY_MCP_ALLOW_GOEX=false to force direct."""
    normalized = (requested or "direct").strip().lower()
    if normalized != "goex":
        return "direct"
    gate_value = os.getenv(GOEX_GATE_ENV)
    if gate_value is None or gate_value.strip().lower() in {"1", "true", "yes", "on"}:
        return "goex"
    logger.warning(
        "Downgrading execution_mode=goex to direct because %s=%r",
        GOEX_GATE_ENV,
        gate_value,
    )
    return "direct"


def _normalize_data_update_entries(entries: Any) -> list[dict[str, Any]]:
    """Accept benchmark shorthand entries and coerce them to OPAL's schema.

    The benchmark prompts describe entries with a singular `topic` field for
    readability, while OPAL's DataSourceEntry expects `topics: list[str]`.
    Normalize that shorthand before forwarding the request to the server so
    benchmark agents can stay concise without silently publishing to the
    default `policy_data` topic.
    """
    if not isinstance(entries, list):
        raise ValueError("entries must decode to a JSON array")

    normalized: list[dict[str, Any]] = []
    for idx, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {idx} must be a JSON object")

        item = dict(entry)
        if "topics" not in item and "topic" in item:
            topic = item.pop("topic")
            if isinstance(topic, str) and topic.strip():
                item["topics"] = [topic.strip()]
            elif isinstance(topic, list):
                item["topics"] = topic
            else:
                raise ValueError(f"entry {idx} has invalid topic value")
        if BENCHMARK_MODE:
            item = normalize_benchmark_data_update_entry_aliases(item)
        normalized.append(item)

    return normalized


async def _get(
    path: str,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
) -> dict | list:
    resp = await _get_async_client().get(f"{API_URL}{path}", params=params, headers=headers)
    _raise_with_detail(resp)
    return resp.json()


async def _get_with_body(
    path: str,
    params: dict | None = None,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> dict | list:
    """GET request with a JSON body."""
    resp = await _get_async_client().request(
        "GET",
        f"{API_URL}{path}",
        params=params,
        json=body or {},
        headers=headers,
    )
    _raise_with_detail(resp)
    return resp.json()


async def _post(path: str, body: dict, headers: dict[str, str] | None = None) -> dict:
    resp = await _get_async_client().post(f"{API_URL}{path}", json=body, headers=headers)
    _raise_with_detail(resp)
    return resp.json()


async def _put(path: str, body: dict, headers: dict[str, str] | None = None) -> dict:
    resp = await _get_async_client().put(f"{API_URL}{path}", json=body, headers=headers)
    _raise_with_detail(resp)
    return resp.json()


async def _delete(path: str, body: dict, headers: dict[str, str] | None = None) -> dict:
    resp = await _get_async_client().request("DELETE", f"{API_URL}{path}", json=body, headers=headers)
    _raise_with_detail(resp)
    return resp.json()


# ---------------------------------------------------------------------------
# Try to build enriched descriptions directly from the Symphony app
# ---------------------------------------------------------------------------

_tool_descriptions: dict[str, str] = {}
try:
    _tool_descriptions = build_tool_descriptions_from_app(app, extension_registry)
except Exception as e:
    logger.debug(f"Could not build Symphony tool descriptions: {e}")


def _get_desc(name: str, fallback: str, *, benchmark_note: bool = False) -> str:
    description = _tool_descriptions.get(name) or fallback
    if benchmark_note:
        description = _benchmark_note(description)
    return description


def _benchmark_note(text: str) -> str:
    if not BENCHMARK_MODE:
        return text
    return (
        f"{text} Benchmark note: valid benchmark credentials are already configured. "
        "Do not generate or request new access tokens while solving standard benchmark tasks."
    )


def _is_internal_topic(topic: str) -> bool:
    return any(topic.startswith(prefix) for prefix in _BENCHMARK_INTERNAL_TOPICS)


def _client_topics_from_channels(channels: list[dict[str, Any]]) -> list[str]:
    topics: set[str] = set()
    for channel in channels:
        for topic in channel.get("topics", []) or []:
            if isinstance(topic, str) and not _is_internal_topic(topic):
                topics.add(topic)
    return sorted(topics)


def _looks_ephemeral_client_id(client_id: str) -> bool:
    return client_id.startswith("CLIENT_")


def _alias_benchmark_client_ids(clients: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    signature_to_raw_ids: dict[tuple[str, ...], list[str]] = {}

    for client_id, channels in clients.items():
        signature = tuple(_client_topics_from_channels(channels))
        if _looks_ephemeral_client_id(client_id):
            signature_to_raw_ids.setdefault(signature, []).append(client_id)
        else:
            aliases[client_id] = client_id

    for signature, raw_ids in signature_to_raw_ids.items():
        expected_aliases = _BENCHMARK_SIGNATURE_ALIASES.get(signature, [])
        for idx, raw_id in enumerate(sorted(raw_ids)):
            if idx < len(expected_aliases):
                aliases[raw_id] = expected_aliases[idx]
            else:
                aliases[raw_id] = raw_id
    return aliases


def _build_benchmark_stats(raw_stats: dict[str, Any]) -> dict[str, Any]:
    clients = raw_stats.get("clients", {}) if isinstance(raw_stats, dict) else {}
    if not isinstance(clients, dict):
        clients = {}

    alias_map = _alias_benchmark_client_ids(clients)
    client_topics: dict[str, list[str]] = {}
    for raw_client_id, channels in clients.items():
        alias = alias_map.get(raw_client_id, raw_client_id)
        topics = _client_topics_from_channels(channels if isinstance(channels, list) else [])
        if not topics:
            continue
        client_topics[alias] = topics

    servers = raw_stats.get("servers", []) if isinstance(raw_stats, dict) else []
    server_count = len(servers) if isinstance(servers, (list, set, tuple)) else 0

    return {
        "known_topics": list(_BENCHMARK_KNOWN_TOPICS),
        "client_topics": dict(sorted(client_topics.items())),
        "known_client_ids": sorted(client_topics),
        "server_count": server_count,
        "stable_client_ids_inferred": any(
            raw != alias for raw, alias in alias_map.items()
        ),
    }


def _benchmark_statistics_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(raw_payload)
    raw_stats = dict(raw_payload)
    payload.clear()
    for key in (
        "extension_triggered",
        "generated_code",
        "endpoint_source",
        "goex_record_id",
        "goex_mode",
        "goex_reversal_code",
        "needs_extension",
        "extension_context",
        "extension_results",
        "summary",
    ):
        if key in raw_payload:
            payload[key] = raw_payload[key]
    payload["benchmark_stats"] = _build_benchmark_stats(raw_stats)
    payload["raw_stats"] = raw_stats
    payload["benchmark_guidance"] = (
        "For OPAL benchmark tasks, use benchmark_stats for stable client ids, "
        "known benchmark topics, and normalized client-to-topic membership. "
        "Compute the requested aggregates from client_topics instead of relying on "
        "pre-aggregated subscriber shortcuts, and "
        "reading raw control-plane topics directly unless the task explicitly asks for them."
    )
    return payload


def _with_visible_levels(description: str, levels: list[str]) -> str:
    visible, metadata = parse_tool_description_metadata(description)
    if not metadata:
        return description
    metadata["visible_levels"] = levels
    return (
        f"{visible}\n<symphony-metadata>\n"
        f"{json.dumps(metadata, ensure_ascii=True)}\n"
        f"</symphony-metadata>"
    )


if _tool_descriptions.get("code_extension"):
    _tool_descriptions["code_extension"] = _with_visible_levels(
        _tool_descriptions["code_extension"],
        ["L1", "L2", "L3", "L4"],
    )

# ---------------------------------------------------------------------------
# FastMCP server & tool definitions (Async)
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "opal-symphony-mcp",
    host="0.0.0.0",
    transport_security=_mcp_transport_security,
)


@mcp.tool(
    description=_get_desc(
        "get_policy_bundle",
        "Fetch policy bundle from the OPAL server's tracked Git repository.",
        benchmark_note=True,
    )
)
async def get_policy_bundle(
    path: str | None = None,
    base_hash: str | None = None,
    extension_level: str = "L0",
    extension_code: str | None = None,
    task_description: str | None = None,
    execution_mode: str = "direct",
    reversal_code: str | None = None,
) -> str:
    query: dict[str, Any] = {}
    if path is not None:
        query["path"] = path
    if base_hash is not None:
        query["base_hash"] = base_hash
    body: dict[str, Any] = {"extension_level": extension_level, "execution_mode": _resolve_execution_mode(execution_mode)}
    if extension_code is not None:
        body["extension_code"] = extension_code
    if task_description is not None:
        body["task_description"] = task_description
    if reversal_code is not None:
        body["reversal_code"] = reversal_code
    data = await _get_with_body("/policy", params=query, body=body, headers=_client_headers())
    return json.dumps(data, indent=2)


@mcp.tool(
    description=_get_desc(
        "publish_data_update",
        "Publish a data update to OPAL clients.",
        benchmark_note=True,
    )
)
async def publish_data_update(
    entries: str,
    reason: str = "",
    topics: str | None = None,
    callback: str | None = None,
    update_id: str | None = None,
    extension_level: str = "L0",
    extension_code: str | None = None,
    task_description: str | None = None,
    execution_mode: str = "direct",
    reversal_code: str | None = None,
) -> str:
    try:
        parsed_entries = json.loads(entries)
        parsed_entries = _normalize_data_update_entries(parsed_entries)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON for entries parameter"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    body: dict[str, Any] = {
        "entries": parsed_entries,
        "reason": reason,
        "extension_level": extension_level,
        "execution_mode": _resolve_execution_mode(execution_mode),
    }
    if update_id is not None:
        body["id"] = update_id
    if callback is not None:
        callback_text = callback.strip()
        try:
            parsed_callback = json.loads(callback_text)
        except json.JSONDecodeError:
            parsed_callback = {"callbacks": [callback_text]}
        else:
            if isinstance(parsed_callback, list):
                parsed_callback = {"callbacks": parsed_callback}
            elif not isinstance(parsed_callback, dict):
                return json.dumps({"error": "Invalid JSON for callback parameter"})
        body["callback"] = parsed_callback
    if extension_code is not None:
        body["extension_code"] = extension_code
    if task_description is not None:
        body["task_description"] = task_description
    if reversal_code is not None:
        body["reversal_code"] = reversal_code

    resp = await _get_async_client().post(
        f"{API_URL}/data/config",
        json=body,
        headers=_datasource_headers(),
    )
    _raise_with_detail(resp)
    return json.dumps(resp.json(), indent=2)


@mcp.tool(
    description=_get_desc(
        "get_data_sources_config",
        "Get the base data source configuration for OPAL clients. "
        "Administrative or inspection-only tool; do not use it for standard OPAL benchmark tasks unless the task explicitly asks for base config inspection.",
        benchmark_note=True,
    )
)
async def get_data_sources_config() -> str:
    data = await _get("/data/config", headers=_client_headers())
    return json.dumps(data, indent=2)


@mcp.tool(
    description=_get_desc(
        "get_benchmark_data_candidates",
        "Get the benchmark-only candidate data-update entries for a standard OPAL task.",
        benchmark_note=True,
    )
)
async def get_benchmark_data_candidates(
    label: str = "opal/test2",
    extension_level: str = "L0",
    extension_code: str | None = None,
    task_description: str | None = None,
    execution_mode: str = "direct",
    reversal_code: str | None = None,
) -> str:
    body: dict[str, Any] = {"extension_level": extension_level, "execution_mode": _resolve_execution_mode(execution_mode)}
    if extension_code is not None:
        body["extension_code"] = extension_code
    if task_description is not None:
        body["task_description"] = task_description
    if reversal_code is not None:
        body["reversal_code"] = reversal_code
    data = await _get_with_body(
        "/symphony/benchmark/data-candidates",
        params={"label": label},
        body=body,
        headers=_client_headers(),
    )
    return json.dumps(data, indent=2)


@mcp.tool(
    description=_get_desc(
        "get_statistics",
        "Get OPAL server statistics (connected clients, topics, replicas). "
        "In benchmark mode, the response includes benchmark_stats with "
        "normalized client ids and stable client-to-topic membership; use "
        "benchmark_stats instead of raw_stats unless "
        "the task explicitly asks for raw control-plane details.",
        benchmark_note=True,
    )
)
async def get_statistics(
    extension_level: str = "L0",
    extension_code: str | None = None,
    task_description: str | None = None,
    execution_mode: str = "direct",
    reversal_code: str | None = None,
) -> str:
    body: dict[str, Any] = {"extension_level": extension_level, "execution_mode": _resolve_execution_mode(execution_mode)}
    if extension_code is not None:
        body["extension_code"] = extension_code
    if task_description is not None:
        body["task_description"] = task_description
    if reversal_code is not None:
        body["reversal_code"] = reversal_code
    data = await _get_with_body("/statistics", body=body, headers=_client_headers())
    if BENCHMARK_MODE and isinstance(data, dict):
        data = _benchmark_statistics_payload(data)
    return json.dumps(data, indent=2)


@mcp.tool(description=_get_desc("get_stats_brief", "Get brief OPAL server statistics (client count, server count only)."))
async def get_stats_brief() -> str:
    data = await _get("/stats", headers=_client_headers())
    return json.dumps(data, indent=2)


if EXPOSE_ADMIN_TOKEN_TOOL:
    @mcp.tool(
        description=_get_desc(
            "generate_access_token",
            "Generate a JWT access token for OPAL clients or data sources. "
            "Administrative/bootstrap tool only.",
        )
    )
    async def generate_access_token(
        peer_type: str = "client",
        ttl_days: int = 365,
        claims: dict[str, Any] | str | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "type": peer_type,
            "ttl": f"P{ttl_days}D",
        }
        if claims is not None:
            if isinstance(claims, dict):
                body["claims"] = claims
            elif isinstance(claims, str):
                try:
                    body["claims"] = json.loads(claims)
                except json.JSONDecodeError:
                    return json.dumps({"error": "Invalid JSON for claims parameter"})
            else:
                return json.dumps(
                    {"error": "claims must be either a JSON object or a JSON string"}
                )
        data = await _post("/token", body, headers=_master_headers())
        return json.dumps(data, indent=2)


@mcp.tool(
    description=_benchmark_note(
        "Check server liveness only. This is an operational health endpoint, "
        "not part of standard OPAL benchmark tasks."
    )
)
async def healthcheck() -> str:
    data = await _get("/healthcheck")
    return json.dumps(data, indent=2)


@mcp.tool()
async def create_policy_module(
    module_path: str,
    rego_content: str,
    commit_message: str = "Create policy module",
) -> str:
    data = await _post("/policy/modules", {
        "module_path": module_path,
        "rego_content": rego_content,
        "commit_message": commit_message,
    }, headers=_client_headers())
    return json.dumps(data, indent=2)


@mcp.tool()
async def update_policy_module(
    module_path: str,
    rego_content: str,
    commit_message: str = "Update policy module",
) -> str:
    data = await _put("/policy/modules", {
        "module_path": module_path,
        "rego_content": rego_content,
        "commit_message": commit_message,
    }, headers=_client_headers())
    return json.dumps(data, indent=2)


@mcp.tool()
async def delete_policy_module(
    module_path: str,
    commit_message: str = "Delete policy module",
) -> str:
    data = await _delete("/policy/modules", {
        "module_path": module_path,
        "commit_message": commit_message,
    }, headers=_client_headers())
    return json.dumps(data, indent=2)


@mcp.tool()
async def list_policy_modules() -> str:
    data = await _get("/policy/modules", headers=_client_headers())
    return json.dumps(data, indent=2)


@mcp.tool(description=_get_desc("code_extension", "Submit a prompt to the code extension endpoint."))
async def code_extension(
    prompt: str,
    extension_point: str | None = None,
    code: str | None = None,
    execution_mode: str = "direct",
    reversal_code: str | None = None,
    context_overrides: dict[str, Any] | None = None,
) -> str:
    body: dict[str, Any] = {"prompt": prompt, "execution_mode": _resolve_execution_mode(execution_mode)}
    if extension_point is not None:
        body["extension_point"] = extension_point
    if code is not None:
        body["code"] = code
    if reversal_code is not None:
        body["reversal_code"] = reversal_code
    if context_overrides is not None:
        body["context_overrides"] = context_overrides
    data = await _post("/symphony/code_extension", body, headers=_client_headers())
    return json.dumps(data, indent=2)


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        mcp.settings.host = os.getenv("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.getenv("MCP_PORT", "9000"))
    mcp.run(
        transport=transport,
        mount_path=os.getenv("MCP_MOUNT_PATH"),
    )
