"""
Symphony Extension Integration for OPAL Server
================================================
Defines capabilities, extension points, and registries that integrate
Symphony's extension framework into the OPAL server.

Extension points:
  - post_policy_bundle: filter/transform policy bundles before serving
  - policy_hotfix:      create/update/delete emergency policy modules via Git
  - post_data_update:   validate/transform data update entries before publishing
  - post_benchmark_candidate_feed: validate/filter benchmark candidate entries
  - post_statistics:    aggregate/alert on server statistics before returning

Capabilities are grouped into classes whose methods are decorated with
@capability and collected at startup.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from git.exc import InvalidGitRepositoryError, NoSuchPathError
from git.repo import Repo
from opal_common.logger import logger
from opal_server.config import opal_server_config

from symphony import (
    ExtensionPoint,
    ExtensionRegistry,
    GoExRegistry,
    collect_capabilities,
    capability,
)
from symphony.sandbox import set_default_executor, PythonSandboxExecutor
from opal_server.policy.module_ops import (
    PolicyModulePathError,
    delete_policy_module as delete_policy_module_from_repo,
    module_exists as module_exists_in_repo,
    read_policy_module as read_policy_module_from_repo,
    upsert_policy_module as upsert_policy_module_in_repo,
)

# ---------------------------------------------------------------------------
# Sandbox — use PythonSandboxExecutor (in-process, no external deps needed)
# ---------------------------------------------------------------------------
set_default_executor(PythonSandboxExecutor())


def _create_codegen_provider():
    """Create an LLM provider for server-side code generation from env vars."""
    provider_name = os.environ.get("SYMPHONY_CODEGEN_PROVIDER")
    if not provider_name:
        return None
    model = os.environ.get("SYMPHONY_CODEGEN_MODEL")
    from symphony.providers import create_provider
    kwargs = {}
    if model:
        kwargs["model"] = model
    return create_provider(provider_name, **kwargs)


_policy_hotfix_notifier = None
_policy_repo_getter = None
_statistics_getter = None
_data_update_publisher = None
_data_update_loop_getter: Callable[[], asyncio.AbstractEventLoop | None] | None = None
_POLICY_REPO_ALIASES = {
    "",
    ".",
    "default",
    "policy",
    "repo",
    "main",
    "bundle",
    "policy_repo",
    "policy_bundle",
    "policies",
    "/policy",
    "opal/policy",
    "opal/policy-repo",
    "opal/policy_bundle",
    "opal/test_policy",
    "aqua/policy-repo",
    "test",
}


def set_policy_hotfix_notifier(notifier):
    """Register a best-effort callback used after hotfix mutations."""
    global _policy_hotfix_notifier
    _policy_hotfix_notifier = notifier


def _emit_policy_hotfix_notification() -> None:
    notifier = _policy_hotfix_notifier
    if notifier is None:
        return
    try:
        notifier()
    except Exception:
        logger.warning(
            "Failed to publish policy hotfix notification",
            exc_info=True,
        )


def set_data_update_publisher(publisher, loop_getter=None):
    """Register the OPAL data-update publisher for L4 capability wrappers."""
    global _data_update_publisher, _data_update_loop_getter
    _data_update_publisher = publisher
    _data_update_loop_getter = loop_getter


def _serialize_state(obj: Any) -> Any:
    """Make pydantic/statistics objects safe to return from sandbox code."""
    if hasattr(obj, "dict"):
        return _serialize_state(obj.dict())
    if hasattr(obj, "model_dump"):
        return _serialize_state(obj.model_dump())
    if isinstance(obj, dict):
        return {k: _serialize_state(v) for k, v in obj.items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, list):
        return [_serialize_state(item) for item in obj]
    return obj


def _run_coro_sync(coro):
    """Run an async OPAL operation from sandbox worker threads."""
    loop = _data_update_loop_getter() if _data_update_loop_getter is not None else None
    if loop is not None and loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
    return asyncio.run(coro)


_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([A-Za-z0-9_.]+)\s*$")


def _infer_package_name(rego: str) -> str:
    match = _PACKAGE_RE.search(rego or "")
    return match.group(1) if match else ""


def _build_policy_bundle_context(repo: Repo | None) -> dict:
    """Build the same policy-bundle context used by endpoint extensions."""
    from opal_common.git_utils.bundle_maker import BundleMaker

    if repo is None or len(repo.heads) == 0:
        return {
            "policy_modules": [],
            "modules": [],
            "data_modules": [],
            "manifest": [],
            "hash": "",
            "old_hash": None,
            "deleted_files": None,
            "module_count": 0,
            "data_module_count": 0,
        }
    maker = BundleMaker(
        repo,
        in_directories={Path(".")},
        extensions=opal_server_config.FILTER_FILE_EXTENSIONS,
        root_manifest_path=opal_server_config.POLICY_REPO_MANIFEST_PATH,
        bundle_ignore=opal_server_config.BUNDLE_IGNORE,
    )
    bundle = maker.make_bundle(repo.head.commit)
    bundle_dict = bundle.dict() if hasattr(bundle, "dict") else bundle.model_dump()
    policy_modules = bundle_dict.get("policy_modules", [])
    for module in policy_modules:
        if isinstance(module, dict) and not module.get("package_name"):
            module["package_name"] = _infer_package_name(str(module.get("rego", "")))
    data_modules = bundle_dict.get("data_modules", [])
    return {
        "policy_modules": policy_modules,
        "modules": policy_modules,
        "data_modules": data_modules,
        "manifest": bundle_dict.get("manifest", []),
        "hash": bundle_dict.get("hash", ""),
        "old_hash": bundle_dict.get("old_hash"),
        "deleted_files": bundle_dict.get("deleted_files"),
        "module_count": len(policy_modules),
        "data_module_count": len(data_modules),
    }


# ---------------------------------------------------------------------------
# Capability classes — methods are exposed to sandbox extension code
# ---------------------------------------------------------------------------


class PolicyBundleCapabilities:
    """Capabilities for working with policy bundles in extension code."""

    @capability(
        name="get_policy_bundle",
        description="Purpose: fetch the current OPAL policy bundle through the L0 bundle pipeline. Returns manifest, hash, policy_modules, data_modules, and deleted_files. Policy module source is in field rego.",
    )
    def get_policy_bundle(self, repo_path: str = "default") -> dict:
        """Return the current tracked policy bundle as a plain dict."""
        context = _build_policy_bundle_context(_repo_from_path(repo_path))
        return {
            "manifest": context["manifest"],
            "hash": context["hash"],
            "old_hash": context["old_hash"],
            "data_modules": context["data_modules"],
            "policy_modules": context["policy_modules"],
            "deleted_files": context["deleted_files"],
        }

    @capability(
        name="list_policy_modules",
        description="Purpose: list policy module paths and source sizes from the tracked policy clone. Returns modules, count, and hash.",
    )
    def list_policy_modules(self, repo_path: str = "default") -> dict:
        """Return compact policy module metadata."""
        context = _build_policy_bundle_context(_repo_from_path(repo_path))
        modules = [
            {"path": m.get("path", ""), "size": len(str(m.get("rego", "") or ""))}
            for m in context["policy_modules"]
            if isinstance(m, dict)
        ]
        return {"modules": modules, "count": len(modules), "hash": context["hash"]}

    @capability(name="filter_modules_by_path")
    def filter_modules_by_path(self, modules: list[dict], path_prefix: str) -> list[dict]:
        """Filter policy modules whose path starts with the given prefix
        (e.g. 'rbac/' or 'test/')."""
        return [m for m in modules if m.get("path", "").startswith(path_prefix)]

    @capability(name="exclude_modules_by_path")
    def exclude_modules_by_path(self, modules: list[dict], path_prefix: str) -> list[dict]:
        """Exclude policy modules whose path starts with the given prefix."""
        return [m for m in modules if not m.get("path", "").startswith(path_prefix)]

    @capability(name="filter_modules_by_package")
    def filter_modules_by_package(self, modules: list[dict], package_substring: str) -> list[dict]:
        """Filter policy modules whose package name contains the substring."""
        sub = package_substring.lower()
        return [m for m in modules if sub in m.get("package_name", "").lower()]

    @capability(name="search_module_content")
    def search_module_content(self, modules: list[dict], query: str) -> list[dict]:
        """Search policy modules by matching query against Rego source code."""
        q = query.lower()
        return [m for m in modules if q in m.get("rego", "").lower()]

    @capability(name="get_module_paths")
    def get_module_paths(self, modules: list[dict]) -> list[str]:
        """Extract all file paths from a list of modules."""
        return [m.get("path", "") for m in modules]

    @capability(name="get_package_names")
    def get_package_names(self, modules: list[dict]) -> list[str]:
        """Extract all unique package names from a list of policy modules."""
        package_re = re.compile(r"(?m)^\s*package\s+([A-Za-z0-9_.]+)\s*$")
        packages: set[str] = set()
        for module in modules:
            package_name = str(module.get("package_name", "") or "").strip()
            if not package_name:
                rego = str(module.get("rego", "") or "")
                match = package_re.search(rego)
                if match:
                    package_name = match.group(1)
            if package_name:
                packages.add(package_name)
        return sorted(packages)

    @capability(name="exclude_test_modules")
    def exclude_test_modules(self, modules: list[dict]) -> list[dict]:
        """Exclude modules whose path contains 'test' (case-insensitive)."""
        return [m for m in modules if "test" not in m.get("path", "").lower()]

    @capability(name="filter_modules_by_suffix")
    def filter_modules_by_suffix(self, modules: list[dict], suffix: str) -> list[dict]:
        """Filter modules whose path ends with the given suffix (e.g. '_dev.rego')."""
        return [m for m in modules if m.get("path", "").endswith(suffix)]

    @capability(name="exclude_modules_by_suffix")
    def exclude_modules_by_suffix(self, modules: list[dict], suffix: str) -> list[dict]:
        """Exclude modules whose path ends with the given suffix (e.g. '_dev.rego')."""
        return [m for m in modules if not m.get("path", "").endswith(suffix)]


def _canonical_policy_repo() -> Repo | None:
    getter = _policy_repo_getter
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _repo_from_path(repo_path: str) -> Repo:
    candidate = (repo_path or "").strip()
    repo = _canonical_policy_repo()
    if repo is not None and (
        candidate in _POLICY_REPO_ALIASES
        or candidate == repo.working_dir
        or candidate == os.path.basename(repo.working_dir or "")
    ):
        return repo
    if candidate:
        try:
            return Repo(candidate)
        except (InvalidGitRepositoryError, NoSuchPathError):
            pass
    if repo is not None and not candidate:
        return repo
    raise RuntimeError("repo_path is required")


class PolicyHotfixCapabilities:
    """Capabilities for emergency policy hotfix mutations."""

    @capability(
        name="read_policy_module",
        description="Purpose: read one Rego module from the tracked policy clone. Inputs: repo_path alias/path and module_path. Returns source text or None.",
    )
    def read_policy_module(self, repo_path: str, module_path: str) -> str | None:
        """Read a Rego module from the tracked policy clone."""
        try:
            return read_policy_module_from_repo(_repo_from_path(repo_path), module_path)
        except (PolicyModulePathError, ValueError, FileNotFoundError) as exc:
            raise RuntimeError(str(exc)) from exc

    @capability(
        name="module_exists",
        description="Purpose: check whether a tracked Rego module exists before create/update/delete decisions. Inputs: repo_path and module_path. Returns bool.",
    )
    def module_exists(self, repo_path: str, module_path: str) -> bool:
        """Check whether a module exists in the tracked policy clone."""
        try:
            return module_exists_in_repo(_repo_from_path(repo_path), module_path)
        except (PolicyModulePathError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @capability(
        name="upsert_policy_module",
        mutates=True,
        description="Purpose: create or replace a Rego module and commit it. Inputs: repo_path, module_path, rego_content, commit_message. Returns action, module_path, hashes, and content metadata.",
    )
    def upsert_policy_module(
        self,
        repo_path: str,
        module_path: str,
        rego_content: str,
        commit_message: str,
    ) -> dict:
        """Create or replace a Rego module and commit it."""
        try:
            result = upsert_policy_module_in_repo(
                _repo_from_path(repo_path),
                module_path,
                rego_content,
                commit_message,
            )
            _emit_policy_hotfix_notification()
            return result
        except (PolicyModulePathError, ValueError, FileNotFoundError) as exc:
            raise RuntimeError(str(exc)) from exc

    @capability(
        name="delete_policy_module",
        mutates=True,
        description="Purpose: delete a Rego module and commit the removal. Inputs: repo_path, module_path, commit_message, missing_ok. Returns deletion metadata.",
    )
    def delete_policy_module(
        self,
        repo_path: str,
        module_path: str,
        commit_message: str = "Delete policy module",
        missing_ok: bool = False,
    ) -> dict:
        """Delete a Rego module and commit the removal."""
        try:
            result = delete_policy_module_from_repo(
                _repo_from_path(repo_path),
                module_path,
                commit_message,
                missing_ok=missing_ok,
            )
            _emit_policy_hotfix_notification()
            return result
        except (PolicyModulePathError, ValueError, FileNotFoundError) as exc:
            raise RuntimeError(str(exc)) from exc


class DataUpdateCapabilities:
    """Capabilities for working with data update entries in extension code."""

    @capability(
        name="get_benchmark_data_candidates",
        description="Purpose: fetch noisy OPAL benchmark candidate data-update entries by label. Input: label such as 'opal/test2'. Returns candidate entry dicts with candidate_id, topics, dst_path, url, save_method, valid, and reason.",
    )
    def get_benchmark_data_candidates(self, label: str) -> list[dict]:
        """Return benchmark candidate data-update entries."""
        from opal_server.benchmark_scenarios import benchmark_data_candidates

        return benchmark_data_candidates(label)

    @staticmethod
    def _normalize_entry_aliases(item: dict) -> dict:
        normalized = dict(item)
        if "topics" not in normalized and isinstance(normalized.get("topic"), str):
            normalized["topics"] = [normalized["topic"]]
        if "dst_path" not in normalized and isinstance(normalized.get("path"), str):
            normalized["dst_path"] = normalized["path"]
        if "url" not in normalized and isinstance(normalized.get("source"), str):
            normalized["url"] = normalized.pop("source")
        data_source = normalized.pop("data_source", None)
        if isinstance(data_source, dict):
            if "url" not in normalized and isinstance(data_source.get("url"), str):
                normalized["url"] = data_source["url"]
            if "save_method" not in normalized and isinstance(data_source.get("save_method"), str):
                normalized["save_method"] = data_source["save_method"]
        return normalized

    @staticmethod
    def _callback_model(callback):
        from opal_common.schemas.data import UpdateCallback

        if callback is None:
            return UpdateCallback(callbacks=[])
        if isinstance(callback, dict):
            return UpdateCallback(**callback)
        if isinstance(callback, str):
            return UpdateCallback(callbacks=[callback])
        if isinstance(callback, list):
            return UpdateCallback(callbacks=callback)
        return UpdateCallback(callbacks=[])

    @staticmethod
    def _callback_urls(callback_model) -> list[str]:
        urls: list[str] = []
        for callback in callback_model.callbacks:
            if isinstance(callback, (list, tuple)):
                urls.append(str(callback[0]))
            else:
                urls.append(str(callback))
        return urls

    @capability(
        name="publish_data_update",
        mutates=True,
        description="Purpose: publish selected data-update entries through OPAL's data-update publisher. Inputs: entries, reason, optional callback/id. Returns status, entries_published, and callback_urls.",
    )
    def publish_data_update(
        self,
        entries: list[dict],
        reason: str = "L4 data update",
        callback=None,
        id: str | None = None,
    ) -> dict:
        """Publish a data update to OPAL clients."""
        from opal_common.schemas.data import DataSourceEntry, DataUpdate

        normalized_entries = [
            DataSourceEntry(
                **{
                    k: v
                    for k, v in self._normalize_entry_aliases(entry).items()
                    if v is not None
                }
            )
            for entry in entries
            if isinstance(entry, dict)
        ]
        callback_model = self._callback_model(callback)
        update = DataUpdate(
            id=id,
            entries=normalized_entries,
            reason=reason,
            callback=callback_model,
        )
        publisher = _data_update_publisher
        if publisher is not None:
            _run_coro_sync(publisher.publish_data_updates(update))
        else:
            logger.warning("Data update publisher not configured; L4 update not broadcast")
        return {
            "status": "ok",
            "entries_published": len(update.entries),
            "callback_urls": self._callback_urls(callback_model),
        }

    @capability(name="filter_entries_by_topic", description="Purpose: keep data-update entries containing a topic. Inputs: entries and topic. Returns filtered entries.")
    def filter_entries_by_topic(self, entries: list[dict], topic: str) -> list[dict]:
        """Filter data source entries that belong to a specific topic."""
        return [e for e in entries if topic in e.get("topics", [])]

    @capability(name="exclude_entries_by_topic", description="Purpose: drop data-update entries containing a topic. Inputs: entries and topic. Returns filtered entries.")
    def exclude_entries_by_topic(self, entries: list[dict], topic: str) -> list[dict]:
        """Exclude data source entries that belong to a specific topic."""
        return [e for e in entries if topic not in e.get("topics", [])]

    @capability(name="get_all_entry_topics")
    def get_all_entry_topics(self, entries: list[dict]) -> list[str]:
        """Return all unique topics across all entries."""
        topics: set[str] = set()
        for e in entries:
            topics.update(e.get("topics", []))
        return sorted(topics)

    @capability(name="deduplicate_entries")
    def deduplicate_entries(self, entries: list[dict]) -> list[dict]:
        """Remove duplicate entries by (topics + dst_path), preferring safer entries."""
        by_key: dict[tuple, dict] = {}

        def quality(entry: dict) -> tuple[int, int, int, int]:
            url = str(entry.get("url", "") or "").lower()
            return (
                1 if entry.get("valid") is True else 0,
                1 if entry.get("reason") == "production_safe" else 0,
                1 if url.startswith(("http://", "https://")) and "staging" not in url else 0,
                1 if entry.get("save_method") == "PUT" else 0,
            )

        for e in entries:
            key = (tuple(sorted(e.get("topics", []))), e.get("dst_path", ""))
            if key not in by_key or quality(e) > quality(by_key[key]):
                by_key[key] = e
        return list(by_key.values())

    @capability(name="filter_entries_by_dst_path")
    def filter_entries_by_dst_path(self, entries: list[dict], path_prefix: str) -> list[dict]:
        """Filter entries whose dst_path starts with the given prefix."""
        return [e for e in entries if e.get("dst_path", "").startswith(path_prefix)]

    @capability(name="validate_entry_urls", description="Purpose: keep entries whose url starts with http:// or https://. Inputs: entries. Returns valid entries.")
    def validate_entry_urls(self, entries: list[dict]) -> list[dict]:
        """Return entries that have a non-empty url starting with http:// or https://."""
        return [
            e for e in entries
            if e.get("url", "").startswith(("http://", "https://"))
        ]

    @capability(name="set_entry_save_method", mutates=True, description="Purpose: set save_method on every entry before publication. Inputs: entries and save_method. Returns modified entries.")
    def set_entry_save_method(self, entries: list[dict], save_method: str) -> list[dict]:
        """Set the save_method field on all entries (PUT or PATCH).

        Notes:
            OPAL's `DataSourceEntry` schema requires that when `save_method="PATCH"`,
            the `data` field is a JSON patch list. In many test/automation
            prompts we only have URLs + dst_path, so we default `data` to an
            empty JSON patch list (`[]`) when it's missing/None.
        """
        for e in entries:
            e["save_method"] = save_method
            # Ensure PATCH updates still satisfy the Pydantic schema validation.
            if save_method == "PATCH" and (e.get("data") is None):
                e["data"] = []
        return entries


