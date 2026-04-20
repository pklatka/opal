from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _allow_block(*conditions: str) -> str:
    body = "\n".join(f"    {condition}" for condition in conditions)
    return f"allow {{\n{body}\n}}"


def payments_outage_gate_baseline_rego() -> str:
    return """package app.incident.payments_outage_gate

default allow = false

# Baseline outage gate: only platform admins can force the action.
allow {
    input.actor.class == "platform_admin"
    input.request.operation == "cache_failover"
}
"""


def payments_replica_promote_hotfix_rego() -> str:
    return """package app.incident.payments_replica_promote_hotfix

default allow = false

allow {
    input.incident.severity == "sev-1"
    input.actor.class == "oncall_responder"
    input.flags.emergency_override == true
    input.request.operation == "promote_replica"
}
"""


def cache_failover_hotfix_rego() -> str:
    return """package app.incident.cache_failover_hotfix

default allow = false

allow {
    input.incident.severity == "sev-1"
    input.actor.class == "oncall_responder"
    input.flags.emergency_override == true
    input.request.operation == "cache_failover"
}
"""


def payments_break_glass_rego() -> str:
    return """package app.incident.break_glass

default allow = false

allow {
    input.incident.severity == "sev-1"
    input.actor.class == "oncall_responder"
    input.flags.emergency_override == true
    input.request.operation == "cache_failover"
    not input.time.within_business_hours
}
"""


def _module_text(package_name: str, *conditions: str) -> str:
    return "\n".join(
        [
            f"package {package_name}",
            "",
            "default allow = false",
            "",
            _allow_block(*conditions),
            "",
        ]
    )


def benchmark_rejected_candidate_paths() -> list[str]:
    return [
        "incident/payments_replica_promote_admin.rego",
        "incident/payments_replica_promote_no_severity.rego",
        "incident/payments_replica_delete_cluster.rego",
        "incident/payments_replica_promote_business_hours.rego",
        "incident/payments_replica_promote_no_override.rego",
        "incident/break_glass_oncall_promote_replica.rego",
    ]


def benchmark_valid_candidate_ids(label: str) -> list[str]:
    if label != "opal/test2":
        return []
    return ["cand_02", "cand_06", "cand_11"]


