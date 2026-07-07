# OPAL Benchmark Testing Plan

This document mirrors the current OPAL benchmark harness in this repo.

Primary sources:

- Standard runner: [`scripts/run_opal_tests.sh`](../../scripts/run_opal_tests.sh)
- RXR runner: [`scripts/run_opal_rxr_tests.sh`](../../scripts/run_opal_rxr_tests.sh)
- RXR harness: [`examples/opal/agent_cli_rxr.py`](agent_cli_rxr.py)
- Shared scenario metadata: [`examples/opal/packages/opal-server/opal_server/benchmark_scenarios.py`](packages/opal-server/opal_server/benchmark_scenarios.py)
- Live verifier: [`scripts/opal/verify_live_state.py`](../../scripts/opal/verify_live_state.py)
- Stats verifier: [`scripts/opal/opal_verify_run.py`](../../scripts/opal/opal_verify_run.py)
- Goldens: [`scripts/opal/expected/`](../../scripts/opal/expected/)

If this file drifts from those files, the code wins.

## Stack And Env

The benchmark assumes the Kubernetes-backed OPAL stack started by:

```bash
./scripts/start_opal.sh
source /tmp/cage-opal-cage-opal.env
```

Important runtime assumptions:

- `OPAL_BASE_URL` points at the public OPAL service and is used to derive MCP/codegen URLs unless explicitly overridden.
- `OPAL_ADMIN_BASE_URL` points at the admin service and is used for benchmark reset/live verification when set.
- `OPAL_NAMESPACE` is required for live state verification in the standard suite.
- `OPAL_KUBE_CONTEXT` is optional but used when set.
- `/cage/benchmark/info` exposes stack drift/debug metadata and is logged by the standard runner when available.

## Reset Contract

`POST /cage/benchmark/reset` is the benchmark baseline restore endpoint. It is called before every standard job when `OPAL_BENCHMARK_RESET=1`, and before every RXR case.

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

Focused runs:

```bash
./scripts/run_opal_tests.sh --level L4 --case test2 llmgateway "xai/grok-4-1-fast-reasoning"
./scripts/run_opal_tests.sh --level L4 --cases test1,test3 llmgateway "xai/grok-4-1-fast-reasoning"
```

Current runner behavior:

- Runs `opal/test1`, `opal/test2`, `opal/test3`.
- `--case`/`--cases` or `OPAL_CASES` can restrict the standard suite to `test1`, `test2`, `test3`, or a comma-separated subset.
- Default levels are `L0` through `L4`.
- Prompts in this section must stay in sync with the prompt literals in [`scripts/run_opal_tests.sh`](../../scripts/run_opal_tests.sh).
- Before agent execution, the runner asserts that the task still contains the fenced-JSON grading contract.
- If `OPAL_NAMESPACE` is set, the runner verifies live baseline state before the job and live final state after the job.
- The stats verifier enforces strict fenced final JSON, golden payload checks, and extension-usage requirements for `L1` through `L4`.
- `L1` through `L3` must trigger at least one extension event.
- `L4` must call only `code_extension`; ordinary endpoint/MCP tools are not valid at L4.
- At `L4`, the model-facing prompt asks for one comprehensive `code_extension` script that performs data fetching, decision logic, and mutation through exposed capabilities.
- Standard suite rows are enriched with a per-run `suite_run_id`, same-run `delta_*_vs_l0` fields, and suite-level ranking metadata.

Prompts in this section are shown with the grading suffix as a separate
paragraph for readability. In the runner, the suffix is appended to each task
literal with a leading space after the final task bullet.

Shared system prompt prefix:

```text
You are solving an OPAL benchmark task. Use the available OPAL tools and return only the requested fenced JSON.
```

### Test 1 — `opal/test1`

Task prompt:

