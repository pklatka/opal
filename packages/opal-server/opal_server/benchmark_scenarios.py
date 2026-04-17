from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
        summary_token="cache_failover",
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
    modules: dict[str, str] = {}
    for scenario in STANDARD_SCENARIOS.values():
        if scenario.baseline_module_path and scenario.baseline_module_rego:
            modules[scenario.baseline_module_path] = scenario.baseline_module_rego
    return modules


def benchmark_reset_delete_paths() -> list[str]:
    delete_paths = set()
    for scenario in STANDARD_SCENARIOS.values():
        delete_paths.update(scenario.delete_module_paths)
    return sorted(delete_paths)
