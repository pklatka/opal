# OPAL Benchmark Testing Plan

This document mirrors the current OPAL benchmark harness in this repo.

Primary sources:

- Standard runner: [`scripts/run_opal_tests.sh`](../../scripts/run_opal_tests.sh)
- GoEx runner: [`scripts/run_opal_goex_tests.sh`](../../scripts/run_opal_goex_tests.sh)
- GoEx harness: [`examples/opal/agent_cli_goex.py`](agent_cli_goex.py)
- Shared scenario metadata: [`examples/opal/packages/opal-server/opal_server/benchmark_scenarios.py`](packages/opal-server/opal_server/benchmark_scenarios.py)
- Live verifier: [`scripts/opal/verify_live_state.py`](../../scripts/opal/verify_live_state.py)
- Stats verifier: [`scripts/opal/opal_verify_run.py`](../../scripts/opal/opal_verify_run.py)
- Goldens: [`scripts/opal/expected/`](../../scripts/opal/expected/)

If this file drifts from those files, the code wins.

## Stack And Env

The benchmark assumes the Kubernetes-backed OPAL stack started by:

```bash
./scripts/start_opal.sh
source /tmp/symphony-opal-symphony-opal.env
```

Important runtime assumptions:

- `OPAL_BASE_URL` points at the public OPAL service and is used to derive MCP/codegen URLs unless explicitly overridden.
- `OPAL_ADMIN_BASE_URL` points at the admin service and is used for benchmark reset/live verification when set.
- `OPAL_NAMESPACE` is required for live state verification in the standard suite.
- `OPAL_KUBE_CONTEXT` is optional but used when set.
- `/symphony/benchmark/info` exposes stack drift/debug metadata and is logged by the standard runner when available.

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

Reset also seeds the benchmark-only noisy policy corpus and benchmark candidate feed:

- policy corpus size reported in suite stats: `80` for `opal/test1` and `opal/test3`
- benchmark candidate count reported for `opal/test2`: `72`
- valid candidate ids for `opal/test2`: `cand_02`, `cand_06`, `cand_11`

Reset publishes the OPAL webhook topic so policy watchers and clients converge to the restored baseline.

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
- The stats verifier enforces strict fenced final JSON, golden payload checks, and extension-usage requirements for `L1` through `L4`.
- `L1` through `L3` must trigger at least one extension event.
- `L4` must call only `code_extension`; ordinary endpoint/MCP tools are not valid at L4.
- Standard suite rows are enriched with a per-run `suite_run_id`, same-run `delta_*_vs_l0` fields, and suite-level ranking metadata.

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

The production payments outage is still unresolved because the active authorization gate at `incident/payments_outage_gate.rego` blocks the on-call responders from running the recovery action. Inspect the live policy bundle, which now contains several near-match production and non-production break-glass modules. Find the single existing production policy whose semantics should govern this outage gate, and update the active module so that the outage is resolved without widening access beyond the intended sev-1 break-glass conditions.

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
- The corpus includes several near matches; only one existing production donor is correct.
- Do not leave the active module overly permissive.
- Use the tracked OPAL policy repo so clients receive the change.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `source_policy_path`, `target_module_path`, `package_name`, and `outage_resolved`.
```

Base extra system line:

```text
This is an outage-policy-application task. Keep any policy change narrowly scoped to the requested production outage conditions and preserve the required final JSON contract.
```

Level-specific extension additions:

- `L1` to `L4`: `Benchmark rule: extension levels must include a relevant Symphony extension interaction while working with the policy-bundle context.`
- `L1` to `L4`: `Verification rule: a passing non-L0 run must show at least one valid Symphony extension interaction before the final state change. Choose an appropriate extension-capable tool from the tool list, and keep extension output compact.`
- `L4` only: `At L4, the only valid tool surface is code_extension; solve the policy task through that endpoint and its sandbox capabilities.`

Live verification contract:

- client app: `opal-client-authz-a`
- package path: `app/incident/payments_outage_gate/allow`
- baseline positive input must be `false`
- final positive input must be `true`
- negative control must remain `false`

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

An SRE is deciding whether to trigger the emergency cache-failover rollout. First inspect the live control-plane statistics for topics ["policy_data", "incident_access", "feature_flags", "directory_sync", "audit_logs", "compliance_audit"]. Use the normalized `benchmark_stats` view to compute topic counts from `client_topics`; do not rely on pre-aggregated subscriber lists. Then fetch the benchmark candidate feed for `opal/test2` and select only the production-safe entries that satisfy the rollout.

Decision rule:
- If `incident_access` has at least 4 subscribers and `feature_flags` has at least 2 subscribers, trigger the emergency rollout now.
- Otherwise, do not publish any emergency rollout.

If the rollout is triggered:
- Fetch the candidate entries from the benchmark candidate feed for `opal/test2`.
- Publish only the valid production-safe subset with one-time callback `https://ops.internal/v1/opal/update-report`.
- The correct selected candidate ids are hidden among invalid distractors.