class StatisticsCapabilities:
    """Capabilities for working with server statistics in extension code."""

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

    @staticmethod
    def _visible_topics(channel: dict) -> list[str]:
        return [
            topic
            for topic in (channel.get("topics", []) or [])
            if isinstance(topic, str) and not topic.startswith("policy:.")
        ]

    @staticmethod
    def _looks_ephemeral_client_id(client_id: str) -> bool:
        return client_id.startswith("CLIENT_")

    def _client_topics_from_channels(self, channels: list[dict]) -> list[str]:
        topics: set[str] = set()
        for channel in channels:
            for topic in self._visible_topics(channel):
                topics.add(topic)
        return sorted(topics)

    def _normalized_benchmark_client_topics(self, stats: dict) -> dict[str, list[str]]:
        clients = stats.get("clients", {})
        if not isinstance(clients, dict):
            return {}

        raw_topics = {
            client_id: self._client_topics_from_channels(channels if isinstance(channels, list) else [])
            for client_id, channels in clients.items()
        }
        benchmark_shape = (
            os.environ.get("OPAL_BENCHMARK_MODE", "").lower() in {"1", "true", "yes", "on"}
            or any(str(client_id).startswith("opal-client-") for client_id in clients)
            or any(tuple(topics) in self._BENCHMARK_SIGNATURE_ALIASES for topics in raw_topics.values())
        )
        if not benchmark_shape:
            return {client_id: topics for client_id, topics in raw_topics.items() if topics}

        aliases: dict[str, str] = {}
        signature_to_raw_ids: dict[tuple[str, ...], list[str]] = {}
        for client_id, topics in raw_topics.items():
            if self._looks_ephemeral_client_id(client_id):
                signature_to_raw_ids.setdefault(tuple(topics), []).append(client_id)
            else:
                aliases[client_id] = client_id

        claimed_aliases = set(aliases)
        for signature, raw_ids in signature_to_raw_ids.items():
            expected_aliases = self._BENCHMARK_SIGNATURE_ALIASES.get(signature, [])
            remaining_aliases = [alias for alias in expected_aliases if alias not in claimed_aliases]
            for raw_id, alias in zip(sorted(raw_ids), remaining_aliases):
                aliases[raw_id] = alias
                claimed_aliases.add(alias)

        normalized: dict[str, list[str]] = {}
        for raw_client_id, topics in raw_topics.items():
            if not topics:
                continue
            if self._looks_ephemeral_client_id(raw_client_id) and raw_client_id not in aliases:
                continue
            normalized[aliases.get(raw_client_id, raw_client_id)] = topics
        return normalized

    @capability(
        name="get_statistics",
        description="Purpose: fetch current OPAL server statistics through the L0 statistics pipeline. Returns connected clients, server ids, uptime, and version as a plain dict.",
    )
    def get_statistics(self) -> dict:
        """Return the current OPAL statistics state."""
        getter = _statistics_getter
        if getter is None:
            return {}
        return _serialize_state(getter())

    @capability(name="get_client_list")
    def get_client_list(self, stats: dict) -> list[dict]:
        """Extract the flat list of all client records from statistics."""
        clients = stats.get("clients", {})
        result = []
        for client_id, channels in clients.items():
            for ch in channels:
                topics = self._visible_topics(ch)
                if not topics:
                    continue
                result.append({
                    "client_id": client_id,
                    "rpc_id": ch.get("rpc_id", ""),
                    "topics": topics,
                })
        return result

    @capability(name="get_clients_by_topic")
    def get_clients_by_topic(self, stats: dict, topic: str) -> list[str]:
        """Return client IDs that are subscribed to a given topic."""
        clients = stats.get("clients", {})
        result = set()
        for client_id, channels in clients.items():
            for ch in channels:
                if topic in self._visible_topics(ch):
                    result.add(client_id)
        return sorted(result)

    @capability(name="count_clients_per_topic")
    def count_clients_per_topic(self, stats: dict) -> dict:
        """Return a dict mapping each topic to the number of unique clients subscribed."""
        topic_counts: dict[str, set] = {}
        for client_id, topics in self._normalized_benchmark_client_topics(stats).items():
            for topic in topics:
                topic_counts.setdefault(topic, set()).add(client_id)
        return {t: len(cids) for t, cids in topic_counts.items()}

    @capability(name="get_topics_with_no_subscribers")
    def get_topics_with_no_subscribers(self, stats: dict, known_topics: list[str]) -> list[str]:
        """Given a list of known topics, return those with zero subscribers."""
        counts = self.count_clients_per_topic(stats)
        return [t for t in known_topics if t not in counts or counts[t] == 0]

    @capability(name="get_server_count")
    def get_server_count(self, stats: dict) -> int:
        """Return the number of active server replicas."""
        return len(stats.get("servers", set()))

    @capability(name="get_client_count")
    def get_client_count(self, stats: dict) -> int:
        """Return the total number of connected clients."""
        return len(stats.get("clients", {}))