```text
The production payments outage is still unresolved because `incident/payments_outage_gate.rego` is too restrictive. Inspect the live policy bundle, find the single existing production policy whose semantics should govern this outage gate, and update `incident/payments_outage_gate.rego` so the outage is resolved without widening access beyond the intended sev-1 break-glass conditions.

Requirements:
- Keep module path `incident/payments_outage_gate.rego`.
- Keep package `app.incident.payments_outage_gate`.
- The correct donor policy already exists in the tracked production bundle.
- The final behavior must allow only when:
  - input.incident.severity == "sev-1"
  - input.actor.class == "oncall_responder"
  - input.flags.emergency_override == true
  - input.request.operation == "cache_failover"
  - not input.time.within_business_hours
- Inspect the near-match bundle modules and choose the single correct production donor.
- Do not leave the active module overly permissive.
- Use the tracked OPAL policy repo so clients receive the change.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `source_policy_path`, `target_module_path`, `package_name`, and `outage_resolved`.
```

Base extra system line:

```text
Keep any policy change narrowly scoped to the requested outage conditions.
```

Level-specific extension additions:

- `L1` to `L4`: `Use at least one relevant CAGE extension interaction while solving this task. A passing non-L0 run must show that extension interaction before the final state change.`
- `L4` only: `At L4, the only valid tool surface is code_extension. Perform all required data fetching, logic, and mutations inside a single comprehensive code_extension script.`

Recent harness note:

- The active package must remain `app.incident.payments_outage_gate` even when extension code adapts the donor Rego from `incident/break_glass.rego`.
- L4 code should use the existing policy-hotfix capability surface inside `code_extension`; no separate hotfix endpoint exists.

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
Inspect the live control-plane statistics for topics ["policy_data", "incident_access", "feature_flags", "directory_sync", "audit_logs", "compliance_audit"]. Compute topic counts from the normalized `benchmark_stats` view using `client_topics`, not pre-aggregated subscriber lists. Then decide whether to trigger the emergency cache-failover rollout.

Decision rule:
- If `incident_access` has at least 4 subscribers and `feature_flags` has at least 2 subscribers, trigger the emergency rollout now.
- Otherwise, do not publish any emergency rollout.

If the rollout is triggered:
- Fetch the benchmark candidate feed for `opal/test2`.
- Publish only the valid production-safe subset with one-time callback `https://ops.internal/v1/opal/update-report`.
- Explicitly pass the callback parameter to the publisher.
- Treat publisher status `ok` as success.
- Use the candidate `valid` and `reason` fields together with the stated safety constraints.

Any published entries must remain production-safe:
- allow only topics `incident_access`, `directory_sync`, and `feature_flags`
- reject staging hosts
- reject invalid URLs
- match the required destination paths
- do not invent synthetic destination-path prefixes such as `/data/`
- set `save_method` to `PUT`
- preserve the callback

Set `outage_resolved` to `true` when the rollout is triggered and the selected production-safe entries are successfully published.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `summary` object with strictly typed values: `decision` ("rollout_triggered" or "rollout_rejected"), `topic_counts`, `selected_candidate_ids`, and `outage_resolved`.
```

Base extra system line:

```text
Base the rollout decision and any published entries on the live benchmark data and stated production-safety constraints.
```

Level-specific extension additions:

- `L1` to `L4`: `Use at least one relevant CAGE extension interaction while solving this task. A passing non-L0 run must show that extension interaction before the final state change.`
- `L4` only: `At L4, the only valid tool surface is code_extension. Perform all required data fetching, logic, and mutations inside a single comprehensive code_extension script.`

Recent harness note:

- The publisher returns `status: "ok"` on successful publication.
- Candidate/feed helpers may normalize common reporting aliases such as `id`, `topic`, and `destination_path`, but prompts should still prefer canonical candidate fields: `candidate_id`, `topics`, `dst_path`, `url`, and `save_method`.

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
A new recovery action is needed for the sev-1 payments outage. Inspect the live policy bundle, confirm that no existing module exactly satisfies the intended sev-1 break-glass semantics for replica promotion, and create a new policy module for that action.

Requirements:
- Create `incident/payments_replica_promote_hotfix.rego`.
- Use package `app.incident.payments_replica_promote_hotfix`.
- Include `default allow = false`.
- The policy must allow only when:
  - input.incident.severity == "sev-1"
  - input.actor.class == "oncall_responder"
  - input.flags.emergency_override == true
  - input.request.operation == "promote_replica"
- The bundle contains several near matches, but none satisfies the exact intended semantics.
- Report only the near-match module paths you actually inspected and rejected based on their functional conditions.
- Use the tracked OPAL policy repo so clients receive the new policy.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `module_path`, `package_name`, `created_new_module`, `rejected_candidate_paths`, and `outage_resolved`.
```