def benchmark_fixture_policy_modules() -> dict[str, str]:
    modules = {
        "incident/break_glass.rego": payments_break_glass_rego(),
        "incident/break_glass_platform_admin.rego": _module_text(
            "app.incident.break_glass_platform_admin",
            'input.incident.severity == "sev-1"',
            'input.actor.class == "platform_admin"',
            'input.request.operation == "cache_failover"',
        ),
        "incident/break_glass_oncall_business_hours.rego": _module_text(
            "app.incident.break_glass_oncall_business_hours",
            'input.incident.severity == "sev-1"',
            'input.actor.class == "oncall_responder"',
            'input.flags.emergency_override == true',
            'input.request.operation == "cache_failover"',
            "input.time.within_business_hours",
        ),
        "incident/break_glass_oncall_no_override.rego": _module_text(
            "app.incident.break_glass_oncall_no_override",
            'input.incident.severity == "sev-1"',
            'input.actor.class == "oncall_responder"',
            'input.request.operation == "cache_failover"',
            "not input.time.within_business_hours",
        ),
        "incident/break_glass_oncall_promote_replica.rego": _module_text(
            "app.incident.break_glass_oncall_promote_replica",
            'input.incident.severity == "sev-1"',
            'input.actor.class == "oncall_responder"',
            'input.flags.emergency_override == true',
            'input.request.operation == "promote_replica"',
        ),
        "incident/payments_replica_promote_admin.rego": _module_text(
            "app.incident.payments_replica_promote_admin",
            'input.actor.class == "platform_admin"',
            'input.request.operation == "promote_replica"',
        ),
        "incident/payments_replica_promote_no_severity.rego": _module_text(
            "app.incident.payments_replica_promote_no_severity",
            'input.actor.class == "oncall_responder"',
            'input.flags.emergency_override == true',
            'input.request.operation == "promote_replica"',
        ),
        "incident/payments_replica_delete_cluster.rego": _module_text(
            "app.incident.payments_replica_delete_cluster",
            'input.incident.severity == "sev-1"',
            'input.actor.class == "oncall_responder"',
            'input.flags.emergency_override == true',
            'input.request.operation == "delete_cluster"',
        ),
        "incident/payments_replica_promote_business_hours.rego": _module_text(
            "app.incident.payments_replica_promote_business_hours",
            'input.incident.severity == "sev-1"',
            'input.actor.class == "oncall_responder"',
            'input.flags.emergency_override == true',
            'input.request.operation == "promote_replica"',
            "input.time.within_business_hours",
        ),
        "incident/payments_replica_promote_no_override.rego": _module_text(
            "app.incident.payments_replica_promote_no_override",
            'input.incident.severity == "sev-1"',
            'input.actor.class == "oncall_responder"',
            'input.request.operation == "promote_replica"',
        ),
        "audit/cache_failover_observer.rego": _module_text(
            "app.audit.cache_failover_observer",
            'input.request.operation == "view_incident"',
            'input.actor.class == "auditor"',
        ),
        "finance/payments_approver.rego": _module_text(
            "app.finance.payments_approver",
            'input.actor.class == "finance_manager"',
            'input.request.operation == "approve_wire"',
        ),
        "staging/break_glass.rego": _module_text(
            "app.staging.break_glass",
            'input.environment == "staging"',
            'input.request.operation == "cache_failover"',
        ),
        "audit/directory_readonly.rego": _module_text(
            "app.audit.directory_readonly",
            'input.actor.class == "auditor"',
            'input.request.operation == "read_directory"',
        ),
        "finance/report_export.rego": _module_text(
            "app.finance.report_export",
            'input.actor.class == "finance_analyst"',
            'input.request.operation == "export_report"',
        ),
        "staging/feature_flag_override.rego": _module_text(
            "app.staging.feature_flag_override",
            'input.environment == "staging"',
            'input.request.operation == "toggle_feature_flag"',
        ),
    }

    for idx in range(1, 16):
        modules[f"incident/break_glass_variant_{idx:02d}.rego"] = _module_text(
            f"app.incident.break_glass_variant_{idx:02d}",
            'input.incident.severity == "sev-1"',
            'input.actor.class == "oncall_responder"',
            'input.flags.emergency_override == true',
            'input.request.operation == "cache_failover"',
            "not input.time.within_business_hours" if idx % 5 == 0 else "input.time.within_business_hours",
        )
    for idx in range(1, 13):
        modules[f"incident/payments_replica_variant_{idx:02d}.rego"] = _module_text(
            f"app.incident.payments_replica_variant_{idx:02d}",
            'input.incident.severity == "sev-1"' if idx % 2 == 0 else 'input.incident.severity == "sev-2"',
            'input.actor.class == "oncall_responder"' if idx % 3 else 'input.actor.class == "platform_admin"',
            'input.flags.emergency_override == true' if idx % 4 else 'input.flags.emergency_override == false',
            'input.request.operation == "promote_replica"' if idx % 5 else 'input.request.operation == "delete_cluster"',
        )
    for idx in range(1, 13):
        modules[f"audit/filler_{idx:02d}.rego"] = _module_text(
            f"app.audit.filler_{idx:02d}",
            'input.actor.class == "auditor"',
            f'input.request.operation == "audit_action_{idx:02d}"',
        )
    for idx in range(1, 13):
        modules[f"finance/filler_{idx:02d}.rego"] = _module_text(
            f"app.finance.filler_{idx:02d}",
            'input.actor.class == "finance_manager"',
            f'input.request.operation == "finance_action_{idx:02d}"',
        )
    for idx in range(1, 13):
        modules[f"staging/filler_{idx:02d}.rego"] = _module_text(
            f"app.staging.filler_{idx:02d}",
            'input.environment == "staging"',
            f'input.request.operation == "staging_action_{idx:02d}"',
        )
    return modules