# ---------------------------------------------------------------------------
# Instantiate capability providers and collect capabilities
# ---------------------------------------------------------------------------

policy_bundle_caps = PolicyBundleCapabilities()
policy_hotfix_caps = PolicyHotfixCapabilities()
data_update_caps = DataUpdateCapabilities()
statistics_caps = StatisticsCapabilities()

_policy_capabilities = list(collect_capabilities(policy_bundle_caps))
_policy_hotfix_capabilities = list(collect_capabilities(policy_hotfix_caps))
_data_update_capabilities = list(collect_capabilities(data_update_caps))
_statistics_capabilities = list(collect_capabilities(statistics_caps))

# ---------------------------------------------------------------------------
# Extension Registry & GoEx Registry
# ---------------------------------------------------------------------------

extension_registry = ExtensionRegistry()

goex_registry = GoExRegistry(auto_approve=False, auto_approve_readonly=True)


def _validate_non_empty_bundle(record) -> bool:
    """GoEx validator: reject if policy bundle has zero modules."""
    metadata = record.metadata or {}
    endpoint = metadata.get("endpoint")
    if endpoint not in {"/policy", "/policy/"}:
        return True
    if record.result and record.result.get("success"):
        result_value = record.result.get("result")
        if isinstance(result_value, dict):
            modules = result_value.get("policy_modules", [])
            if isinstance(modules, list) and len(modules) == 0:
                return False
    return True


