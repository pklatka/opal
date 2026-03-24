"""
Symphony Extension Integration for OPAL Server
================================================
Defines capabilities, extension points, and registries that integrate
Symphony's extension framework into the OPAL server.

Extension points:
  - post_policy_bundle: filter/transform policy bundles before serving
  - post_data_update:   validate/transform data update entries before publishing
  - post_statistics:    aggregate/alert on server statistics before returning

Capabilities are grouped into classes whose methods are decorated with
@capability and collected at startup.
"""

from __future__ import annotations

import os

from symphony import (
    ExtensionPoint,
    ExtensionRegistry,
    GoExRegistry,
    collect_capabilities,
    capability,
    SYSTEM_PROMPTS,
)
from symphony.sandbox import set_default_executor, PythonSandboxExecutor

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


# ---------------------------------------------------------------------------
# Capability classes — methods are exposed to sandbox extension code
# ---------------------------------------------------------------------------


class PolicyBundleCapabilities:
    """Capabilities for working with policy bundles in extension code."""

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
        return sorted({m.get("package_name", "") for m in modules if m.get("package_name")})

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


class DataUpdateCapabilities:
    """Capabilities for working with data update entries in extension code."""

    @capability(name="filter_entries_by_topic")
    def filter_entries_by_topic(self, entries: list[dict], topic: str) -> list[dict]:
        """Filter data source entries that belong to a specific topic."""
        return [e for e in entries if topic in e.get("topics", [])]

    @capability(name="exclude_entries_by_topic")
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
        """Remove duplicate entries (by url + dst_path combination)."""
        seen: set[tuple] = set()
        result = []
        for e in entries:
            key = (e.get("url", ""), e.get("dst_path", ""))
            if key not in seen:
                seen.add(key)
                result.append(e)
        return result

    @capability(name="filter_entries_by_dst_path")
    def filter_entries_by_dst_path(self, entries: list[dict], path_prefix: str) -> list[dict]:
        """Filter entries whose dst_path starts with the given prefix."""
        return [e for e in entries if e.get("dst_path", "").startswith(path_prefix)]

    @capability(name="validate_entry_urls")
    def validate_entry_urls(self, entries: list[dict]) -> list[dict]:
        """Return entries that have a non-empty url starting with http:// or https://."""
        return [
            e for e in entries
            if e.get("url", "").startswith(("http://", "https://"))
        ]

    @capability(name="set_entry_save_method", mutates=True)
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

    @capability(name="get_client_list")
    def get_client_list(self, stats: dict) -> list[dict]:
        """Extract the flat list of all client records from statistics."""
        clients = stats.get("clients", {})
        result = []
        for client_id, channels in clients.items():
            for ch in channels:
                result.append({
                    "client_id": client_id,
                    "rpc_id": ch.get("rpc_id", ""),
                    "topics": ch.get("topics", []),
                })
        return result

    @capability(name="get_clients_by_topic")
    def get_clients_by_topic(self, stats: dict, topic: str) -> list[str]:
        """Return client IDs that are subscribed to a given topic."""
        clients = stats.get("clients", {})
        result = set()
        for client_id, channels in clients.items():
            for ch in channels:
                if topic in ch.get("topics", []):
                    result.add(client_id)
        return sorted(result)

    @capability(name="count_clients_per_topic")
    def count_clients_per_topic(self, stats: dict) -> dict:
        """Return a dict mapping each topic to the number of unique clients subscribed."""
        clients = stats.get("clients", {})
        topic_counts: dict[str, set] = {}
        for client_id, channels in clients.items():
            for ch in channels:
                for topic in ch.get("topics", []):
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
data_update_caps = DataUpdateCapabilities()
statistics_caps = StatisticsCapabilities()

_policy_capabilities = list(collect_capabilities(policy_bundle_caps))
_data_update_capabilities = list(collect_capabilities(data_update_caps))
_statistics_capabilities = list(collect_capabilities(statistics_caps))

# ---------------------------------------------------------------------------
# Extension Registry & GoEx Registry
# ---------------------------------------------------------------------------

extension_registry = ExtensionRegistry()

goex_registry = GoExRegistry(auto_approve=False, auto_approve_readonly=True)


def _validate_non_empty_bundle(record) -> bool:
    """GoEx validator: reject if policy bundle has zero modules."""
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
            "Post-processing hook for policy bundles. Extension code can "
            "filter, transform, or augment the policy bundle before it is "
            "served to clients. Useful for excluding test policies from "
            "production bundles, injecting environment-specific data, "
            "filtering by package or directory, or implementing custom "
            "bundle construction logic."
        ),
        trigger_description=(
            "Runs when extension_level is L1+ and extension code is provided "
            "in the policy bundle request"
        ),
        capabilities=list(_policy_capabilities),
        codegen_provider=_create_codegen_provider(),
    )
)

post_data_update = extension_registry.register(
    ExtensionPoint(
        name="post_data_update",
        description=(
            "Post-processing hook for data update events. Extension code can "
            "validate, filter, deduplicate, or transform data source entries "
            "before they are published to clients. Useful for conditional "
            "publishing, entry validation, topic-based filtering, or "
            "implementing custom publication logic."
        ),
        trigger_description=(
            "Runs when extension_level is L1+ and extension code is provided "
            "in the data update request"
        ),
        capabilities=list(_data_update_capabilities),
        codegen_provider=_create_codegen_provider(),
    )
)

post_statistics = extension_registry.register(
    ExtensionPoint(
        name="post_statistics",
        description=(
            "Post-processing hook for server statistics. Extension code can "
            "compute aggregates, detect anomalies, generate alerts, or "
            "reformat statistics before they are returned. Useful for "
            "monitoring, alerting on disconnected clients, or building "
            "custom dashboards."
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
    def _provider() -> dict:
        state = stats_getter()
        state_dict = state.dict() if hasattr(state, "dict") else state.model_dump()
        return {"stats": state_dict}
    post_statistics.context_provider = _provider


def set_policy_bundle_context_provider(repo_getter):
    """Register a context provider for the post_policy_bundle extension point.

    Args:
        repo_getter: A callable returning the current Git Repo object
            (or None if not ready).
    """
    from opal_common.git_utils.bundle_maker import BundleMaker
    from opal_server.config import opal_server_config
    from pathlib import Path

    def _provider() -> dict:
        repo = repo_getter()
        if repo is None or len(repo.heads) == 0:
            return {"policy_modules": [], "modules": [], "data_modules": [],
                    "manifest": [], "hash": "", "module_count": 0, "data_module_count": 0}
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
        return {
            "policy_modules": policy_modules,
            "modules": policy_modules,
            "data_modules": bundle_dict.get("data_modules", []),
            "manifest": bundle_dict.get("manifest", []),
            "hash": bundle_dict.get("hash", ""),
            "old_hash": bundle_dict.get("old_hash"),
            "module_count": len(policy_modules),
            "data_module_count": len(bundle_dict.get("data_modules", [])),
        }
    post_policy_bundle.context_provider = _provider


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
    post_data_update.codegen_provider = provider
    post_statistics.codegen_provider = provider