def benchmark_data_candidates(label: str) -> list[dict[str, Any]]:
    if label != "opal/test2":
        return []
    candidates = [
        {"candidate_id": "cand_01", "topics": ["incident_access"], "dst_path": "/incident/access", "url": "http://staging-benchmark-data:8081/v1/incidents/sev1/access-grants", "save_method": "PUT", "valid": False, "reason": "staging_host"},
        {"candidate_id": "cand_02", "topics": ["incident_access"], "dst_path": "/incident/access", "url": "http://opal-benchmark-data:8081/v1/incidents/sev1/access-grants", "save_method": "PUT", "valid": True, "reason": "production_safe"},
        {"candidate_id": "cand_03", "topics": ["policy_data"], "dst_path": "/incident/access", "url": "http://opal-benchmark-data:8081/v1/incidents/sev1/access-grants", "save_method": "PUT", "valid": False, "reason": "wrong_topic"},
        {"candidate_id": "cand_04", "topics": ["incident_access"], "dst_path": "/incident/access/legacy", "url": "http://opal-benchmark-data:8081/v1/incidents/sev1/access-grants", "save_method": "PUT", "valid": False, "reason": "wrong_destination_path"},
        {"candidate_id": "cand_05", "topics": ["incident_access"], "dst_path": "/incident/access", "url": "http://opal-benchmark-data:8081/v1/incidents/sev0/access-grants", "save_method": "PUT", "valid": False, "reason": "duplicate_older_incident"},
        {"candidate_id": "cand_06", "topics": ["directory_sync"], "dst_path": "/directory/emergency/groups", "url": "http://opal-benchmark-data:8081/v1/directory/emergency/groups", "save_method": "PUT", "valid": True, "reason": "production_safe"},
        {"candidate_id": "cand_07", "topics": ["directory_sync"], "dst_path": "/directory/emergency/groups", "url": "http://opal-benchmark-data:8081/v1/directory/emergency/groups", "save_method": "PATCH", "valid": False, "reason": "wrong_save_method"},
        {"candidate_id": "cand_08", "topics": ["audit_logs"], "dst_path": "/audit/incident/raw", "url": "http://opal-benchmark-data:8081/v1/audit/incidents/raw", "save_method": "PUT", "valid": False, "reason": "disallowed_audit_topic"},
        {"candidate_id": "cand_09", "topics": ["feature_flags"], "dst_path": "/feature_flags/cache_failover", "url": "ftp://opal-benchmark-data:8081/v1/feature-flags/cache-failover", "save_method": "PUT", "valid": False, "reason": "invalid_url_scheme"},
        {"candidate_id": "cand_10", "topics": ["directory_sync"], "dst_path": "/directory/groups", "url": "http://opal-benchmark-data:8081/v1/directory/emergency/groups", "save_method": "PUT", "valid": False, "reason": "wrong_directory_path"},
        {"candidate_id": "cand_11", "topics": ["feature_flags"], "dst_path": "/feature_flags/cache_failover", "url": "http://opal-benchmark-data:8081/v1/feature-flags/cache-failover", "save_method": "PUT", "valid": True, "reason": "production_safe"},
        {"candidate_id": "cand_12", "topics": ["compliance_audit"], "dst_path": "/compliance/audit/failover", "url": "http://opal-benchmark-data:8081/v1/compliance/audit/failover", "save_method": "PUT", "valid": False, "reason": "disallowed_compliance_topic"},
    ]
    for idx in range(13, 73):
        mod = idx % 8
        if mod == 0:
            topic = ["incident_access"]
            dst_path = "/incident/access"
            url = f"http://staging-benchmark-data:8081/v1/incidents/sev1/access-grants/{idx}"
            reason = "staging_host"
        elif mod == 1:
            topic = ["policy_data"]
            dst_path = "/incident/access"
            url = f"http://opal-benchmark-data:8081/v1/incidents/sev1/access-grants/{idx}"
            reason = "wrong_topic"
        elif mod == 2:
            topic = ["incident_access"]
            dst_path = f"/incident/access/archive/{idx}"
            url = f"http://opal-benchmark-data:8081/v1/incidents/sev1/access-grants/{idx}"
            reason = "wrong_destination_path"
        elif mod == 3:
            topic = ["directory_sync"]
            dst_path = "/directory/emergency/groups"
            url = f"http://opal-benchmark-data:8081/v1/directory/legacy/groups/{idx}"
            reason = "duplicate_older_incident"
        elif mod == 4:
            topic = ["directory_sync"]
            dst_path = f"/directory/groups/{idx}"
            url = f"http://opal-benchmark-data:8081/v1/directory/emergency/groups/{idx}"
            reason = "wrong_directory_path"
        elif mod == 5:
            topic = ["feature_flags"]
            dst_path = "/feature_flags/cache_failover"
            url = f"ftp://opal-benchmark-data:8081/v1/feature-flags/cache-failover/{idx}"
            reason = "invalid_url_scheme"
        elif mod == 6:
            topic = ["audit_logs"]
            dst_path = f"/audit/incident/raw/{idx}"
            url = f"http://opal-benchmark-data:8081/v1/audit/incidents/raw/{idx}"
            reason = "disallowed_audit_topic"
        else:
            topic = ["compliance_audit"]
            dst_path = f"/compliance/audit/failover/{idx}"
            url = f"http://opal-benchmark-data:8081/v1/compliance/audit/failover/{idx}"
            reason = "disallowed_compliance_topic"
        save_method = "PATCH" if idx % 9 == 0 else "PUT"
        candidates.append(
            {
                "candidate_id": f"cand_{idx:02d}",
                "topics": topic,
                "dst_path": dst_path,
                "url": url,
                "save_method": save_method,
                "valid": False,
                "reason": "wrong_save_method" if save_method == "PATCH" else reason,
            }
        )
    return candidates