Any published entries must remain production-safe:
- allow only topics `incident_access`, `directory_sync`, and `feature_flags`
- reject staging hosts
- reject invalid URLs
- match the required destination paths
- set `save_method` to `PUT`
- preserve the callback

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `summary` object with keys `decision`, `topic_counts`, `selected_candidate_ids`, and `outage_resolved`.
```

Base extra system line:

```text
This is a stats-to-action task. Base the rollout decision and any published entries on the live benchmark data and the required production-safety constraints, then preserve the required final JSON contract.
```

Level-specific extension additions:

- `L1` to `L4`: `Benchmark rule: extension levels must include a relevant Symphony extension interaction while working with the statistics or candidate data.`
- `L1` to `L4`: `Verification rule: a passing non-L0 run must show at least one valid Symphony extension interaction before the final state change. Choose an appropriate extension-capable tool from the tool list, and keep extension output compact.`
- `L4` only: `At L4, the only valid tool surface is code_extension; solve the statistics and candidate-data task through that endpoint and its sandbox capabilities.`

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
    "selected_candidate_ids": [
      "cand_02",
      "cand_06",
      "cand_11"
    ],
    "outage_resolved": true
  }
}
```

Additional verifier semantics for `opal/test2`:

- `decision` is accepted if it is one of:
  - `trigger_emergency_rollout`
  - `emergency_rollout_triggered`
  - `rollout_triggered`
  - `trigger`
  - `triggered`
- `selected_candidate_ids` must be a list matching the expected safe subset.

### Test 3 — `opal/test3`

Task prompt:

```text
This is a stateful OPAL outage-remediation benchmark. Benchmark auth is already configured.

A new recovery action is needed for the sev-1 payments outage. Inspect the live policy bundle, which now contains several near-match replica-promotion and unrelated production modules, and confirm that no existing module exactly satisfies the intended sev-1 break-glass semantics. Then create a new policy module that allows the on-call responders to run the replica promotion action under tightly scoped outage conditions.

Requirements:
- Create a new module at `incident/payments_replica_promote_hotfix.rego`.
- The module package must be `app.incident.payments_replica_promote_hotfix`.
- Include `default allow = false`.
- The policy should allow only when:
  - input.incident.severity == "sev-1"
  - input.actor.class == "oncall_responder"
  - input.flags.emergency_override == true
  - input.request.operation == "promote_replica"
- The bundle contains several near matches, but none satisfies the exact intended semantics.
- Report the rejected near-match module paths you inspected.
- Use the tracked OPAL policy repo so clients receive the new policy.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `module_path`, `package_name`, `created_new_module`, `rejected_candidate_paths`, and `outage_resolved`.
```

Base extra system line:

```text
This is a new-policy-creation task. Keep any new policy narrowly scoped to the requested production outage conditions, avoid modifying unrelated modules, and preserve the required final JSON contract.
```

Level-specific extension additions:

- `L1` to `L4`: `Benchmark rule: extension levels must include a relevant Symphony extension interaction while assessing the policy-bundle context for the new policy.`
- `L1` to `L4`: `Verification rule: a passing non-L0 run must show at least one valid Symphony extension interaction before the final state change. Choose an appropriate extension-capable tool from the tool list, and keep extension output compact.`
- `L4` only: `At L4, the only valid tool surface is code_extension; solve the policy-bundle assessment and mutation task through that endpoint and its sandbox capabilities.`

Live verification contract:

- client app: `opal-client-authz-a`
- package path: `app/incident/payments_replica_promote_hotfix/allow`
- baseline module is absent
- final positive input must become `true`
- negative control must remain `false`

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
    "rejected_candidate_paths": [
      "incident/payments_replica_promote_admin.rego",
      "incident/payments_replica_promote_no_severity.rego",
      "incident/payments_replica_delete_cluster.rego",
      "incident/payments_replica_promote_business_hours.rego",
      "incident/payments_replica_promote_no_override.rego",
      "incident/break_glass_oncall_promote_replica.rego"
    ],
    "outage_resolved": true
  }
}
```

Additional verifier semantics for `opal/test3`:

- `rejected_candidate_paths` must be a list.
- It may contain extra paths, but it must include the six expected near matches above.

### Standard Suite Stats

Per-job rows are written to `stats/opal/opal_<provider>_<model>__server_<provider>_<model>.jsonl` and enriched with:

- `suite_run_id`
- `passed`
- `prompt_tokens`, `completion_tokens`, `cost_usd` normalization
- same-run `delta_total_tokens`, `delta_prompt_tokens`, `delta_completion_tokens`, `delta_latency_ms`, `delta_cost_usd`
- `total_tokens_delta_vs_l0`, `prompt_tokens_delta_vs_l0`, `completion_tokens_delta_vs_l0`, `latency_ms_delta_vs_l0`, `cost_usd_delta_vs_l0`
- `benchmark_policy_module_count`
- `benchmark_candidate_count`
- `benchmark_selected_candidate_count`
- `extension_used`

Suite summaries are written to `stats/opal/suite_<provider>_<model>__server_<provider>_<model>.jsonl` and include:

- `suite_run_id`
- `level`
- `pass_count`
- `passing_total_tokens`
- `passing_latency_ms`
- `benchmark_policy_module_count`
- `benchmark_candidate_count`
- `benchmark_selected_candidate_count`
- `extension_used_count`
- `ranking_key`

Current ranking key:

1. `pass_count` descending
2. `passing_total_tokens` ascending
3. `passing_latency_ms` ascending

## GoEx Suite

Shell entrypoint:

```bash
./scripts/run_opal_goex_tests.sh --cases "current test1 test3" anthropic haiku
```

Current runner behavior:

- default levels: `L1 L2 L3 L4`
- default cases: `current test1 test3`
- `L0` runs `--execution-mode direct`
- `L1` through `L4` run `--execution-mode goex`
- each case calls benchmark reset before execution
- preflight is `GET /healthcheck` on the resolved API URL
- `OPAL_ADMIN_BASE_URL` or `--admin-base-url` overrides the control-plane API URL for reset/live verification, but MCP and codegen still derive from the public base URL unless explicitly overridden
- per-job harness stats rows include:
  - `phase1_passed`
  - `phase2_passed`
  - `goex_record_ids`
  - `goex_reversed_ok_count`
- full token usage for GoEx runs is stored in `logs/opal-goex/.../opal_goex.export.jsonl`, not in the harness `.stats.jsonl` rows

### GoEx Model Flow

This is the model-facing flow for each `L1` through `L4` OPAL GoEx case.

1. The harness resets OPAL to the benchmark baseline and sends the selected system prompt plus one case task prompt to the model.
2. At `L1` through `L3`, the model may use read-only OPAL/MCP tools to inspect existing modules or bundles for context. At `L4`, inspection must happen inside `code_extension` through exposed capabilities.
3. The actual reversible policy mutation is expected to go through the existing `create_policy_module` or `update_policy_module` endpoint with extension/goex parameters for `L1` through `L3`, and through `code_extension` for `L4`; it must not use bundle post-processing or any separate hotfix endpoint for the GoEx mutation.
4. Inside that hotfix extension, the model should use the extension point's published capability metadata to decide which policy-hotfix helpers are available and how to call them.
5. The forward GoEx execution leaves the requested policy change applied and records an execution result with enough metadata for validation and reversal. The grader looks for `module_path`, `package_name`, `action`, `rego_content`, `previous_rego`, `module_exists_before`, and `repo_path` when available.
6. The model's final assistant message is separate from the GoEx execution result. It must end with exactly one fenced JSON block containing a top-level `result` object for the benchmark grader.
7. After the final answer, the harness verifies the live authorization decision, runs the GoEx reversal, and verifies that the baseline state was restored.

To see what the model actually did in a run:

- Per-case transcript: `logs/opal-goex/<model>/<case>/<level>/opal_goex.log`
- Combined transcript: `logs/opal-goex/<model>/opal_goex.log`
- Raw exported run stats: `logs/opal-goex/<model>/<case>/<level>/opal_goex.export.jsonl`
- Suite result rows: `stats/opal/goex/suite_<provider>_<model>.jsonl`

In the transcript, look for `[Tool]` blocks to see each tool call, `[Sandbox Code]` blocks to see extension code, `[GoEx Mode]` blocks to see recorded GoEx IDs, and `[Reversal Code]` blocks to see reversal logic. The harness also prints the final verification and reversal outcome near the end of each case log.

### GoEx System Prompts

For each GoEx job, the model receives one system prompt from this section plus one task prompt from the selected GoEx case below. The final fenced JSON contract lives in the task prompt, while the GoEx execution-result contract is described in both the system prompt and the task prompt.

`L0` direct baseline prompt:

```text
You are running the OPAL GoEx baseline at L0. Do not call code_extension and do not use extension/goex parameters on policy module endpoints. Use list_policy_modules if needed to determine whether the hotfix module already exists. If it already exists, call update_policy_module; otherwise call create_policy_module. Use module_path and commit_message from the task, and provide rego_content that implements the requested outage policy change. Follow the task's grading contract exactly and end with exactly one fenced JSON block containing the requested top-level result object.
```

`L1` system prompt:

```text
You are running an OPAL GoEx round-trip test at L1. For each required mutation, use GoEx-enabled extension execution at this level with valid forward execution logic and valid reversal logic. Do not rely on direct baseline execution. In OPAL, the reversible mutation should use the existing `create_policy_module` or `update_policy_module` endpoint with `extension_level="L1"`, extension_code, and goex parameters rather than a read-only inspection step, a bundle post-processing extension, or a separate hotfix endpoint. Extension code for this hotfix should rely on the policy-hotfix capabilities exposed for that endpoint; ordinary MCP/request tools are not available inside that extension code. Use the extension capability metadata to choose the appropriate helper calls. The forward path should apply exactly the requested mutation, leave it applied, and return structured identifiers and hotfix metadata for the created object, including the final Rego source as `rego_content`. The reversal path should read the execution result, undo exactly what was created, and report its outcome.
```

`L2` system prompt:

```text
You are running an OPAL GoEx round-trip test at L2. For each required mutation, use GoEx-enabled execution at this level and request extension behavior with reversal support. Do not rely on direct baseline execution. In OPAL, the reversible mutation should use the existing `create_policy_module` or `update_policy_module` endpoint with `extension_level="L2"`, task_description, and goex parameters rather than a read-only inspection step, a bundle post-processing extension, or a separate hotfix endpoint. Extension code for this hotfix should rely on the policy-hotfix capabilities exposed for that endpoint; ordinary MCP/request tools are not available inside that extension code. Use the extension capability metadata to choose the appropriate helper calls. Apply exactly the requested mutation once per target object, return structured identifiers for created objects, and ensure the reversal path can undo them from the execution result. Include the final Rego source as `rego_content` in that result.
```

`L3` system prompt:

```text
You are running an OPAL GoEx round-trip test at L3. For each required mutation, use GoEx-enabled extension execution at this level with source-aware reversible behavior. In OPAL, the reversible mutation should use the source-aware existing `create_policy_module` or `update_policy_module` endpoint with `extension_level="L3"`, task_description, and goex parameters rather than a read-only inspection step, a bundle post-processing extension, or a separate hotfix endpoint. Extension code for this hotfix should rely on the policy-hotfix capabilities exposed for that endpoint; ordinary MCP/request tools are not available inside that extension code. Use the extension capability metadata to choose the appropriate helper calls. Apply exactly one mutation per requested target object. Do not perform exploratory, duplicate, or debugging mutations. Return structured identifiers for what was created so reversal can undo exactly those changes from the execution result, including the final Rego source as `rego_content`.
```

`L4` system prompt:

```text
You are running an OPAL GoEx round-trip test at L4. Use GoEx-enabled reversible extension execution at this level. The forward path should perform only the requested mutations, leave them applied, and assign the final structured payload to the expected result variable. In OPAL, the reversible mutation should use the `code_extension` hotfix path rather than a read-only inspection step, a bundle post-processing extension, or raw policy CRUD fallback. Extension code for this hotfix should rely on the policy-hotfix capabilities exposed for that extension point; ordinary MCP/request tools are not available inside that extension code. Use the extension capability metadata to choose the appropriate helper calls. Do not perform exploratory or debugging mutations. The reversal path must undo every created object using identifiers from the execution result. Ensure execution results expose stable identifiers for all created objects, including the final Rego source as `rego_content`.
```

### GoEx Case: `current`

Task prompt:

```text
A sev-1 outage in the production payments cluster forced traffic onto a stale cache replica. Apply an emergency OPAL policy hotfix so the on-call responders can run the cache failover operation while the incident is active. Use GoEx mode for safety and auditability.