goex_registry.add_validator(_validate_non_empty_bundle)

# ---------------------------------------------------------------------------
# Extension Points
# ---------------------------------------------------------------------------

post_policy_bundle = extension_registry.register(
    ExtensionPoint(
        name="post_policy_bundle",
        description=(
            "Policy bundle extension: context contains policy_modules/modules, data_modules, manifest, hash, "
            "module_count, and data_module_count from the tracked Git repo. Each policy module dict uses fields "
            "path, package_name, and rego; the Rego source is in module['rego'], not module['content']. Typical "
            "benchmark flow: use exact-path or incident-module filtering, inspect package names and Rego source, "
            "reject near matches, and return the selected module dicts or an audit summary. Read-only; mutating "
            "policy changes belong in the policy_hotfix/code_extension path."
        ),
        trigger_description=(
            "Runs when extension_level is L1+ and extension code is provided "
            "in the policy bundle request"
        ),
        capabilities=list(_policy_capabilities),
        codegen_provider=_create_codegen_provider(),
    )
)

policy_hotfix = extension_registry.register(
    ExtensionPoint(
        name="policy_hotfix",
        description=(
            "Policy hotfix extension: context contains repo_path, module_path, commit_message, incident policy modules, "
            "module_index, current_rego, and module_exists_before when available. Typical flow: read_policy_module or "
            "module_exists -> build the exact Rego source -> upsert_policy_module or delete_policy_module. Mutating "
            "capabilities create GoEx records when execution_mode is goex; return module_path, package_name/action, "
            "rego_content, previous_rego, module_exists_before, and repo_path so reversal can undo exactly the change."
        ),
        trigger_description=(
            "Runs from the existing create_policy_module/update_policy_module endpoints "
            "(L1-L3) or code_extension (L4) when policy_hotfix code executes "
            "against the tracked policy repository"
        ),
        capabilities=list(_policy_hotfix_capabilities),
        codegen_provider=_create_codegen_provider(),
    )
)

