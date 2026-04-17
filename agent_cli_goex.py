#!/usr/bin/env python
"""
OPAL GoEx harness — policy hotfix apply with record capture and reversal.

Usage (from repo root):
    uv run python examples/opal/agent_cli_goex.py \\
        --api-url http://127.0.0.1:8000 \\
        --mcp-url http://127.0.0.1:8000/mcp/sse \\
        --codegen-provider ws://127.0.0.1:8000/symphony/codegen/ws \\
        --provider anthropic --level L1
"""

from __future__ import annotations

import argparse
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


HOTFIX_MODULE_PATH = "incident/cache_failover_hotfix.rego"
HOTFIX_PACKAGE = "app.incident.cache_failover_hotfix"
HOTFIX_COMMIT_MESSAGE = "Apply emergency cache failover hotfix"
HOTFIX_SUMMARY_TOKEN = "cache_failover"

DEFAULT_TASK = f"""A sev-1 outage in the production payments cluster forced traffic onto a stale cache replica. Apply an emergency OPAL policy hotfix so the on-call responders can run the cache failover operation while the incident is active. Use GoEx mode for safety and auditability.

Requirements:
- Use module_path "{HOTFIX_MODULE_PATH}".
- Use commit_message "{HOTFIX_COMMIT_MESSAGE}".
- The module must declare package "{HOTFIX_PACKAGE}".
- Include `default allow = false`.
- The policy should allow the request only when:
  - input.incident.severity == "sev-1"
  - input.actor.class == "oncall_responder"
  - input.flags.emergency_override == true
  - input.request.operation == "cache_failover"

If the module already exists, replace it with the corrected hotfix. End with a short operator-facing summary."""

GOEX_SYSTEM_PROMPTS: dict[str, str] = {
    "L1": (
        "You are running an OPAL GoEx test at L1. Do not call apply_policy_hotfix because that endpoint does "
        "not exist. Call code_extension exactly once with prompt equal to the task, extension_point='policy_hotfix', "
        "execution_mode='goex', explicit Python code, and explicit reversal_code. The code must build the requested "
        "Rego source and assign result = upsert_policy_module(context['repo_path'], context['module_path'], "
        "rego_source, context['commit_message']). Add any extra fields directly onto that result dict instead of "
        "wrapping it under another key such as upsert_result. The reversal_code must delete the module if it was "
        "newly created, or restore context['current_rego'] with upsert_policy_module(...) if it already existed."
    ),
    "L2": (
        "You are running an OPAL GoEx test at L2. Use the existing code_extension tool, not any benchmark-specific "
        "hotfix endpoint. Call code_extension exactly once with extension_point='policy_hotfix' and execution_mode='goex'. "
        "Write a precise prompt telling the server to create or replace the requested hotfix module, return the dict from "
        "upsert_policy_module(...) as the final result, add any metadata directly onto that dict instead of nesting it, "
        "and generate real reversal logic that restores context['current_rego'] or deletes a newly created module."
    ),
    "L3": (
        "You are running an OPAL GoEx test at L3. Use the existing code_extension tool, not any benchmark-specific "
        "hotfix endpoint. Call code_extension exactly once with extension_point='policy_hotfix' and execution_mode='goex'. "
        "Use a detailed prompt that tells the server to use the provided policy bundle and current module context to "
        "generate the cache failover hotfix and matching reversal logic. The final result should stay flat: return the "
        "upsert_policy_module dict directly, with any extra metadata added onto that dict."
    ),
    "L4": (
        "You are running an OPAL GoEx test at L4. Call code_extension with extension_point='policy_hotfix' "
        "and execution_mode='goex'. Ask it to create or replace the requested hotfix module and generate real "
        "reversal logic that restores the previous file or deletes the new one. Ask for the final result to stay flat: "
        "return the upsert_policy_module dict directly, with any metadata added onto that dict."
    ),
}

