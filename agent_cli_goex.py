#!/usr/bin/env python
"""
OPAL GoEx harness — stateful OPAL mutation benchmarks with record capture and reversal.

Usage (from repo root):
    uv run python examples/opal/agent_cli_goex.py \\
        --api-url http://127.0.0.1:8000 \\
        --mcp-url http://127.0.0.1:8000/mcp/sse \\
        --codegen-provider ws://127.0.0.1:8000/symphony/codegen/ws \\
        --provider anthropic --level L1
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from symphony import SymphonyRunner
from symphony.providers import create_provider
from symphony.stats_export import make_export_callbacks
from opal_server.benchmark_scenarios import get_goex_scenario


class _CodegenWebsocketClient:
    def __init__(self, *, url: str, provider_name: str, model: str, reasoning: bool) -> None:
        self.url = url
        self._provider_name = provider_name
        self._model = model
        self._reasoning = reasoning
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def start(self) -> None:
        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._shutdown = asyncio.Event()
            try:
                loop.run_until_complete(self._serve())
            except Exception as exc:
                self._error = exc
                self._ready.set()
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True, name="opal-goex-codegen")
        self._thread.start()
        self._ready.wait(timeout=5.0)
        if self._error:
            raise RuntimeError("Codegen client failed to start") from self._error

    def stop(self) -> None:
        if self._loop and self._shutdown:
            self._loop.call_soon_threadsafe(self._shutdown.set)
        if self._thread:
            self._thread.join(timeout=5.0)

    async def _serve(self) -> None:
        import websockets

        async with websockets.connect(self.url, open_timeout=60) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "provider": self._provider_name,
                        "model": self._model,
                        "reasoning": self._reasoning,
                    }
                )
            )
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if not ack.get("ok"):
                raise RuntimeError(ack.get("error", "registration rejected"))
            self._ready.set()
            assert self._shutdown is not None

            while not self._shutdown.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                req = json.loads(raw)
                if req.get("type") != "generate_code":
                    continue
                resp = await self._generate(req.get("prompt", ""))
                resp["type"] = "generate_code_result"
                resp["request_id"] = req.get("request_id")
                await ws.send(json.dumps(resp))

    async def _generate(self, prompt: str) -> dict[str, Any]:
        provider = create_provider(
            self._provider_name,
            model=self._model,
            reasoning=self._reasoning,
            timeout=600,
        )
        parts: list[str] = []
        try:
            resp = await provider.generate_code(prompt, on_content=parts.append)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                await close()
        return {"ok": True, "content": "".join(parts) or resp.content}


DEFAULT_SCENARIO = get_goex_scenario("current")
HOTFIX_MODULE_PATH = DEFAULT_SCENARIO.module_path

GOEX_SYSTEM_PROMPTS: dict[str, str] = {
    "L1": (
        "You are running an OPAL GoEx round-trip test at L1. "
        "For each required mutation, use GoEx-enabled extension execution at this level with valid forward execution "
        "logic and valid reversal logic. Do not rely on direct baseline execution. In OPAL, the reversible mutation "
        "should use the predefined `apply_policy_hotfix` extension hook rather than a read-only inspection step, a bundle "
        "post-processing extension, or raw policy CRUD fallback. Extension code for this hotfix should rely on "
        "the policy-hotfix capabilities exposed for that extension point; ordinary MCP/request tools are not "
        "available inside that extension code. Use the extension capability metadata to choose the appropriate "
        "helper calls. "
        "The forward path should apply exactly the requested mutation, leave it applied, and return structured "
        "identifiers and hotfix metadata for the created object, including the final Rego source as `rego_content`. "
        "The reversal path should read the execution result, undo exactly what was created, and report its outcome."
    ),
    "L2": (
        "You are running an OPAL GoEx round-trip test at L2. "
        "For each required mutation, use GoEx-enabled execution at this level and request extension behavior "
        "with reversal support. Do not rely on direct baseline execution. In OPAL, the reversible mutation should "
        "use the predefined `apply_policy_hotfix` extension hook rather than a read-only inspection step, a bundle post-processing "
        "extension, or raw policy CRUD fallback. Extension code for this hotfix should rely on the policy-hotfix "
        "capabilities exposed for that extension point; ordinary MCP/request tools are not available inside that "
        "extension code. Use the extension capability metadata to choose the appropriate helper calls. Apply exactly the requested mutation "
        "once per target object, return structured identifiers for created objects, and ensure the reversal path "
        "can undo them from the execution result. Include the final Rego source as `rego_content` in that result."
    ),
    "L3": (
        "You are running an OPAL GoEx round-trip test at L3. "
        "For each required mutation, use GoEx-enabled extension execution at this level with source-aware reversible "
        "behavior. In OPAL, the reversible mutation should use the source-aware `apply_policy_hotfix` extension hook rather than a "
        "read-only inspection step, a bundle post-processing extension, or raw policy CRUD fallback. Extension code "
        "for this hotfix should rely on the policy-hotfix capabilities exposed for that extension point; ordinary "
        "MCP/request tools are not available inside that extension code. Use the extension capability metadata to "
        "choose the appropriate helper calls. Apply exactly "
        "one mutation per requested target object. Do not perform exploratory, duplicate, or debugging mutations. "
        "Return structured identifiers for what was created so reversal can undo exactly those changes from the "
        "execution result, including the final Rego source as `rego_content`."
    ),
    "L4": (
        "You are running an OPAL GoEx round-trip test at L4. "
        "Use GoEx-enabled reversible extension execution at this level. The forward path should perform only the "
        "requested mutations, leave them applied, and assign the final structured payload to the expected "
        "result variable. In OPAL, the reversible mutation should use the `code_extension` hotfix path rather "
        "than a read-only inspection step, a bundle post-processing extension, or raw policy CRUD fallback. Extension code "
        "for this hotfix should rely on the policy-hotfix capabilities exposed for that extension point; ordinary "
        "MCP/request tools are not available inside that extension code. Use the extension capability metadata to "
        "choose the appropriate helper calls. Do "
        "not perform exploratory or debugging mutations. The reversal path must undo every created object using "
        "identifiers from the execution result. Ensure execution results expose stable identifiers for all created "
        "objects, including the final Rego source as `rego_content`."
    ),
}

DIRECT_SYSTEM_PROMPT = (
    "You are running the OPAL GoEx baseline at L0. Do not call code_extension and do not call any "
    "apply_policy_hotfix endpoint. Use list_policy_modules if needed to determine whether "
    "the hotfix module already exists. If it already exists, call update_policy_module; otherwise call "
    "create_policy_module. Use module_path and commit_message from the task, and provide rego_content "
    "that implements the requested outage policy change. Follow the task's grading contract exactly and "
    "end with exactly one fenced JSON block containing the requested top-level result object."
)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
CLIENT_TOKEN = os.getenv("OPAL_CLIENT_TOKEN")


def _origin_url(url: str, *, scheme: str | None = None) -> str:
    parts = urlsplit(url)
    resolved_scheme = scheme or parts.scheme
    return urlunsplit((resolved_scheme, parts.netloc, "", "", ""))


def _auth_headers() -> dict[str, str]:
    if not CLIENT_TOKEN:
        return {}
    return {"Authorization": f"Bearer {CLIENT_TOKEN}"}


def _candidate_api_urls(args: argparse.Namespace) -> list[str]:
    candidates: list[str] = []
    if args.api_url:
        candidates.append(args.api_url)
    for env_name in ("OPAL_API_URL", "API_URL"):
        value = os.getenv(env_name)
        if value:
            candidates.append(value)
    if args.codegen_provider:
        scheme = "https" if args.codegen_provider.startswith("wss://") else "http"
        candidates.append(_origin_url(args.codegen_provider, scheme=scheme))
    if args.mcp_url:
        candidates.append(_origin_url(args.mcp_url))
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        normalized = candidate.rstrip("/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def _resolve_api_url(args: argparse.Namespace) -> str:
    candidates = _candidate_api_urls(args)
    if not candidates:
        return "http://127.0.0.1:8000"
    if args.api_url:
        return candidates[0]
    errors: list[str] = []
    for candidate in candidates:
        try:
            resp = httpx.get(f"{candidate}/healthcheck", timeout=5)
            resp.raise_for_status()
            return candidate
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError(
        "Could not resolve OPAL API URL. Tried: " + "; ".join(errors)
    )


def _reverse_record(api_url: str, record_id: str) -> dict[str, Any]:
    resp = httpx.post(
        f"{api_url.rstrip('/')}/symphony/goex/records/{record_id}/reverse",
        headers=_auth_headers(),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _get_record(api_url: str, record_id: str) -> dict[str, Any]:
    resp = httpx.get(
        f"{api_url.rstrip('/')}/symphony/goex/records/{record_id}",
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _reset_benchmark_state(api_url: str) -> dict[str, Any]:
    resp = httpx.post(
        f"{api_url.rstrip('/')}/symphony/benchmark/reset",
        headers=_auth_headers(),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _list_policy_modules(api_url: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{api_url.rstrip('/')}/policy/modules",
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("modules", [])


def _hotfix_exists(api_url: str, module_path: str = HOTFIX_MODULE_PATH) -> bool:
    return any(item.get("path") == module_path for item in _list_policy_modules(api_url))

def _fetch_hotfix_module(api_url: str, module_path: str = HOTFIX_MODULE_PATH) -> dict[str, Any] | None:
    if not _hotfix_exists(api_url, module_path):
        return None
    resp = httpx.get(
        f"{api_url.rstrip('/')}/policy",
        headers=_auth_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    modules = payload.get("policy_modules", [])
    for module in modules:
        if module.get("path") == module_path:
            return module
    return None


def _fetch_stack_info(api_url: str) -> dict[str, Any] | None:
    try:
        resp = httpx.get(
            f"{api_url.rstrip('/')}/symphony/benchmark/info",
            headers=_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _flatten_hotfix_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    flattened = dict(payload)
    nested = None
    for key in ("upsert_result", "update_result"):
        value = payload.get(key)
        if isinstance(value, dict):
            nested = value
            break
    if nested is None:
        nested = payload.get("upsert_policy_module")
        if isinstance(nested, dict):
            nested = {
                "action": nested.get("action") or "updated",
                "module_path": nested.get("module_path"),
                "rego_content": nested.get("new_rego_content") or nested.get("rego_content"),
                "previous_rego": nested.get("old_rego_content"),
            }
    if isinstance(nested, dict):
        flattened = dict(nested)
        for key in (
            "repo_path",
            "module_path",
            "existed",
            "current_rego",
            "summary",
            "module_exists_before",
            "previous_rego",
        ):
            if key in payload and key not in flattened:
                flattened[key] = payload[key]
    
    if "rego_content" not in flattened:
        for key in ("new_rego", "new_rego_content", "rego"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                flattened["rego_content"] = value
                break
    if "previous_rego" not in flattened:
        for key in ("old_rego", "original_rego", "old_rego_content", "current_rego"):
            value = payload.get(key)
            if value is not None:
                flattened["previous_rego"] = value
                break
    if "module_exists_before" not in flattened:
        if "existed" in payload:
            flattened["module_exists_before"] = bool(payload.get("existed"))
        elif "current_rego" in payload:
            flattened["module_exists_before"] = payload.get("current_rego") is not None

    action = flattened.get("action")
    if action in ("create", "update"):
        flattened["action"] = action + "d"
    elif action in {"upsert", "upserted", "upsert_policy_module"}:
        flattened["action"] = (
            "updated" if flattened.get("module_exists_before") else "created"
        )
    return flattened


def _hotfix_snapshot_from_record(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result")
    if not isinstance(result, dict):
        return {}
    raw_result = result.get("result")
    if isinstance(raw_result, dict):
        snapshot = _flatten_hotfix_result(raw_result)
    else:
        results = result.get("results")
        first = results[0] if isinstance(results, list) and results else None
        snapshot = _flatten_hotfix_result(first if isinstance(first, dict) else None)
    if record.get("id"):
        snapshot.setdefault("goex_record_id", record["id"])
    if record.get("reversal_code"):
        snapshot.setdefault("goex_reversal_code", record["reversal_code"])
    return snapshot


def _hotfix_snapshot_from_code(code: str | None) -> dict[str, Any]:
    """Best-effort static recovery for generated policy-hotfix code.

    Some older L4/GoEx runs printed a JSON payload instead of assigning it to
    `result`, so the tool result may be empty even though the generated code
    contains enough stable metadata for diagnostics.
    """
    if not isinstance(code, str) or "upsert_policy_module" not in code:
        return {}
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {}

    string_values: dict[str, str] = {}
    bool_values: dict[str, bool] = {}
    snapshot: dict[str, Any] = {"action": "updated"}

    def literal_value(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in string_values:
                return string_values[node.id]
            if node.id in bool_values:
                return bool_values[node.id]
        return None

    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = literal_value(node.value)
        if isinstance(target, ast.Name):
            if isinstance(value, str):
                string_values[target.id] = value
            elif isinstance(value, bool):
                bool_values[target.id] = value
        elif (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"hotfix_result", "result"}
        ):
            key = literal_value(target.slice)
            if isinstance(key, str) and value is not None:
                snapshot[key] = value

    for key in ("repo_path", "module_path", "rego_content", "previous_rego", "package_name"):
        if key not in snapshot and key in string_values:
            snapshot[key] = string_values[key]
    if "module_exists_before" not in snapshot and "module_exists_before" in bool_values:
        snapshot["module_exists_before"] = bool_values["module_exists_before"]
        snapshot["action"] = "updated" if bool_values["module_exists_before"] else "created"

    return _flatten_hotfix_result(snapshot)


def _code_contains_policy_mutation(code: str | None) -> bool:
    if not isinstance(code, str):
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    mutating_names = {
        "upsert_policy_module",
        "delete_policy_module",
        "create_policy_module",
        "update_policy_module",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in mutating_names:
            return True
        if isinstance(func, ast.Attribute) and func.attr in mutating_names:
            return True
    return False


def _is_valid_hotfix_snapshot(
    snapshot: dict[str, Any],
    *,
    module_path: str,
    package_name: str,
    summary_token: str,
) -> bool:
    if snapshot.get("module_path") != module_path:
        return False
    if snapshot.get("action") not in {"created", "updated", "create", "update"}:
        return False
    rego = str(snapshot.get("rego_content", "") or "")
    return package_name in rego and summary_token in rego


def _module_matches_hotfix(
    module: dict[str, Any] | None,
    *,
    package_name: str,
    summary_token: str,
) -> bool:
    if not isinstance(module, dict):
        return False
    rego = str(module.get("rego", "") or "")
    return package_name in rego and summary_token in rego


def _wait_for_hotfix_module(
    api_url: str,
    module_path: str = HOTFIX_MODULE_PATH,
    *,
    timeout_seconds: int = 20,
) -> dict[str, Any] | None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        module = _fetch_hotfix_module(api_url, module_path)
        if module is not None:
            return module
        time.sleep(1)
    return None


def _wait_for_server_hotfix_state(
    api_url: str,
    *,
    module_path: str,
    package_name: str,
    summary_token: str,
    expect_present: bool,
    timeout_seconds: int = 120,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        module = _fetch_hotfix_module(api_url, module_path)
        visible = _module_matches_hotfix(
            module,
            package_name=package_name,
            summary_token=summary_token,
        )
        if visible == expect_present:
            return True
        time.sleep(3)
    return False


def _normalize_hotfix_snapshot(name: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if name in {"create_policy_module", "update_policy_module"}:
        return data
    if name not in {"code_extension", "apply_policy_hotfix"}:
        return None
    snapshot: dict[str, Any] = {}
    results = data.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        snapshot.update(_flatten_hotfix_result(results[0]))
    code_snapshot = _hotfix_snapshot_from_code(data.get("generated_code"))
    for key, value in code_snapshot.items():
        snapshot.setdefault(key, value)
    if data.get("goex_record_id"):
        snapshot["goex_record_id"] = data["goex_record_id"]
    if data.get("goex_reversal_code"):
        snapshot["goex_reversal_code"] = data["goex_reversal_code"]
    if data.get("generated_code"):
        snapshot["generated_code"] = data["generated_code"]
    has_hotfix_details = any(snapshot.get(key) for key in ("action", "module_path", "rego_content"))
    if not has_hotfix_details and not _code_contains_policy_mutation(data.get("generated_code")):
        return None
    return snapshot or None


def _build_capturing_callbacks(
    record_ids: list[str],
    hotfix_snapshots: list[dict[str, Any]],
    *,
    export_stats: str | None,
    stats_app: str,
    level: str,
    provider: str,
    model: str,
    reasoning: bool,
    task: str,
    mcp_url: str,
    codegen_provider: str | None,
    benchmark_label: str | None,
    benchmark_final_json_key: str | None,
) -> dict[str, Callable]:
    codegen_side = {
        "L0": "n/a",
        "L1": "client",
        "L2": "server",
        "L3": "server",
        "L4": "server",
    }.get(level, "server")
    if export_stats:
        base = make_export_callbacks(
            app=stats_app,
            level=level,
            provider=provider,
            model=model,
            reasoning=reasoning,
            codegen_side=codegen_side,
            task=task,
            export_path=Path(export_stats),
            benchmark_final_json_key=benchmark_final_json_key,
            benchmark_label=benchmark_label,
            extra_fields={
                "mcp_url": mcp_url,
                "goex_harness": "opal",
                "codegen_provider": codegen_provider or "",
            },
        )
    else:
        base = SymphonyRunner.terminal_callbacks()
    base_on_tool_result = base["on_tool_result"]

    def on_tool_result(name: str, result: str) -> None:
        base_on_tool_result(name, result)
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return
        snapshot = _normalize_hotfix_snapshot(name, data)
        if snapshot is None:
            return
        hotfix_snapshots.append(snapshot)
        rid = snapshot.get("goex_record_id") or data.get("goex_record_id")
        if rid:
            record_ids.append(str(rid))

    base["on_tool_result"] = on_tool_result
    return base


def _kubectl_base(kube_context: str | None) -> list[str]:
    cmd = ["kubectl"]
    if kube_context:
        cmd.extend(["--context", kube_context])
    return cmd


def _client_pod(namespace: str, kube_context: str | None, app_name: str = "opal-client-authz-a") -> str:
    cmd = _kubectl_base(kube_context) + [
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        f"app.kubernetes.io/name={app_name}",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _hotfix_visible_on_client(
    namespace: str,
    kube_context: str | None,
    *,
    module_path: str,
    package_name: str,
) -> bool:
    pod = _client_pod(namespace, kube_context, "opal-client-authz-a")
    script = (
        "import sys, urllib.request, urllib.error\n"
        "url='http://127.0.0.1:8181/v1/policies/' + sys.argv[1]\n"
        "try:\n"
        "    data = urllib.request.urlopen(url, timeout=10).read().decode('utf-8')\n"
        "    sys.stdout.write(data)\n"
        "    sys.exit(0)\n"
        "except urllib.error.HTTPError as exc:\n"
        "    sys.stdout.write(str(exc.code))\n"
        "    sys.exit(10 if exc.code == 404 else 11)\n"
    )
    cmd = _kubectl_base(kube_context) + [
        "-n",
        namespace,
        "exec",
        pod,
        "--",
        "python",
        "-c",
        script,
        module_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return package_name in result.stdout
    if result.returncode == 10:
        return False
    raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def _wait_for_client_hotfix(
    namespace: str,
    kube_context: str | None,
    *,
    module_path: str,
    package_name: str,
    expect_present: bool,
    timeout_seconds: int = 120,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        visible = _hotfix_visible_on_client(
            namespace,
            kube_context,
            module_path=module_path,
            package_name=package_name,
        )
        if visible == expect_present:
            return True
        time.sleep(3)
    return False


def _client_decision(
    namespace: str,
    kube_context: str | None,
    *,
    client_app: str,
    package_path: str,
    input_payload: dict[str, Any],
) -> tuple[bool, bool]:
    pod = _client_pod(namespace, kube_context, client_app)
    script = (
        "import json, sys, urllib.error, urllib.request\n"
        "req = urllib.request.Request(\n"
        "    'http://127.0.0.1:8181/v1/data/' + sys.argv[1],\n"
        "    data=json.dumps({'input': json.loads(sys.argv[2])}).encode('utf-8'),\n"
        "    headers={'Content-Type': 'application/json'},\n"
        ")\n"
        "try:\n"
        "    print(urllib.request.urlopen(req, timeout=10).read().decode('utf-8'))\n"
        "except urllib.error.HTTPError as exc:\n"
        "    print(json.dumps({'http_error': exc.code, 'body': exc.read().decode('utf-8')}))\n"
    )
    cmd = _kubectl_base(kube_context) + [
        "-n",
        namespace,
        "exec",
        pod,
        "--",
        "python",
        "-c",
        script,
        package_path,
        json.dumps(input_payload),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    data = json.loads(result.stdout.strip())
    if "http_error" in data:
        return False, False
    value = data.get("result")
    return bool(value is not None), bool(value)


def _wait_for_client_decision(
    namespace: str,
    kube_context: str | None,
    *,
    client_app: str,
    package_path: str,
    input_payload: dict[str, Any],
    expect_value: bool,
    timeout_seconds: int = 120,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        _, value = _client_decision(
            namespace,
            kube_context,
            client_app=client_app,
            package_path=package_path,
            input_payload=input_payload,
        )
        if value == expect_value:
            return True
        time.sleep(3)
    return False


def _client_policy_status(
    namespace: str,
    kube_context: str | None,
    *,
    app_name: str,
    module_path: str,
) -> tuple[int, str]:
    pod = _client_pod(namespace, kube_context, app_name)
    script = (
        "import json, sys, urllib.error, urllib.request\n"
        "url='http://127.0.0.1:8181/v1/policies/' + sys.argv[1]\n"
        "try:\n"
        "    data = urllib.request.urlopen(url, timeout=10).read().decode('utf-8')\n"
        "    print(json.dumps({'status_code': 200, 'body': data}))\n"
        "except urllib.error.HTTPError as exc:\n"
        "    print(json.dumps({'status_code': exc.code, 'body': exc.read().decode('utf-8')}))\n"
    )
    cmd = _kubectl_base(kube_context) + [
        "-n",
        namespace,
        "exec",
        pod,
        "--",
        "python",
        "-c",
        script,
        module_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    payload = json.loads(result.stdout.strip())
    return int(payload.get("status_code", 500)), str(payload.get("body", ""))


def _client_health(
    namespace: str,
    kube_context: str | None,
    *,
    app_name: str,
) -> tuple[int, str]:
    pod = _client_pod(namespace, kube_context, app_name)
    script = (
        "import json, urllib.error, urllib.request\n"
        "url='http://127.0.0.1:7000/healthy'\n"
        "try:\n"
        "    data = urllib.request.urlopen(url, timeout=10).read().decode('utf-8')\n"
        "    print(json.dumps({'status_code': 200, 'body': data}))\n"
        "except urllib.error.HTTPError as exc:\n"
        "    print(json.dumps({'status_code': exc.code, 'body': exc.read().decode('utf-8')}))\n"
    )
    cmd = _kubectl_base(kube_context) + [
        "-n",
        namespace,
        "exec",
        pod,
        "--",
        "python",
        "-c",
        script,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    payload = json.loads(result.stdout.strip())
    return int(payload.get("status_code", 500)), str(payload.get("body", ""))


def _trigger_client_policy_refresh(
    namespace: str,
    kube_context: str | None,
    *,
    app_name: str,
) -> None:
    pod = _client_pod(namespace, kube_context, app_name)
    script = (
        "import json, urllib.error, urllib.request\n"
        "req = urllib.request.Request('http://127.0.0.1:7000/policy-updater/trigger', data=b'', method='POST')\n"
        "try:\n"
        "    data = urllib.request.urlopen(req, timeout=20).read().decode('utf-8')\n"
        "    print(json.dumps({'status_code': 200, 'body': data}))\n"
        "except urllib.error.HTTPError as exc:\n"
        "    print(json.dumps({'status_code': exc.code, 'body': exc.read().decode('utf-8')}))\n"
    )
    cmd = _kubectl_base(kube_context) + [
        "-n",
        namespace,
        "exec",
        pod,
        "--",
        "python",
        "-c",
        script,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    payload = json.loads(result.stdout.strip())
    status_code = int(payload.get("status_code", 500))
    if status_code != 200:
        raise RuntimeError(
            f"policy refresh failed for {app_name}: status={status_code}, body={payload.get('body', '')}"
        )


def _refresh_scenario_client(
    namespace: str | None,
    kube_context: str | None,
    scenario: Any,
) -> None:
    if not namespace:
        return
    app_name = scenario.decision_check.client_app if scenario.decision_check is not None else "opal-client-authz-a"
    _trigger_client_policy_refresh(namespace, kube_context, app_name=app_name)


def _decision_diagnostics(
    api_url: str,
    namespace: str,
    kube_context: str | None,
    *,
    scenario: Any,
) -> str:
    parts: list[str] = []
    if scenario.decision_check is None:
        return ""
    try:
        pos_has_result, pos_value = _client_decision(
            namespace,
            kube_context,
            client_app=scenario.decision_check.client_app,
            package_path=scenario.decision_check.package_path,
            input_payload=scenario.decision_check.positive_input,
        )
        parts.append(f"positive(has_result={pos_has_result}, value={pos_value})")
    except Exception as exc:
        parts.append(f"positive_error={exc!s}")
    try:
        neg_has_result, neg_value = _client_decision(
            namespace,
            kube_context,
            client_app=scenario.decision_check.client_app,
            package_path=scenario.decision_check.package_path,
            input_payload=scenario.decision_check.negative_input,
        )
        parts.append(f"negative(has_result={neg_has_result}, value={neg_value})")
    except Exception as exc:
        parts.append(f"negative_error={exc!s}")
    try:
        status_code, body = _client_policy_status(
            namespace,
            kube_context,
            app_name=scenario.decision_check.client_app,
            module_path=scenario.module_path,
        )
        parts.append(f"client_policy_status={status_code}")
        parts.append(f"client_policy_body={body[:300]}")
    except Exception as exc:
        parts.append(f"client_policy_error={exc!s}")
    try:
        status_code, body = _client_health(
            namespace,
            kube_context,
            app_name=scenario.decision_check.client_app,
        )
        parts.append(f"client_healthy_status={status_code}")
        parts.append(f"client_healthy_body={body[:300]}")
    except Exception as exc:
        parts.append(f"client_healthy_error={exc!s}")
    try:
        module = _fetch_hotfix_module(api_url, scenario.module_path)
        parts.append(
            "server_module="
            + json.dumps(
                {
                    "path": module.get("path") if module else None,
                    "package_name": module.get("package_name") if module else None,
                    "rego_preview": (module.get("rego", "")[:200] if module else None),
                },
                sort_keys=True,
            )
        )
    except Exception as exc:
        parts.append(f"server_module_error={exc!s}")
    return ", ".join(parts)


def run_test(args: argparse.Namespace) -> bool:
    try:
        api_url = _resolve_api_url(args)
    except Exception as exc:
        print(f"\n[1/5] Resolving API...")
        print(f"  {FAIL}  {exc}")
        return False

    scenario = get_goex_scenario(args.case)
    task = args.task or scenario.task
    default_models = {
        "anthropic": "haiku",
        "gemini": "gemini-2.5-flash",
        "llmgateway": "gpt-4o-mini",
        "ollama": "qwen3.5:9b",
        "copilot": "claude-haiku-4.5",
        "agent": "auto",
    }
    model = args.model or default_models.get(args.provider, "haiku")

    print("\n[1/5] OPAL API reachable...")
    print(f"  API URL: {api_url}")
    try:
        httpx.get(f"{api_url}/healthcheck", timeout=5).raise_for_status()
    except Exception as exc:
        print(f"  {FAIL}  healthcheck failed: {exc}")
        return False
    print(f"  {PASS}  healthcheck ok")
    stack_info = _fetch_stack_info(api_url)
    if stack_info:
        print(
            "  stack_info: "
            + json.dumps(
                {
                    "policy_repo_url": stack_info.get("policy_repo_url"),
                    "policy_repo_branch": stack_info.get("policy_repo_branch"),
                    "policy_repo_manifest_path": stack_info.get("policy_repo_manifest_path"),
                    "codegen_provider": stack_info.get("codegen_provider"),
                    "build_id": stack_info.get("build_id"),
                },
                sort_keys=True,
            )
        )

    if not args.skip_reset:
        try:
            reset = _reset_benchmark_state(api_url)
        except Exception as exc:
            print(f"  {FAIL}  could not reset benchmark state: {exc}")
            return False
        print(f"  {PASS}  benchmark reset ok: {json.dumps(reset, ensure_ascii=True)}")
        if args.namespace:
            try:
                _refresh_scenario_client(args.namespace, args.kube_context, scenario)
            except Exception as exc:
                print(f"  {FAIL}  benchmark reset client refresh failed: {exc}")
                return False
    else:
        print("  [INFO] benchmark reset skipped by caller")

    print(
        f"\n[2/5] Running agent (case={args.case}, level={args.level}, execution_mode={args.execution_mode})..."
    )
    print(f"  Task: {task[:200]}...\n" if len(task) > 200 else f"  Task: {task}\n")

    record_ids: list[str] = []
    hotfix_snapshots: list[dict[str, Any]] = []
    callbacks = _build_capturing_callbacks(
        record_ids,
        hotfix_snapshots,
        export_stats=args.export_stats,
        stats_app="opal-goex",
        level=args.level,
        provider=args.provider,
        model=model,
        reasoning=not args.no_reasoning,
        task=task,
        mcp_url=args.mcp_url,
        codegen_provider=args.codegen_provider,
        benchmark_label=args.benchmark_label,
        benchmark_final_json_key=args.benchmark_final_json_key,
    )

    codegen_client: _CodegenWebsocketClient | None = None
    if args.codegen_provider:
        codegen_client = _CodegenWebsocketClient(
            url=args.codegen_provider,
            provider_name=args.provider,
            model=model,
            reasoning=not args.no_reasoning,
        )
        codegen_client.start()
        print(f"  [Codegen Worker] Connected to {args.codegen_provider}")

    runner = SymphonyRunner(
        model=model,
        reasoning=not args.no_reasoning,
        provider=args.provider,
        mcp_url=args.mcp_url,
        mcp_transport=args.transport,
    )

    try:
        system_prompt = None
        if args.execution_mode == "goex":
            base_prompt = GOEX_SYSTEM_PROMPTS.get(args.level)
            if base_prompt is not None:
                system_prompt = base_prompt
        elif args.level == "L0":
            system_prompt = DIRECT_SYSTEM_PROMPT
        asyncio.run(
            runner.run(
                task,
                level=args.level,
                execution_mode=args.execution_mode,
                system_prompt=system_prompt,
                **callbacks,
            )
        )
    finally:
        if codegen_client:
            codegen_client.stop()

    print("\n[3/5] Checking policy hotfix outcome...")
    if not hotfix_snapshots:
        print(f"  {FAIL}  No policy hotfix tool results captured.")
        return False

    last = hotfix_snapshots[-1]
    action = last.get("action")

    if args.execution_mode != "goex":
        module = _wait_for_hotfix_module(api_url, scenario.module_path)
        if module is None:
            print(f"  {FAIL}  Hotfix module {scenario.module_path} was not found after execution.")
            return False
        rego = module.get("rego", "")
        if scenario.package_name not in rego or scenario.summary_token not in rego:
            print(
                f"  {FAIL}  Hotfix module content did not contain expected markers. "
                f"package={scenario.package_name!r}, token={scenario.summary_token!r}"
            )
            return False
        print(
            f"  {PASS}  hotfix module present with package {scenario.package_name} "
            f"(action={action or 'unknown'})"
        )
        if args.namespace:
            try:
                _refresh_scenario_client(args.namespace, args.kube_context, scenario)
            except Exception as exc:
                print(f"  {FAIL}  client refresh failed: {exc}")
                return False
            if scenario.decision_check is not None:
                print(f"\n[3b/5] Checking decision on {scenario.decision_check.client_app}...")
                try:
                    propagated = _wait_for_client_decision(
                        args.namespace,
                        args.kube_context,
                        client_app=scenario.decision_check.client_app,
                        package_path=scenario.decision_check.package_path,
                        input_payload=scenario.decision_check.positive_input,
                        expect_value=True,
                    )
                    _, negative = _client_decision(
                        args.namespace,
                        args.kube_context,
                        client_app=scenario.decision_check.client_app,
                        package_path=scenario.decision_check.package_path,
                        input_payload=scenario.decision_check.negative_input,
                    )
                except Exception as exc:
                    print(f"  {FAIL}  client verification failed: {exc}")
                    return False
                if not propagated or negative:
                    print(f"  {FAIL}  client decision did not converge to the expected state.")
                    return False
                print(f"  {PASS}  outage decision propagated to {scenario.decision_check.client_app}")
            elif scenario.expect_module_presence:
                print("\n[3b/5] Checking hotfix on opal-client-authz-a...")
                try:
                    propagated = _wait_for_client_hotfix(
                        args.namespace,
                        args.kube_context,
                        module_path=scenario.module_path,
                        package_name=scenario.package_name,
                        expect_present=True,
                    )
                except Exception as exc:
                    print(f"  {FAIL}  client verification failed: {exc}")
                    return False
                if not propagated:
                    print(f"  {FAIL}  hotfix never reached opal-client-authz-a.")
                    return False
                print(f"  {PASS}  hotfix propagated to opal-client-authz-a")

        try:
            _reset_benchmark_state(api_url)
        except Exception as exc:
            print(f"  {FAIL}  direct-mode cleanup failed: {exc}")
            return False
        if args.namespace:
            try:
                _refresh_scenario_client(args.namespace, args.kube_context, scenario)
            except Exception as exc:
                print(f"  {FAIL}  client cleanup refresh failed: {exc}")
                return False
            if scenario.decision_check is not None:
                try:
                    cleared = _wait_for_client_decision(
                        args.namespace,
                        args.kube_context,
                        client_app=scenario.decision_check.client_app,
                        package_path=scenario.decision_check.package_path,
                        input_payload=scenario.decision_check.positive_input,
                        expect_value=False,
                    )
                except Exception as exc:
                    print(f"  {FAIL}  client cleanup verification failed: {exc}")
                    return False
                if not cleared:
                    print(f"  {FAIL}  outage decision did not return to the baseline state.")
                    return False
            elif scenario.expect_module_presence:
                try:
                    cleared = _wait_for_client_hotfix(
                        args.namespace,
                        args.kube_context,
                        module_path=scenario.module_path,
                        package_name=scenario.package_name,
                        expect_present=False,
                    )
                except Exception as exc:
                    print(f"  {FAIL}  client cleanup verification failed: {exc}")
                    return False
                if not cleared:
                    print(f"  {FAIL}  hotfix still visible on opal-client-authz-a after cleanup.")
                    return False
        print(f"  {PASS}  L0 baseline created the change and reset restored the repo.")
        print("\n[4/5] Skipping GoEx (direct mode).")
        print("\n[5/5] Skipping reversal (direct mode).")
        return True

    # Deduplicate and pick the last record (the model may retry on errors)
    unique_ids = list(dict.fromkeys(record_ids))
    if len(unique_ids) > 1:
        print(f"  [INFO] Multiple GoEx records captured ({len(unique_ids)}); using the last one.")
        record_ids[:] = [unique_ids[-1]]
    elif len(unique_ids) == 0:
        print(f"  {FAIL}  No GoEx record captured.")
        return False
    else:
        record_ids[:] = unique_ids

    print(f"\n[4/5] Checking GoEx records ({len(record_ids)} captured)...")
    fetched_records: list[dict[str, Any]] = []
    for rid in record_ids:
        try:
            record = _get_record(api_url, rid)
        except Exception as exc:
            print(f"  {FAIL}  Could not fetch record {rid}: {exc}")
            return False
        fetched_records.append(record)
        status = record.get("status", "?")
        has_rev = bool(record.get("reversal_code"))
        print(
            f"  Record {rid[:8]}…  status={status}  reversal_code="
            f"{PASS if has_rev else FAIL}"
        )
        if not has_rev:
            return False
        if status == "auto_reversed":
            print(
                f"  {FAIL}  Record {rid[:8]}… was auto-reversed by server-side validation before client propagation."
            )
            return False
    record_snapshot = _hotfix_snapshot_from_record(fetched_records[-1])
    effective_snapshot = record_snapshot or last
    if not _is_valid_hotfix_snapshot(
        effective_snapshot,
        module_path=scenario.module_path,
        package_name=scenario.package_name,
        summary_token=scenario.summary_token,
    ):
        print(
            f"  {FAIL}  GoEx hotfix result did not contain the expected module details. "
            f"Snapshot: {json.dumps(effective_snapshot, indent=2)[:1200]}"
        )
        return False
    action = effective_snapshot.get("action")
    if not _wait_for_server_hotfix_state(
        api_url,
        module_path=scenario.module_path,
        package_name=scenario.package_name,
        summary_token=scenario.summary_token,
        expect_present=True,
        timeout_seconds=30,
    ):
        print(
            f"  {FAIL}  GoEx record captured a plausible hotfix snapshot, but the server bundle did not "
            f"reflect the expected policy markers within the bounded readback window."
        )
        return False
    print(
        f"  {PASS}  GoEx record captured a valid hotfix and admin API readback matched "
        f"(action={action or 'unknown'})"
    )
    if args.namespace:
        try:
            _refresh_scenario_client(args.namespace, args.kube_context, scenario)
        except Exception as exc:
            print(f"  {FAIL}  client refresh failed: {exc}")
            return False
        if scenario.decision_check is not None:
            print(f"\n[4b/5] Checking decision on {scenario.decision_check.client_app}...")
            try:
                propagated = _wait_for_client_decision(
                    args.namespace,
                    args.kube_context,
                    client_app=scenario.decision_check.client_app,
                    package_path=scenario.decision_check.package_path,
                    input_payload=scenario.decision_check.positive_input,
                    expect_value=True,
                )
                _, negative = _client_decision(
                    args.namespace,
                    args.kube_context,
                    client_app=scenario.decision_check.client_app,
                    package_path=scenario.decision_check.package_path,
                    input_payload=scenario.decision_check.negative_input,
                )
            except Exception as exc:
                print(f"  {FAIL}  client verification failed: {exc}")
                return False
            if not propagated or negative:
                print(f"  {FAIL}  outage decision did not converge to the expected state.")
                print(f"  diagnostics: {_decision_diagnostics(api_url, args.namespace, args.kube_context, scenario=scenario)}")
                return False
            print(f"  {PASS}  outage decision propagated to {scenario.decision_check.client_app}")
        elif scenario.expect_module_presence:
            print("\n[4b/5] Checking hotfix on opal-client-authz-a...")
            try:
                propagated = _wait_for_client_hotfix(
                    args.namespace,
                    args.kube_context,
                    module_path=scenario.module_path,
                    package_name=scenario.package_name,
                    expect_present=True,
                )
            except Exception as exc:
                print(f"  {FAIL}  client verification failed: {exc}")
                return False
            if not propagated:
                print(f"  {FAIL}  hotfix never reached opal-client-authz-a.")
                return False
            print(f"  {PASS}  hotfix propagated to opal-client-authz-a")

    print(f"\n[5/5] Reversing {len(record_ids)} record(s)...")
    for rid in record_ids:
        try:
            result = _reverse_record(api_url, rid)
            new_status = result.get("status", "?")
            mark = PASS if new_status == "reversed" else FAIL
            print(f"  {mark}  {rid[:8]}…  -> status={new_status}")
            if new_status != "reversed":
                return False
        except Exception as exc:
            print(f"  {FAIL}  reverse failed for {rid[:8]}…: {exc}")
            return False

    if not _wait_for_server_hotfix_state(
        api_url,
        module_path=scenario.module_path,
        package_name=scenario.package_name,
        summary_token=scenario.summary_token,
        expect_present=False,
        timeout_seconds=120,
    ):
        print(f"  {FAIL}  server bundle still reflects the hotfix after reversal.")
        return False
    if scenario.decision_check is None and _hotfix_exists(api_url, scenario.module_path):
        print(f"  {FAIL}  Hotfix module still exists after reversal.")
        return False
    if args.namespace:
        try:
            _refresh_scenario_client(args.namespace, args.kube_context, scenario)
        except Exception as exc:
            print(f"  {FAIL}  client reversal refresh failed: {exc}")
            return False
        if scenario.decision_check is not None:
            try:
                cleared = _wait_for_client_decision(
                    args.namespace,
                    args.kube_context,
                    client_app=scenario.decision_check.client_app,
                    package_path=scenario.decision_check.package_path,
                    input_payload=scenario.decision_check.positive_input,
                    expect_value=False,
                )
            except Exception as exc:
                print(f"  {FAIL}  client reversal verification failed: {exc}")
                return False
            if not cleared:
                print(f"  {FAIL}  outage decision did not return to the baseline state after reversal.")
                print(f"  diagnostics: {_decision_diagnostics(api_url, args.namespace, args.kube_context, scenario=scenario)}")
                return False
        elif scenario.expect_module_presence:
            try:
                cleared = _wait_for_client_hotfix(
                    args.namespace,
                    args.kube_context,
                    module_path=scenario.module_path,
                    package_name=scenario.package_name,
                    expect_present=False,
                )
            except Exception as exc:
                print(f"  {FAIL}  client reversal verification failed: {exc}")
                return False
            if not cleared:
                print(f"  {FAIL}  hotfix still visible on opal-client-authz-a after reversal.")
                return False
    print(f"  {PASS}  All reversals succeeded.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OPAL GoEx harness: policy hotfix apply + record reversal.",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="OPAL API base URL (default: resolve from OPAL_API_URL / --mcp-url / --codegen-provider).",
    )
    parser.add_argument(
        "--mcp-url",
        required=True,
        metavar="URL",
        help="MCP SSE endpoint (e.g. http://127.0.0.1:8000/mcp/sse)",
    )
    parser.add_argument(
        "--transport",
        choices=["sse", "streamable-http"],
        default="sse",
    )
    parser.add_argument(
        "--level",
        choices=["L0", "L1", "L2", "L3", "L4"],
        default="L1",
    )
    parser.add_argument(
        "--case",
        choices=["current", "test1", "test3"],
        default="current",
    )
    parser.add_argument(
        "--provider",
        choices=["ollama", "anthropic", "gemini", "llmgateway", "copilot", "agent"],
        default="anthropic",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-reasoning", action="store_true")
    parser.add_argument(
        "--codegen-provider",
        metavar="WS_URL",
        default=None,
        help="Server websocket codegen broker URL",
    )
    parser.add_argument(
        "--execution-mode",
        choices=["direct", "goex"],
        default="direct",
        help="L0: direct (no GoEx). L1-L4 with goex: use --execution-mode goex",
    )
    parser.add_argument("--task", default=None)
    parser.add_argument("--export-stats", metavar="FILE", default=None)
    parser.add_argument("--benchmark-label", metavar="LABEL", default=None)
    parser.add_argument("--benchmark-final-json-key", metavar="KEY", default=None)
    parser.add_argument("--namespace", default=os.getenv("OPAL_NAMESPACE"))
    parser.add_argument("--kube-context", default=os.getenv("OPAL_KUBE_CONTEXT"))
    parser.add_argument("--skip-reset", action="store_true")
    args = parser.parse_args()

    ok = run_test(args)
    print()
    print("=" * 50)
    print(f"  OPAL GoEx harness: {PASS if ok else FAIL}")
    print("=" * 50)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