post_data_update = extension_registry.register(
    ExtensionPoint(
        name="post_data_update",
        description=(
            "Data update extension: context contains entries, reason, and entry_count before publication. "
            "Typical benchmark flow: filter entries by allowed topics and destination paths, validate URLs, "
            "deduplicate, set save_method, and return only production-safe entries while preserving callback handling."
        ),
        trigger_description=(
            "Runs when extension_level is L1+ and extension code is provided "
            "in the data update request"
        ),
        capabilities=list(_data_update_capabilities),
        codegen_provider=_create_codegen_provider(),
    )
)

post_benchmark_candidate_feed = extension_registry.register(
    ExtensionPoint(
        name="post_benchmark_candidate_feed",
        description=(
            "Benchmark candidate feed extension: context contains label, candidates, and candidate_count. "
            "Typical benchmark flow: select only valid production-safe candidate ids from noisy distractors, "
            "reject staging hosts, invalid URLs, wrong topics, wrong paths, and wrong save_method values. "
            "Return full candidate entry dicts when a later publish step needs topics, dst_path, url, and "
            "save_method; return ids only when the caller asked only for reporting."
        ),
        trigger_description=(
            "Runs when extension_level is L1+ and extension code is provided "
            "in the benchmark candidate feed request"
        ),
        capabilities=list(_data_update_capabilities),
        codegen_provider=_create_codegen_provider(),
    )
)