Apply the change through the existing policy-module endpoint for L1-L3 (`create_policy_module` or `update_policy_module` with extension/goex parameters), and through `code_extension` for L4. The forward mutation must remain applied first so GoEx reversal can undo it later. If you inspect modules or bundles for context, keep those steps read-only; the reversible GoEx mutation itself must not go through bundle post-processing or a separate hotfix endpoint.

In the hotfix extension code, use the policy-hotfix capabilities exposed for that extension point; ordinary OPAL request/MCP tools are outside that sandbox. Use the extension capability metadata to choose the appropriate helper calls. The GoEx execution result should include the hotfix metadata needed for grading and reversal: `module_path`, `package_name`, `action`, `rego_content`, `previous_rego`, `module_exists_before`, and `repo_path` when available. This execution result is separate from the final fenced JSON answer.

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

If the module already exists, replace it with the corrected hotfix.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `module_path`, `package_name`, `action`, and `outage_resolved`. Set `module_path` to `incident/cache_failover_hotfix.rego`, `package_name` to `app.incident.cache_failover_hotfix`, `action` to either `created` or `updated`, and `outage_resolved` to `true`.
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

Apply the change through the existing policy-module endpoint for L1-L3 (`create_policy_module` or `update_policy_module` with extension/goex parameters), and through `code_extension` for L4. The forward mutation must remain applied first so GoEx reversal can undo it later. If you inspect modules or bundles for context, keep those steps read-only; the reversible GoEx mutation itself must not go through bundle post-processing or a separate hotfix endpoint.