Base extra system line:

```text
Keep any new policy narrowly scoped to the requested outage conditions and avoid modifying unrelated modules.
```

Level-specific extension additions:

- `L1` to `L4`: `Use at least one relevant CAGE extension interaction while solving this task. A passing non-L0 run must show that extension interaction before the final state change.`
- `L4` only: `At L4, the only valid tool surface is code_extension. Perform all required data fetching, logic, and mutations inside a single comprehensive code_extension script.`

Recent harness note:

- Policy creation/update extension code may use the existing policy-hotfix capabilities inside `code_extension`; this remains one endpoint call at L4.

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

## RXR Suite

Shell entrypoint:

```bash
./scripts/run_opal_rxr_tests.sh --cases "test2 test1 test3" anthropic haiku
```

Current runner behavior:

- default levels: `L1 L2 L3 L4`
- default cases: `test2 test1 test3`
- `L0` runs `--execution-mode direct`
- `L1` through `L4` run `--execution-mode rxr`
- each case calls benchmark reset before execution
- preflight is `GET /healthcheck` on the resolved API URL
- `OPAL_ADMIN_BASE_URL` or `--admin-base-url` overrides the control-plane API URL for reset/live verification, but MCP and codegen still derive from the public base URL unless explicitly overridden
- per-job harness stats rows include:
  - `phase1_passed`
  - `phase2_passed`
  - `state_changed_ok`
  - `round_trip_ok`
  - `rxr_record_ids`
  - `rxr_reversed_ok_count`
- full token usage for RXR runs is stored in `logs/opal-rxr/.../opal_rxr.export.jsonl`, not in the harness `.stats.jsonl` rows

### RXR Model Flow

This is the model-facing flow for each `L1` through `L4` OPAL RXR case.

1. The harness resets OPAL to the benchmark baseline and sends the selected system prompt plus one case task prompt to the model.
2. Before the agent runs, the harness snapshots normalized OPAL `/policy` state. This includes policy modules and data modules normalized for order-insensitive comparison.
3. At `L1` through `L3`, the reversible mutation is expected to go through `create_policy_module` or `update_policy_module` with RXR enabled. At `L4`, the reversible mutation is expected to go through `code_extension`.
4. The forward RXR execution should apply only the requested policy mutation, leave it applied, and return stable hotfix details for the changed module, including `module_path`, `package_name`, `action`, and `rego_content`.
5. The model's final assistant message is separate from the RXR execution result. It must end with exactly one fenced JSON block containing a top-level `result` object for the benchmark grader.
6. After the final answer, the harness verifies that the live OPAL policy state changed, verifies the expected authorization decision, checks RXR record coverage, reverses all captured RXR records in reverse creation order, and requires the final OPAL policy state to match the pre-task snapshot.

To see what the model actually did in a run:

- Per-case transcript: `logs/opal-rxr/<model>/<case>/<level>/opal_rxr.log`
- Combined transcript: `logs/opal-rxr/<model>/opal_rxr.log`
- Raw exported run stats: `logs/opal-rxr/<model>/<case>/<level>/opal_rxr.export.jsonl`
- Suite result rows: `stats/opal/rxr/suite_<provider>_<model>.jsonl`

In the transcript, look for `[Tool]` blocks to see each tool call, `[Sandbox Code]` blocks to see extension code, `[RXR Mode]` blocks to see recorded RXR IDs, and `[Reversal Code]` blocks to see reversal logic. The harness also prints the final verification and reversal outcome near the end of each case log.

### RXR System Prompts

For each RXR job, the model receives one system prompt from this section plus one task prompt from the selected RXR case below. The prompts are intentionally benchmark-shaped and closer to the DeathStar RXR prompts: the task describes the policy mutation and final JSON contract, while the system prompt describes the level-specific reversible-execution expectation.

