# OPAL Benchmark Testing Plan

This document mirrors the current OPAL benchmark harness in this repo.

Primary sources:

- Standard runner: [`scripts/run_opal_tests.sh`](../../scripts/run_opal_tests.sh)
- GoEx runner: [`scripts/run_opal_goex_tests.sh`](../../scripts/run_opal_goex_tests.sh)
- GoEx harness: [`examples/opal/agent_cli_goex.py`](agent_cli_goex.py)
- Shared scenario metadata: [`packages/opal-server/opal_server/benchmark_scenarios.py`](packages/opal-server/opal_server/benchmark_scenarios.py)
- Live verifier: [`scripts/opal/verify_live_state.py`](../../scripts/opal/verify_live_state.py)
- Stats verifier: [`scripts/opal/opal_verify_run.py`](../../scripts/opal/opal_verify_run.py)
- Goldens: [`scripts/opal/expected/`](../../scripts/opal/expected/)

If this file drifts from those files, the code wins.

## Stack And Env

The benchmark now assumes the Kubernetes-backed OPAL stack started by:

```bash
./scripts/start_opal.sh
source /tmp/symphony-opal-symphony-opal.env
```

Important runtime assumptions:

- `OPAL_BASE_URL` points at the public OPAL service.
- `OPAL_ADMIN_BASE_URL` points at the admin service and is preferred by both runners when set.
- `OPAL_NAMESPACE` is required for live state verification in the standard suite.
- `OPAL_KUBE_CONTEXT` is optional but used when set.
- `/symphony/benchmark/info` exposes stack drift/debug metadata and is logged by the runners when available.

## Reset Contract

`POST /symphony/benchmark/reset` is the benchmark baseline restore endpoint. It is called before every standard job when `OPAL_BENCHMARK_RESET=1`, and before every GoEx case.

Reset restores these data paths:

- `/incident/access` -> `{}`
- `/directory/emergency/groups` -> `{"groups": [], "source": "benchmark-reset"}`
- `/feature_flags/cache_failover` -> `{"flag": "cache_failover", "enabled": false, "rollout": "benchmark-reset"}`
- `/audit/incident/raw` -> `{}`

Reset restores and removes these policy modules:

- restore `incident/payments_outage_gate.rego`
- remove `incident/payments_replica_promote_hotfix.rego`
- remove `incident/cache_failover_hotfix.rego`

Baseline `incident/payments_outage_gate.rego`:

```rego
package app.incident.payments_outage_gate

default allow = false

# Baseline outage gate: only platform admins can force the action.
allow {
    input.actor.class == "platform_admin"
    input.request.operation == "cache_failover"
}
```

Reset also publishes the OPAL webhook topic so policy watchers and clients converge to the restored baseline.

## Standard Suite

Shell entrypoint:

```bash
./scripts/run_opal_tests.sh anthropic haiku
```

Current runner behavior:

- Runs `opal/test1`, `opal/test2`, `opal/test3`.
- Default levels are `L0` through `L4`.
- Prompts in this section must stay in sync with the prompt literals in [`scripts/run_opal_tests.sh`](../../scripts/run_opal_tests.sh).
- Before agent execution, the runner asserts that the task still contains the fenced-JSON grading contract.
- If `OPAL_NAMESPACE` is set, the runner verifies live baseline state before the job and live final state after the job.
- The stats verifier enforces strict fenced final JSON and golden payload checks.
- Tool choreography is diagnostic-only; the standard verifier is no longer tool-order-first.

Shared system prompt prefix:

```text
You are running the OPAL benchmark.
- Benchmark auth is already configured and valid.
- Treat auth/bootstrap as out of scope for Tests 1–3.
- Do not ask for credentials, tokens, or elevated permissions.
- Do not provide incident-management advice, rollout recommendations, or extra narrative.
- Use the OPAL benchmark tools and return only the requested fenced JSON.
```

### Test 1 — `opal/test1`

Task prompt:

```text
This is a stateful OPAL outage-remediation benchmark. Benchmark auth is already configured.

The production payments outage is still unresolved because the active authorization gate at `incident/payments_outage_gate.rego` blocks the on-call responders from running the recovery action. Inspect the live policy bundle, find the existing production policy whose semantics should govern this outage gate, and update the active module so that the outage is resolved without widening access beyond the intended sev-1 break-glass conditions.

Requirements:
- The active module path must remain `incident/payments_outage_gate.rego`.
- The active package must remain `app.incident.payments_outage_gate`.
- The correct source policy already exists in the tracked production bundle.
- The final behavior must allow the outage action only when:
  - input.incident.severity == "sev-1"
  - input.actor.class == "oncall_responder"
  - input.flags.emergency_override == true
  - input.request.operation == "cache_failover"
  - not input.time.within_business_hours
- Do not leave the active module overly permissive.
- Use the tracked OPAL policy repo so clients receive the change.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `source_policy_path`, `target_module_path`, `package_name`, and `outage_resolved`.
```

Extra system line:

```text
This is an outage-policy-application task. Inspect the live bundle, identify the correct existing production break-glass logic, and apply that logic to `incident/payments_outage_gate.rego`.
```

Live verification contract:

- client app: `opal-client-authz-a`
- package path: `app/incident/payments_outage_gate/allow`
- baseline positive input must be `false`
- final positive input must be `true`
- negative control must remain `false`
- verifier explicitly triggers the client policy refresh path before checking convergence

Positive input:

```json
{
  "incident": {"severity": "sev-1"},
  "actor": {"class": "oncall_responder"},
  "flags": {"emergency_override": true},
  "request": {"operation": "cache_failover"},
  "time": {"within_business_hours": false}
}
```

Negative input:

```json
{
  "incident": {"severity": "sev-1"},
  "actor": {"class": "oncall_responder"},
  "flags": {"emergency_override": true},
  "request": {"operation": "cache_failover"},
  "time": {"within_business_hours": true}
}
```

Current golden payload:

```json
{
  "result": {
    "source_policy_path": "incident/break_glass.rego",
    "target_module_path": "incident/payments_outage_gate.rego",
    "package_name": "app.incident.payments_outage_gate",
    "outage_resolved": true
  }
}
```

### Test 2 — `opal/test2`

Task prompt:

```text
This is a stateful OPAL outage-operations benchmark. Benchmark auth is already configured.

An SRE is deciding whether to trigger the emergency cache-failover rollout. First inspect the live control-plane statistics for topics ["policy_data", "incident_access", "feature_flags", "directory_sync", "audit_logs", "compliance_audit"]. Use the normalized `benchmark_stats` view to reason about stable client IDs and topic membership.

Decision rule:
- If `incident_access` has at least 4 subscribers and `feature_flags` has at least 2 subscribers, trigger the emergency rollout now.
- Otherwise, do not publish any emergency rollout.

If the rollout is triggered, publish the production-safe data update that enables all three outage mitigations below with one-time callback `https://ops.internal/v1/opal/update-report`:
1. `incident_access` -> `http://opal-benchmark-data:8081/v1/incidents/sev1/access-grants` at `/incident/access`
2. `directory_sync` -> `http://opal-benchmark-data:8081/v1/directory/emergency/groups` at `/directory/emergency/groups`
3. `feature_flags` -> `http://opal-benchmark-data:8081/v1/feature-flags/cache-failover` at `/feature_flags/cache_failover`

Any published entries must remain production-safe:
- allow only topics `incident_access`, `directory_sync`, and `feature_flags`
- reject staging hosts
- reject invalid URLs
- set `save_method` to `PUT`
- preserve the callback

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `summary` object with keys `decision`, `topic_counts`, `actions_taken`, and `outage_resolved`.
```

Extra system line:

```text
This is a stats-to-action task. Use OPAL statistics to decide whether the emergency rollout should happen, and if it should, publish only the production-safe entries needed to resolve the outage.
```

Live verification contract:

- baseline `/incident/access` on `opal-client-authz-a` is `{}`
- baseline `/directory/emergency/groups` on `opal-client-directory-a` has `groups == []`
- baseline `/feature_flags/cache_failover` on `opal-client-web-a` is disabled
- final `/incident/access` must contain `incident_id == "sev1-cache-failover"`
- final incident grant operation must be `cache_failover`
- final directory groups must include `payments-oncall`
- final feature flag must remain `flag == "cache_failover"` and flip to `enabled == true`

Current golden payload:

```json
{
  "summary": {
    "decision": "rollout_triggered",
    "topic_counts": {
      "policy_data": 4,
      "incident_access": 4,
      "feature_flags": 2,
      "directory_sync": 2,
      "audit_logs": 2,
      "compliance_audit": 0
    },
    "actions_taken": "published 3 production-safe data update entries with emergency outage mitigations",
    "outage_resolved": true
  }
}
```

Additional verifier semantics for `opal/test2`:

- `decision` is accepted if it is one of:
  - `trigger_emergency_rollout`
  - `emergency_rollout_triggered`
  - `rollout_triggered`
- `actions_taken` may be either:
  - a non-empty string, or
  - a non-empty list of allowed action tokens

### Test 3 — `opal/test3`

Task prompt:

```text
This is a stateful OPAL outage-remediation benchmark. Benchmark auth is already configured.

