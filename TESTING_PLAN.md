# OPAL Benchmark Testing Plan

This document is the source of truth for the benchmark prompts and scenario
contracts used by the OPAL example in this repo.

The standard suite is driven by
[`scripts/run_opal_tests.sh`](../../scripts/run_opal_tests.sh).
The GoEx suite is driven by [`agent_cli_goex.py`](agent_cli_goex.py) and
[`scripts/run_opal_goex_tests.sh`](../../scripts/run_opal_goex_tests.sh).
Shared scenario state lives in
[`packages/opal-server/opal_server/benchmark_scenarios.py`](packages/opal-server/opal_server/benchmark_scenarios.py).

## Benchmark Model

- The benchmark tests Symphony on top of a real OPAL deployment.
- Standard tests are stateful outage scenarios, not read-only extraction toys.
- The benchmark reset endpoint `POST /symphony/benchmark/reset` runs before
  every standard job and before every GoEx case.
- Standard-suite grading is live-state-first:
  - reset must restore the expected broken baseline
  - the agent must change OPAL state so the outage is resolved
  - negative controls must remain denied or absent
  - the final response must still end with one fenced JSON object
- The standard-suite JSON is still checked against fixtures under
  [`scripts/opal/expected/`](../../scripts/opal/expected/).
- GoEx remains record-first and reversal-first:
  - capture exactly one GoEx record
  - verify the live change
  - reverse it
  - confirm the baseline state is restored

## Reset Contract

`POST /symphony/benchmark/reset` restores all benchmark baselines:

- data paths used by the stats-driven rollout scenario
- `incident/payments_outage_gate.rego` restored to the restrictive baseline
- `incident/payments_replica_promote_hotfix.rego` removed
- `incident/cache_failover_hotfix.rego` removed

The baseline `incident/payments_outage_gate.rego` content is:

```rego
package app.incident.payments_outage_gate

default allow = false

# Baseline outage gate: only platform admins can force the action.
allow {
    input.actor.class == "platform_admin"
    input.request.operation == "cache_failover"
}
```

## Standard Suite

The standard suite runs `opal/test1`, `opal/test2`, and `opal/test3` across
`L0` through `L4`.

Shared system prompt prefix for all three tests:

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

Test 1 extra system prompt:

```text
This is an outage-policy-application task. Inspect the live bundle, identify the correct existing production break-glass logic, and apply that logic to `incident/payments_outage_gate.rego`.
```

Expected live behavior:

- baseline decision on `opal-client-authz-a` is `false`
- positive query must flip to `true`
- negative control must remain `false`

Positive decision input:

```json
{
  "incident": {"severity": "sev-1"},
  "actor": {"class": "oncall_responder"},
  "flags": {"emergency_override": true},
  "request": {"operation": "cache_failover"},
  "time": {"within_business_hours": false}
}
```

Negative control input:

```json
{
  "incident": {"severity": "sev-1"},
  "actor": {"class": "oncall_responder"},
  "flags": {"emergency_override": true},
  "request": {"operation": "cache_failover"},
  "time": {"within_business_hours": true}
}
```

Expected JSON fixture:

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

Test 2 extra system prompt:

```text
This is a stats-to-action task. Use OPAL statistics to decide whether the emergency rollout should happen, and if it should, publish only the production-safe entries needed to resolve the outage.
```

Expected live behavior:

- `/incident/access` appears on `opal-client-authz-a`
- `/directory/emergency/groups` appears on `opal-client-directory-a`
- `/feature_flags/cache_failover` appears on `opal-client-web-a`
- invalid, staging, or forbidden rollout artifacts are absent

Expected JSON fixture:

```json
{
  "summary": {
    "decision": "trigger_emergency_rollout",
    "topic_counts": {
      "policy_data": 4,
      "incident_access": 4,
      "feature_flags": 2,
      "directory_sync": 2,
      "audit_logs": 2,
      "compliance_audit": 0
    },
    "actions_taken": [
      "published_incident_access",
      "published_directory_sync",
      "published_feature_flags"
    ],
    "outage_resolved": true
  }
}
```

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

Test 3 extra system prompt:

```text
This is a new-policy-creation task. Create a tightly scoped new recovery policy module at `incident/payments_replica_promote_hotfix.rego` and do not overwrite unrelated production modules.
```

Expected live behavior:

- baseline module is absent
- positive decision flips from missing or `false` to `true`
- negative control remains `false`

Positive decision input:

```json
{
  "incident": {"severity": "sev-1"},
  "actor": {"class": "oncall_responder"},
  "flags": {"emergency_override": true},
  "request": {"operation": "promote_replica"}
}
```

Negative control input:

```json
{
  "incident": {"severity": "sev-1"},
  "actor": {"class": "oncall_responder"},
  "flags": {"emergency_override": true},
  "request": {"operation": "delete_cluster"}
}
```

Expected JSON fixture:

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

The GoEx suite now runs multiple stateful mutation cases across `L0` to `L4`.

- `current`: existing cache failover hotfix scenario
- `test1`: update the active outage gate module through GoEx
- `test3`: create the new replica-promote policy through GoEx

Shell entrypoint:

```bash
./scripts/run_opal_goex_tests.sh --cases "current test1 test3" anthropic haiku
```

Harness options:

- `L0` uses `--execution-mode direct`
- `L1` to `L4` use `--execution-mode goex`
- every case resets benchmark state before execution

### GoEx System Prompts

Direct baseline prompt for `L0`:

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

Verification:

- valid GoEx record exists with reversal code
- module content matches `incident/cache_failover_hotfix.rego`
- client receives the policy
- reversal removes it again

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

Verification:

- exactly one GoEx record
- positive decision on `app/incident/payments_outage_gate/allow` becomes `true`
- negative control remains `false`
- reversal restores the restrictive baseline

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

Verification:

- exactly one GoEx record
- new module appears
- positive decision on `app/incident/payments_replica_promote_hotfix/allow` becomes `true`
- negative control remains `false`
- reversal removes the module and restores the baseline