def benchmark_policy_module_count() -> int:
    return len(benchmark_reset_policy_modules())


def benchmark_candidate_count(label: str) -> int:
    return len(benchmark_data_candidates(label))


@dataclass(frozen=True)
class DecisionCheck:
    client_app: str
    package_path: str
    positive_input: dict[str, Any]
    negative_input: dict[str, Any]


@dataclass(frozen=True)
class StandardScenario:
    label: str
    final_json_key: str
    baseline_module_path: str | None = None
    baseline_module_rego: str | None = None
    delete_module_paths: tuple[str, ...] = ()
    decision_check: DecisionCheck | None = None


@dataclass(frozen=True)
class GoexScenario:
    case_id: str
    label_stub: str
    module_path: str
    package_name: str
    commit_message: str
    summary_token: str
    task: str
    decision_check: DecisionCheck | None = None
    expect_module_presence: bool = False


STANDARD_SCENARIOS: dict[str, StandardScenario] = {
    "opal/test1": StandardScenario(
        label="opal/test1",
        final_json_key="result",
        baseline_module_path="incident/payments_outage_gate.rego",
        baseline_module_rego=payments_outage_gate_baseline_rego(),
        delete_module_paths=(
            "incident/payments_replica_promote_hotfix.rego",
            "incident/cache_failover_hotfix.rego",
        ),
        decision_check=DecisionCheck(
            client_app="opal-client-authz-a",
            package_path="app/incident/payments_outage_gate/allow",
            positive_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": True},
                "request": {"operation": "cache_failover"},
                "time": {"within_business_hours": False},
            },
            negative_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": True},
                "request": {"operation": "cache_failover"},
                "time": {"within_business_hours": True},
            },
        ),
    ),
    "opal/test2": StandardScenario(
        label="opal/test2",
        final_json_key="summary",
        baseline_module_path="incident/payments_outage_gate.rego",
        baseline_module_rego=payments_outage_gate_baseline_rego(),
        delete_module_paths=(
            "incident/payments_replica_promote_hotfix.rego",
            "incident/cache_failover_hotfix.rego",
        ),
    ),
    "opal/test3": StandardScenario(
        label="opal/test3",
        final_json_key="result",
        baseline_module_path="incident/payments_outage_gate.rego",
        baseline_module_rego=payments_outage_gate_baseline_rego(),
        delete_module_paths=(
            "incident/payments_replica_promote_hotfix.rego",
            "incident/cache_failover_hotfix.rego",
        ),
        decision_check=DecisionCheck(
            client_app="opal-client-authz-a",
            package_path="app/incident/payments_replica_promote_hotfix/allow",
            positive_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": True},
                "request": {"operation": "promote_replica"},
            },
            negative_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": True},
                "request": {"operation": "delete_cluster"},
            },
        ),
    ),
}