A new recovery action is needed for the sev-1 payments outage, and no existing production module in the live bundle covers it. Create a new policy module that allows the on-call responders to run the replica promotion action under tightly scoped outage conditions.

Requirements:
- Create a new module at `incident/payments_replica_promote_hotfix.rego`.
- The module package must be `app.incident.payments_replica_promote_hotfix`.
- Include `default allow = false`.
- The policy should allow only when:
  - input.incident.severity == "sev-1"
  - input.actor.class == "oncall_responder"
  - input.flags.emergency_override == true
  - input.request.operation == "promote_replica"
- No existing module in the live bundle already grants this exact operation, so create a new module instead of overwriting an unrelated one.
- Use the tracked OPAL policy repo so clients receive the new policy.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `module_path`, `package_name`, `created_new_module`, and `outage_resolved`.
```

Extra system line:

```text
This is a new-policy-creation task. Create a tightly scoped new recovery policy module at `incident/payments_replica_promote_hotfix.rego` and do not overwrite unrelated production modules.
```

Live verification contract:

- client app: `opal-client-authz-a`
- package path: `app/incident/payments_replica_promote_hotfix/allow`
- baseline module is absent
- final positive input must become `true`
- negative control must remain `false`
- verifier explicitly triggers the client policy refresh path before checking convergence

Positive input:

```json
{
  "incident": {"severity": "sev-1"},
  "actor": {"class": "oncall_responder"},
  "flags": {"emergency_override": true},
  "request": {"operation": "promote_replica"}
}
```

Negative input:

```json
{
  "incident": {"severity": "sev-1"},
  "actor": {"class": "oncall_responder"},
  "flags": {"emergency_override": true},
  "request": {"operation": "delete_cluster"}
}
```

Current golden payload:

```json
{
  "result": {
    "module_path": "incident/payments_replica_promote_hotfix.rego",
    "package_name": "app.incident.payments_replica_promote_hotfix",
    "created_new_module": true,
    "outage_resolved": true
  }
}
```

## GoEx Suite

Shell entrypoint:

```bash
./scripts/run_opal_goex_tests.sh --cases "current test1 test3" anthropic haiku
```

Current runner behavior:

- default levels: `L0 L1 L2 L3 L4`
- default cases: `current test1 test3`
- `L0` runs `--execution-mode direct`
- `L1` through `L4` run `--execution-mode goex`
- each case calls benchmark reset before execution
- preflight is `GET /healthcheck` on the resolved API URL
- `OPAL_ADMIN_BASE_URL` or `--admin-base-url` overrides the base URL used to derive API, MCP, and codegen URLs
- per-job stats rows now include:
  - `phase1_passed`
  - `phase2_passed`
  - `goex_record_ids`
  - `goex_reversed_ok_count`

### GoEx System Prompts

`L0` direct baseline prompt:

```text
You are running the OPAL GoEx baseline at L0. Do not call code_extension and do not call any nonexistent apply_policy_hotfix endpoint. First call list_policy_modules. If the hotfix module already exists, call update_policy_module; otherwise call create_policy_module. Use module_path and commit_message from the task, and provide rego_content that implements the requested outage policy change.
```

`L1` system prompt:

```text
You are running an OPAL GoEx test at L1. Do not call apply_policy_hotfix because that endpoint does not exist. Call code_extension exactly once with prompt equal to the task, extension_point='policy_hotfix', execution_mode='goex', explicit Python code, and explicit reversal_code. The code must build the requested Rego source and assign result = upsert_policy_module(context['repo_path'], context['module_path'], rego_source, context['commit_message']). Add any extra fields directly onto that result dict instead of wrapping it under another key such as upsert_result. The reversal_code must delete the module if it was newly created, or restore context['current_rego'] with upsert_policy_module(...) if it already existed.
```

`L2` system prompt:

```text
You are running an OPAL GoEx test at L2. Use the existing code_extension tool, not any benchmark-specific hotfix endpoint. Call code_extension exactly once with extension_point='policy_hotfix' and execution_mode='goex'. Write a precise prompt telling the server to create or replace the requested hotfix module, return the dict from upsert_policy_module(...) as the final result, add any metadata directly onto that dict instead of nesting it, and generate real reversal logic that restores context['current_rego'] or deletes a newly created module.
```

`L3` system prompt:

```text
You are running an OPAL GoEx test at L3. Use the existing code_extension tool, not any benchmark-specific hotfix endpoint. Call code_extension exactly once with extension_point='policy_hotfix' and execution_mode='goex'. Use a detailed prompt that tells the server to use the provided policy bundle and current module context to generate the requested outage policy change and matching reversal logic. The final result should stay flat: return the upsert_policy_module dict directly, with any extra metadata added onto that dict.
```

`L4` system prompt:

```text
You are running an OPAL GoEx test at L4. Call code_extension with extension_point='policy_hotfix' and execution_mode='goex'. Ask it to create or replace the requested hotfix module and generate real reversal logic that restores the previous file or deletes the new one. Ask for the final result to stay flat: return the upsert_policy_module dict directly, with any metadata added onto that dict.
```

### GoEx Case: `current`

Task prompt:

```text
A sev-1 outage in the production payments cluster forced traffic onto a stale cache replica. Apply an emergency OPAL policy hotfix so the on-call responders can run the cache failover operation while the incident is active. Use GoEx mode for safety and auditability.

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