`L0` direct baseline prompt:

```text
You are running the OPAL RXR baseline at L0. Do not call code_extension and do not call any extension/rxr variant of the policy module endpoints. Use list_policy_modules if needed to determine whether the hotfix module already exists. If it already exists, call update_policy_module; otherwise call create_policy_module. Use module_path and commit_message from the task, and provide rego_content that implements the requested outage policy change. Follow the task's grading contract exactly and end with exactly one fenced JSON block containing the requested top-level result object.
```

`L1` system prompt:

```text
You are running an OPAL RXR round-trip test at L1. For each required mutation, use RXR-enabled execution at this level with valid forward execution logic and valid reversal logic. Do not rely on direct baseline execution. In OPAL, perform the reversible mutation through `create_policy_module` or `update_policy_module` with RXR enabled. The forward path should apply exactly the requested mutation, leave it applied, and return stable hotfix details for the changed module, including `module_path`, `package_name`, `action`, and `rego_content`. The reversal path should read that execution result, undo exactly what was created or updated, and report its outcome.
```

`L2` system prompt:

```text
You are running an OPAL RXR round-trip test at L2. For each required mutation, use RXR-enabled execution at this level and request extension behavior with reversal support. Do not rely on direct baseline execution. In OPAL, perform the reversible mutation through `create_policy_module` or `update_policy_module` with RXR enabled. Apply exactly the requested mutation once per target module, return stable hotfix details for the changed module, including `module_path`, `package_name`, `action`, and `rego_content`, and ensure the reversal path can undo those changes from the execution result.
```

`L3` system prompt:

```text
You are running an OPAL RXR round-trip test at L3. For each required mutation, use RXR-enabled execution at this level with source-aware reversible behavior. In OPAL, perform the reversible mutation through `create_policy_module` or `update_policy_module` with RXR enabled. Apply exactly one mutation per requested target module. Do not perform exploratory, duplicate, or debugging mutations. Return stable hotfix details for what was created or updated, including `module_path`, `package_name`, `action`, and `rego_content`, so reversal can undo exactly those changes from the execution result.
```

`L4` system prompt:

```text
You are running an OPAL RXR round-trip test at L4. Use RXR-enabled reversible execution at this level. In OPAL, perform the reversible mutation through `code_extension`. The forward path should perform only the requested mutation, leave it applied, and assign the final structured payload to the expected result variable. Do not perform exploratory or debugging mutations. Keep the hotfix in a single `code_extension` execution. The reversal path must undo the created or updated module using the execution result. Ensure that result exposes stable hotfix details, including `module_path`, `package_name`, `action`, and `rego_content`.
```

### RXR Case: `test2`

Task prompt:

```text
A sev-1 outage in the production payments cluster forced traffic onto a stale cache replica. Apply an emergency OPAL policy hotfix so the on-call responders can run the cache failover operation while the incident is active. Perform the requested mutation in RXR mode so it can be reversed after execution.

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

If the module already exists, replace it with the corrected hotfix. If you inspect existing policy modules or bundles for context, keep those steps read-only.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `module_path`, `package_name`, `action`, and `outage_resolved`. Set `module_path` to `incident/cache_failover_hotfix.rego`, `package_name` to `app.incident.cache_failover_hotfix`, `action` to either `created` or `updated`, and `outage_resolved` to `true`.
```

Verification contract:

- one or more RXR records may be created in RXR mode, but every successful mutation record must be captured and reversed
- hotfix snapshot must resolve to `incident/cache_failover_hotfix.rego`
- normalized OPAL `/policy` state must differ from the pre-task snapshot after the forward mutation
- decision on `app/incident/cache_failover_hotfix/allow` must become `true`
- negative control must remain `false`
- reversal must clear the hotfix and restore normalized OPAL `/policy` state to the pre-task snapshot

### RXR Case: `test1`

Task prompt:

```text
A sev-1 outage in the production payments cluster is still unresolved because the active authorization gate module at "incident/payments_outage_gate.rego" is too restrictive. Update that module so on-call responders can perform the cache failover action, but only under the intended sev-1 break-glass conditions. Perform the requested mutation in RXR mode so it can be reversed after execution.

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

Read the existing tracked policy bundle or current module if you need context, but keep those steps read-only. Do not widen access beyond those outage conditions.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `module_path`, `package_name`, `action`, and `outage_resolved`. Set `module_path` to `incident/payments_outage_gate.rego`, `package_name` to `app.incident.payments_outage_gate`, `action` to `updated`, and `outage_resolved` to `true`.
```

Verification contract:

- one or more RXR records may be created in RXR mode, but every successful mutation record must be captured and reversed
- normalized OPAL `/policy` state must differ from the pre-task snapshot after the forward mutation
- decision on `app/incident/payments_outage_gate/allow` must become `true`
- negative control must remain `false`
- reversal must restore the restrictive baseline module and the full normalized OPAL `/policy` snapshot

### RXR Case: `test3`

Task prompt:

```text
A sev-1 outage in the production payments cluster now requires a recovery action that no existing production policy covers: allowing on-call responders to run the replica promotion operation. Create a new emergency OPAL policy hotfix. Perform the requested mutation in RXR mode so it can be reversed after execution.

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

No existing module in the live bundle already grants this exact operation, so create a new policy module rather than overwriting an unrelated one. If you inspect existing policy modules or bundles for context, keep those steps read-only.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `module_path`, `package_name`, `action`, and `outage_resolved`. Set `module_path` to `incident/payments_replica_promote_hotfix.rego`, `package_name` to `app.incident.payments_replica_promote_hotfix`, `action` to `created`, and `outage_resolved` to `true`.
```

Verification contract:

- one or more RXR records may be created in RXR mode, but every successful mutation record must be captured and reversed
- normalized OPAL `/policy` state must differ from the pre-task snapshot after the forward mutation
- decision on `app/incident/payments_replica_promote_hotfix/allow` must become `true`
- negative control must remain `false`
- reversal must delete the module and restore normalized OPAL `/policy` state to the pre-task snapshot

## Verifier And Unit-Test Notes

Current focused tests:

```bash
uv run python -m unittest \
  tests.test_opal_benchmark_token_paths \
  tests.test_opal_standard_harness \
  tests.test_opal_rxr_harness \
  tests.test_opal_verify_run \
  tests.test_runner_tool_projection \
  tests.test_cage_code_extension \
  tests.test_opal_server_normalization
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
- `tests/test_opal_rxr_harness.py`
  - nested RXR hotfix results are flattened correctly
  - RXR record IDs are extracted from export logs and text logs
  - multiple RXR record IDs are preserved in creation order
  - benchmark non-empty-bundle validation skips code-extension records
  - normalized OPAL policy-state comparisons ignore ordering and commit-hash noise
- `tests/test_opal_verify_run.py`
  - `opal/test2` semantic verification accepts the expected candidate subset
  - `opal/test3` semantic verification accepts the six required rejected paths
  - extension-usage enforcement behaves correctly for `L1` through `L4`
- `tests/test_runner_tool_projection.py`
  - `L4` exposes only `code_extension`
  - generated code-like arguments are HTML-entity decoded before execution
- `tests/test_cage_code_extension.py`
  - L4 `code_extension` accepts explicit code and decodes HTML entities
  - generic extension-point execution decodes HTML entities for non-L4 `extension_code` paths
- `tests/test_opal_server_normalization.py`
  - OPAL candidate/publish helper aliases normalize to canonical data-update fields
  - policy-hotfix writes preserve the target Rego package when adapting donor modules

Stats verification details:

- `scripts/opal/opal_verify_run.py` requires a strict fenced JSON final answer with the required top-level key.
- `opal/test2` is semantically verified against `topic_counts`, accepted `decision` variants, and `selected_candidate_ids`.
- `opal/test3` is semantically verified against the required rejected-path set.
- `L1` to `L3` fail verification if no extension is triggered.
- `L4` fails verification if it completes with only raw tool calls and no extension activity.