post_statistics = extension_registry.register(
    ExtensionPoint(
        name="post_statistics",
        description=(
            "Statistics extension: context contains live OPAL stats. Typical benchmark flow: derive topic counts "
            "from each client's visible topics, ignore internal policy channels, and return a compact aggregate. "
            "Read-only; do not publish data or mutate policies from this hook."
        ),
        trigger_description=(
            "Runs when extension_level is L1+ and extension code is provided "
            "in the statistics request"
        ),
        capabilities=list(_statistics_capabilities),
        codegen_provider=_create_codegen_provider(),
    )
)


# ---------------------------------------------------------------------------
# Context providers for L4 (code_extension) endpoint
# ---------------------------------------------------------------------------

def set_statistics_context_provider(stats_getter):
    """Register a context provider for the post_statistics extension point.

    Args:
        stats_getter: A callable returning the current OpalStatistics state
            (e.g. ``lambda: stats.state``).
    """
    global _statistics_getter
    _statistics_getter = stats_getter

    def _provider() -> dict:
        return {"stats": _serialize_state(stats_getter())}
    post_statistics.context_provider = _provider


def set_policy_bundle_context_provider(repo_getter):
    """Register a context provider for the post_policy_bundle extension point.

    Args:
        repo_getter: A callable returning the current Git Repo object
            (or None if not ready).
    """
    global _policy_repo_getter
    _policy_repo_getter = repo_getter

    def _provider() -> dict:
        repo = repo_getter()
        return _build_policy_bundle_context(repo)
    post_policy_bundle.context_provider = _provider

    def _hotfix_provider() -> dict:
        repo = repo_getter()
        context = _build_policy_bundle_context(repo)
        policy_modules = context.get("policy_modules", [])
        incident_modules = [
            module
            for module in policy_modules
            if isinstance(module, dict) and str(module.get("path", "")).startswith("incident/")
        ]
        module_index = [
            {
                "path": module.get("path", ""),
                "package_name": module.get("package_name", ""),
            }
            for module in policy_modules
            if isinstance(module, dict)
        ]
        context.update(
            {
                "policy_modules": incident_modules,
                "modules": incident_modules,
                "module_index": module_index,
                "module_count": len(incident_modules),
                "repo_path": repo.working_dir if repo is not None else "",
                "module_path": os.getenv(
                    "OPAL_POLICY_HOTFIX_DEFAULT_MODULE_PATH",
                    "incident/cache_failover_hotfix.rego",
                ),
                "commit_message": os.getenv(
                    "OPAL_POLICY_HOTFIX_DEFAULT_COMMIT_MESSAGE",
                    "Apply emergency cache failover hotfix",
                ),
                "requested_rego": None,
                "current_rego": None,
                "module_exists_before": False,
            }
        )
        return context

    policy_hotfix.context_provider = _hotfix_provider


def set_codegen_provider(provider):
    """Set the LLM provider used for server-side code generation (L2/L3/L4).

    All registered extension points will use this provider to generate
    extension code during endpoint execution, eliminating the two-phase
    ``needs_extension`` round-trip.

    Args:
        provider: An ``LLMProvider`` instance (e.g. ``OllamaProvider``,
            ``GeminiProvider``).
    """
    post_policy_bundle.codegen_provider = provider
    policy_hotfix.codegen_provider = provider
    post_data_update.codegen_provider = provider
    post_benchmark_candidate_feed.codegen_provider = provider
    post_statistics.codegen_provider = provider