If the module already exists, replace it with the corrected hotfix. End with a short operator-facing summary.
```

Verification contract:

- exactly one GoEx record in GoEx mode
- hotfix snapshot must resolve to `incident/cache_failover_hotfix.rego`
- decision on `app/incident/cache_failover_hotfix/allow` must become `true`
- negative control must remain `false`
- reversal must clear the hotfix and restore the baseline state

### GoEx Case: `test1`

Task prompt:

```text
A sev-1 outage in the production payments cluster is still unresolved because the active authorization gate module at "incident/payments_outage_gate.rego" is too restrictive. Use GoEx mode to update the active module so on-call responders can perform the cache failover action, but only under the intended sev-1 break-glass conditions.

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

Read the existing tracked policy bundle and adapt the correct production break-glass semantics into the active module. Do not widen access beyond those outage conditions. End with a short operator-facing summary.
```

Verification contract:

- exactly one GoEx record in GoEx mode
- decision on `app/incident/payments_outage_gate/allow` must become `true`
- negative control must remain `false`
- reversal must restore the restrictive baseline module

### GoEx Case: `test3`

Task prompt:

```text
A sev-1 outage in the production payments cluster now requires a recovery action that no existing production policy covers: allowing on-call responders to run the replica promotion operation. Use GoEx mode to create a new emergency OPAL policy hotfix.

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

No existing module in the live bundle already grants this exact operation, so create a new policy module rather than overwriting an unrelated one. End with a short operator-facing summary.
```

Verification contract:

- exactly one GoEx record in GoEx mode
- decision on `app/incident/payments_replica_promote_hotfix/allow` must become `true`
- negative control must remain `false`
- reversal must delete the module and restore the baseline state

## Verifier And Unit-Test Notes

Current focused tests:

```bash
uv run python -m unittest \
  tests.test_opal_standard_harness \
  tests.test_opal_goex_harness \
  tests.test_opal_verify_run
```

What those tests cover:

- `tests/test_opal_standard_harness.py`
  - client policy refresh is triggered before live verification
  - failure diagnostics include server module state, client policy status, and client health
- `tests/test_opal_goex_harness.py`
  - nested GoEx hotfix results are flattened correctly
  - GoEx record IDs are extracted from export logs and text logs
  - benchmark non-empty-bundle validation skips code-extension records
- `tests/test_opal_verify_run.py`
  - `opal/test2` semantic verification accepts string `actions_taken`

Stats verification details:

- `scripts/opal/opal_verify_run.py` requires a strict fenced JSON final answer with the required top-level key.
- `opal/test2` is semantically verified instead of requiring a single literal decision/action string.
- Standard verification is live-state-first plus golden-payload verification, not rigid tool-order enforcement.