GOEX_SCENARIOS: dict[str, GoexScenario] = {
    "current": GoexScenario(
        case_id="current",
        label_stub="hotfix",
        module_path="incident/cache_failover_hotfix.rego",
        package_name="app.incident.cache_failover_hotfix",
        commit_message="Apply emergency cache failover hotfix",
        summary_token="cache_failover",
        task="""A sev-1 outage in the production payments cluster forced traffic onto a stale cache replica. Apply an emergency OPAL policy hotfix so the on-call responders can run the cache failover operation while the incident is active. Use GoEx mode for safety and auditability.

Requirements:
- Use module_path "incident/cache_failover_hotfix.rego".
- Use commit_message "Apply emergency cache failover hotfix".
- The module must declare package "app.incident.cache_failover_hotfix".
- Include `default allow = false`.
- The policy should allow the request only when:
  - input.incident.severity == "sev-1"
  - input.actor.class == "oncall_responder"
  - input.flags.emergency_override == true
  - input.request.operation == "cache_failover"

If the module already exists, replace it with the corrected hotfix. End with a short operator-facing summary.""",
        decision_check=DecisionCheck(
            client_app="opal-client-authz-a",
            package_path="app/incident/cache_failover_hotfix/allow",
            positive_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": True},
                "request": {"operation": "cache_failover"},
            },
            negative_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": False},
                "request": {"operation": "cache_failover"},
            },
        ),
    ),
    "test1": GoexScenario(
        case_id="test1",
        label_stub="test1",
        module_path="incident/payments_outage_gate.rego",
        package_name="app.incident.payments_outage_gate",
        commit_message="Restore payments outage gate during sev-1 incident",
        summary_token="oncall_responder",
        task="""A sev-1 outage in the production payments cluster is still unresolved because the active authorization gate module at "incident/payments_outage_gate.rego" is too restrictive. Use GoEx mode to update the active module so on-call responders can perform the cache failover action, but only under the intended sev-1 break-glass conditions.

Requirements:
- Use module_path "incident/payments_outage_gate.rego".
- Use commit_message "Restore payments outage gate during sev-1 incident".
- The module must declare package "app.incident.payments_outage_gate".
- Include `default allow = false`.
- The final policy should allow only when:
  - input.incident.severity == "sev-1"
  - input.actor.class == "oncall_responder"
  - input.flags.emergency_override == true
  - input.request.operation == "cache_failover"
  - not input.time.within_business_hours

Read the existing tracked policy bundle and adapt the correct production break-glass semantics into the active module. Do not widen access beyond those outage conditions. End with a short operator-facing summary.""",
        decision_check=DecisionCheck(
            client_app="opal-client-authz-a",
            package_path="app/incident/payments_outage_gate/allow",
            positive_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": True},
                "request": {"operation": "cache_failover"},
                "time": {"within_business_hours": False},
            },
            negative_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": True},
                "request": {"operation": "cache_failover"},
                "time": {"within_business_hours": True},
            },
        ),
    ),
    "test3": GoexScenario(
        case_id="test3",
        label_stub="test3",
        module_path="incident/payments_replica_promote_hotfix.rego",
        package_name="app.incident.payments_replica_promote_hotfix",
        commit_message="Create payments replica promote hotfix",
        summary_token="promote_replica",
        task="""A sev-1 outage in the production payments cluster now requires a recovery action that no existing production policy covers: allowing on-call responders to run the replica promotion operation. Use GoEx mode to create a new emergency OPAL policy hotfix.

Requirements:
- Use module_path "incident/payments_replica_promote_hotfix.rego".
- Use commit_message "Create payments replica promote hotfix".
- The module must declare package "app.incident.payments_replica_promote_hotfix".
- Include `default allow = false`.
- The policy should allow the request only when:
  - input.incident.severity == "sev-1"
  - input.actor.class == "oncall_responder"
  - input.flags.emergency_override == true
  - input.request.operation == "promote_replica"

No existing module in the live bundle already grants this exact operation, so create a new policy module rather than overwriting an unrelated one. End with a short operator-facing summary.""",
        decision_check=DecisionCheck(
            client_app="opal-client-authz-a",
            package_path="app/incident/payments_replica_promote_hotfix/allow",
            positive_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": True},
                "request": {"operation": "promote_replica"},
            },
            negative_input={
                "incident": {"severity": "sev-1"},
                "actor": {"class": "oncall_responder"},
                "flags": {"emergency_override": True},
                "request": {"operation": "delete_cluster"},
            },
        ),
    ),
}