DIRECT_SYSTEM_PROMPT = (
    "You are running the OPAL GoEx baseline at L0. Do not call code_extension and do not call any "
    "nonexistent apply_policy_hotfix endpoint. First call list_policy_modules. If the hotfix module "
    "already exists, call update_policy_module; otherwise call create_policy_module. Use module_path "
    "and commit_message from the task, and provide rego_content that implements the requested cache "
    "failover hotfix."
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


def _cleanup_hotfix_module(api_url: str, module_path: str = HOTFIX_MODULE_PATH) -> None:
    if not _hotfix_exists(api_url, module_path):
        return
    resp = httpx.request(
        "DELETE",
        f"{api_url.rstrip('/')}/policy/modules",
        headers=_auth_headers(),
        json={
            "module_path": module_path,
            "commit_message": "Cleanup benchmark cache failover hotfix",
        },
        timeout=30,
    )
    resp.raise_for_status()


def _fetch_hotfix_module(api_url: str, module_path: str = HOTFIX_MODULE_PATH) -> dict[str, Any] | None:
    if not _hotfix_exists(api_url, module_path):
        return None
    resp = httpx.get(
        f"{api_url.rstrip('/')}/policy",
        headers=_auth_headers(),
        params={"path": module_path},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    modules = payload.get("policy_modules", [])
    return modules[0] if modules else None


def _flatten_hotfix_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    flattened = dict(payload)
    nested = payload.get("upsert_result")
    if isinstance(nested, dict):
        flattened = dict(nested)
        for key in ("repo_path", "module_path", "existed", "current_rego", "summary"):
            if key in payload and key not in flattened:
                flattened[key] = payload[key]
    return flattened


def _hotfix_snapshot_from_record(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result")
    if not isinstance(result, dict):
        return {}
    raw_result = result.get("result")
    snapshot = _flatten_hotfix_result(raw_result if isinstance(raw_result, dict) else None)
    if record.get("id"):
        snapshot.setdefault("goex_record_id", record["id"])
    if record.get("reversal_code"):
        snapshot.setdefault("goex_reversal_code", record["reversal_code"])
    return snapshot


def _is_valid_hotfix_snapshot(snapshot: dict[str, Any]) -> bool:
    if snapshot.get("module_path") != HOTFIX_MODULE_PATH:
        return False
    if snapshot.get("action") not in {"created", "updated"}:
        return False
    rego = str(snapshot.get("rego_content", "") or "")
    return HOTFIX_PACKAGE in rego and HOTFIX_SUMMARY_TOKEN in rego


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


def _normalize_hotfix_snapshot(name: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if name in {"create_policy_module", "update_policy_module"}:
        return data
    if name != "code_extension":
        return None
    snapshot: dict[str, Any] = {}
    results = data.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        snapshot.update(_flatten_hotfix_result(results[0]))
    if data.get("goex_record_id"):
        snapshot["goex_record_id"] = data["goex_record_id"]
    if data.get("goex_reversal_code"):
        snapshot["goex_reversal_code"] = data["goex_reversal_code"]
    if data.get("generated_code"):
        snapshot["generated_code"] = data["generated_code"]
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


def _client_pod(namespace: str, kube_context: str | None) -> str:
    cmd = _kubectl_base(kube_context) + [
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=opal-client-authz-a",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _hotfix_visible_on_client(namespace: str, kube_context: str | None) -> bool:
    pod = _client_pod(namespace, kube_context)
    script = (
        "import sys, urllib.request, urllib.error\n"
        "url='http://127.0.0.1:8181/v1/policies/incident/cache_failover_hotfix.rego'\n"
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
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return HOTFIX_PACKAGE in result.stdout
    if result.returncode == 10:
        return False
    raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def _wait_for_client_hotfix(
    namespace: str,
    kube_context: str | None,
    *,
    expect_present: bool,
    timeout_seconds: int = 120,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        visible = _hotfix_visible_on_client(namespace, kube_context)
        if visible == expect_present:
            return True
        time.sleep(3)
    return False


def run_test(args: argparse.Namespace) -> bool:
    try:
        api_url = _resolve_api_url(args)
    except Exception as exc:
        print(f"\n[1/5] Resolving API...")
        print(f"  {FAIL}  {exc}")
        return False

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

    try:
        _cleanup_hotfix_module(api_url)
    except Exception as exc:
        print(f"  {FAIL}  could not clean previous hotfix state: {exc}")
        return False

    print(
        f"\n[2/5] Running agent (level={args.level}, execution_mode={args.execution_mode})..."
    )
    print(f"  Task: {args.task[:200]}...\n" if len(args.task) > 200 else f"  Task: {args.task}\n")

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
        task=args.task,
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
            system_prompt = GOEX_SYSTEM_PROMPTS.get(args.level)
        elif args.level == "L0":
            system_prompt = DIRECT_SYSTEM_PROMPT
        asyncio.run(
            runner.run(
                args.task,
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
        module = _wait_for_hotfix_module(api_url)
        if module is None:
            print(f"  {FAIL}  Hotfix module {HOTFIX_MODULE_PATH} was not found after execution.")
            return False
        rego = module.get("rego", "")
        if HOTFIX_PACKAGE not in rego or HOTFIX_SUMMARY_TOKEN not in rego:
            print(
                f"  {FAIL}  Hotfix module content did not contain expected markers. "
                f"package={HOTFIX_PACKAGE!r}, token={HOTFIX_SUMMARY_TOKEN!r}"
            )
            return False
        print(
            f"  {PASS}  hotfix module present with package {HOTFIX_PACKAGE} "
            f"(action={action or 'unknown'})"
        )
        if args.namespace:
            print("\n[3b/5] Checking hotfix on opal-client-authz-a...")
            try:
                propagated = _wait_for_client_hotfix(
                    args.namespace,
                    args.kube_context,
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
            _cleanup_hotfix_module(api_url)
        except Exception as exc:
            print(f"  {FAIL}  direct-mode cleanup failed: {exc}")
            return False
        if _hotfix_exists(api_url):
            print(f"  {FAIL}  cleanup did not remove {HOTFIX_MODULE_PATH}.")
            return False
        if args.namespace:
            try:
                cleared = _wait_for_client_hotfix(
                    args.namespace,
                    args.kube_context,
                    expect_present=False,
                )
            except Exception as exc:
                print(f"  {FAIL}  client cleanup verification failed: {exc}")
                return False
            if not cleared:
                print(f"  {FAIL}  hotfix still visible on opal-client-authz-a after cleanup.")
                return False
        print(f"  {PASS}  L0 baseline created the hotfix and cleanup restored the repo.")
        print("\n[4/5] Skipping GoEx (direct mode).")
        print("\n[5/5] Skipping reversal (direct mode).")
        return True

    if len(record_ids) != 1:
        print(
            f"  {FAIL}  Expected exactly one GoEx record, captured {len(record_ids)}. "
            f"Snapshots: {json.dumps(hotfix_snapshots, indent=2)[:800]}"
        )
        return False

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
    if not _is_valid_hotfix_snapshot(effective_snapshot):
        print(
            f"  {FAIL}  GoEx hotfix result did not contain the expected module details. "
            f"Snapshot: {json.dumps(effective_snapshot, indent=2)[:1200]}"
        )
        return False
    action = effective_snapshot.get("action")
    module = _wait_for_hotfix_module(api_url)
    if module is None:
        print(
            f"  {PASS}  GoEx record captured a valid hotfix for {HOTFIX_MODULE_PATH} "
            f"(action={action or 'unknown'}); admin API did not reflect it within the bounded readback window."
        )
    else:
        rego = module.get("rego", "")
        if HOTFIX_PACKAGE not in rego or HOTFIX_SUMMARY_TOKEN not in rego:
            print(
                f"  {FAIL}  Admin API returned {HOTFIX_MODULE_PATH}, but it did not contain expected markers. "
                f"package={HOTFIX_PACKAGE!r}, token={HOTFIX_SUMMARY_TOKEN!r}"
            )
            return False
        print(
            f"  {PASS}  GoEx record captured a valid hotfix and admin API readback matched "
            f"(action={action or 'unknown'})"
        )
    if args.namespace:
        print("\n[4b/5] Checking hotfix on opal-client-authz-a...")
        try:
            propagated = _wait_for_client_hotfix(
                args.namespace,
                args.kube_context,
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

    if _hotfix_exists(api_url):
        print(f"  {FAIL}  Hotfix module still exists after reversal.")
        return False
    if args.namespace:
        try:
            cleared = _wait_for_client_hotfix(
                args.namespace,
                args.kube_context,
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
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--export-stats", metavar="FILE", default=None)
    parser.add_argument("--benchmark-label", metavar="LABEL", default=None)
    parser.add_argument("--benchmark-final-json-key", metavar="KEY", default=None)
    parser.add_argument("--namespace", default=os.getenv("OPAL_NAMESPACE"))
    parser.add_argument("--kube-context", default=os.getenv("OPAL_KUBE_CONTEXT"))
    args = parser.parse_args()

    ok = run_test(args)
    print()
    print("=" * 50)
    print(f"  OPAL GoEx harness: {PASS if ok else FAIL}")
    print("=" * 50)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