In the hotfix extension code, use the policy-hotfix capabilities exposed for that extension point; ordinary OPAL request/MCP tools are outside that sandbox. Use the extension capability metadata to choose the appropriate helper calls. The GoEx execution result should include the hotfix metadata needed for grading and reversal: `module_path`, `package_name`, `action`, `rego_content`, `previous_rego`, `module_exists_before`, and `repo_path` when available. This execution result is separate from the final fenced JSON answer.

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

Read the existing tracked policy bundle and adapt the correct production break-glass semantics into the active module. Do not widen access beyond those outage conditions.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `module_path`, `package_name`, `action`, and `outage_resolved`. Set `module_path` to `incident/payments_outage_gate.rego`, `package_name` to `app.incident.payments_outage_gate`, `action` to `updated`, and `outage_resolved` to `true`.
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

Apply the change through the existing policy-module endpoint for L1-L3 (`create_policy_module` or `update_policy_module` with extension/goex parameters), and through `code_extension` for L4. The forward mutation must remain applied first so GoEx reversal can undo it later. If you inspect modules or bundles for context, keep those steps read-only; the reversible GoEx mutation itself must not go through bundle post-processing or a separate hotfix endpoint.

In the hotfix extension code, use the policy-hotfix capabilities exposed for that extension point; ordinary OPAL request/MCP tools are outside that sandbox. Use the extension capability metadata to choose the appropriate helper calls. The GoEx execution result should include the hotfix metadata needed for grading and reversal: `module_path`, `package_name`, `action`, `rego_content`, `previous_rego`, `module_exists_before`, and `repo_path` when available. This execution result is separate from the final fenced JSON answer.

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

No existing module in the live bundle already grants this exact operation, so create a new policy module rather than overwriting an unrelated one.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `module_path`, `package_name`, `action`, and `outage_resolved`. Set `module_path` to `incident/payments_replica_promote_hotfix.rego`, `package_name` to `app.incident.payments_replica_promote_hotfix`, `action` to `created`, and `outage_resolved` to `true`.
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
  tests.test_opal_benchmark_token_paths \
  tests.test_opal_standard_harness \
  tests.test_opal_goex_harness \
  tests.test_opal_verify_run
```

What those tests cover:

- `tests/test_opal_benchmark_token_paths.py`
  - benchmark candidate feed has `72` entries
  - valid candidate ids remain `cand_02`, `cand_06`, `cand_11`
  - benchmark policy corpus size remains large enough for the noisy benchmark path
  - benchmark exact-path policy fast path accepts exact `.rego` reads and rejects incompatible calls
- `tests/test_opal_standard_harness.py`
  - client policy refresh is triggered before live verification
  - failure diagnostics include server module state, client policy status, and client health
- `tests/test_opal_goex_harness.py`
  - nested GoEx hotfix results are flattened correctly
  - GoEx record IDs are extracted from export logs and text logs
  - benchmark non-empty-bundle validation skips code-extension records
- `tests/test_opal_verify_run.py`
  - `opal/test2` semantic verification accepts the expected candidate subset
  - `opal/test3` semantic verification accepts the six required rejected paths
  - extension-usage enforcement behaves correctly for `L1` through `L4`

Stats verification details:

- `scripts/opal/opal_verify_run.py` requires a strict fenced JSON final answer with the required top-level key.
- `opal/test2` is semantically verified against `topic_counts`, accepted `decision` variants, and `selected_candidate_ids`.
- `opal/test3` is semantically verified against the required rejected-path set.
- `L1` to `L3` fail verification if no extension is triggered.
- `L4` fails verification if it completes with only raw tool calls and no extension activity.