def get_standard_scenario(label: str) -> StandardScenario:
    return STANDARD_SCENARIOS[label]


def get_goex_scenario(case_id: str) -> GoexScenario:
    return GOEX_SCENARIOS[case_id]


def benchmark_reset_entries() -> list[dict[str, Any]]:
    return [
        {
            "url": "http://opal-benchmark-data:8081/v1/bootstrap/incident_access",
            "topics": ["incident_access"],
            "dst_path": "/incident/access",
            "save_method": "PUT",
            "data": {},
        },
        {
            "url": "http://opal-benchmark-data:8081/v1/bootstrap/directory_sync",
            "topics": ["directory_sync"],
            "dst_path": "/directory/emergency/groups",
            "save_method": "PUT",
            "data": {"groups": [], "source": "benchmark-reset"},
        },
        {
            "url": "http://opal-benchmark-data:8081/v1/bootstrap/feature_flags",
            "topics": ["feature_flags"],
            "dst_path": "/feature_flags/cache_failover",
            "save_method": "PUT",
            "data": {
                "flag": "cache_failover",
                "enabled": False,
                "rollout": "benchmark-reset",
            },
        },
        {
            "url": "http://opal-benchmark-data:8081/v1/bootstrap/audit_logs",
            "topics": ["audit_logs"],
            "dst_path": "/audit/incident/raw",
            "save_method": "PUT",
            "data": {},
        },
    ]


def benchmark_reset_policy_modules() -> dict[str, str]:
    modules: dict[str, str] = dict(benchmark_fixture_policy_modules())
    for scenario in STANDARD_SCENARIOS.values():
        if scenario.baseline_module_path and scenario.baseline_module_rego:
            modules[scenario.baseline_module_path] = scenario.baseline_module_rego
    return modules


def benchmark_reset_delete_paths() -> list[str]:
    delete_paths = set()
    for scenario in STANDARD_SCENARIOS.values():
        delete_paths.update(scenario.delete_module_paths)
    return sorted(delete_paths)
