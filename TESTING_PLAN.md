# OPAL — Focused benchmark suite

This document is the benchmark source of truth for the full OPAL deployment
scenarios in this repo. The exact task strings for Tests 1–3 live in
[`scripts/run_opal_tests.sh`](../../scripts/run_opal_tests.sh), and the exact
GoEx task for Test 4 lives in [`agent_cli_goex.py`](agent_cli_goex.py).
Update those files together with this document whenever prompts change.

For Tests 1–3, the runner appends three prompt layers:

1. task prompt (`OPAL_T1` / `OPAL_T2` / `OPAL_T3`)
2. benchmark system prompt (`OPAL_SYS_T1` / `OPAL_SYS_T2` / `OPAL_SYS_T3`)
3. automated grading suffix (`_GRADING1` / `_GRADING2` / `_GRADING3`)

The sections below now include the exact current task prompts plus the exact
system/grading prompts that materially constrain the run.

Benchmark-mode MCP visibility intentionally hides `generate_access_token`, so
the prompts do not waste budget teaching the model to avoid that tool.
`benchmark_stats` is also intentionally normalized input, not a pre-solved
answer payload.
The standard-suite reset endpoint restores benchmark data paths and removes any
leftover `incident/cache_failover_hotfix.rego` module before the next job.

## One-command environment

From repo root:

```bash
./scripts/start_opal.sh
```

Default tracked policy repo:

```bash
https://github.com/pklatka/opal-example-policy-repo
```

Pass `OPAL_POLICY_REPO_URL=...` only if you want to benchmark against a different public policy repo.

This creates or reuses a local `kind` cluster, deploys the full benchmark
stack, and port-forwards the public and admin OPAL services to:

- `http://127.0.0.1:8000` for public API + embedded MCP
- `http://127.0.0.1:8001` for the single-writer admin API used by GoEx and policy CRUD

The deployer writes a generated env file under `/tmp/` only after the full
stack is ready, with `OPAL_BASE_URL`, `OPAL_ADMIN_BASE_URL`,
`OPAL_NAMESPACE`, `OPAL_KUBE_CONTEXT`, `OPAL_CLIENT_TOKEN`, and
`OPAL_DATA_SOURCE_TOKEN`.

Legacy single-process mode remains available for debugging only:

```bash
./scripts/start_opal_single_process.sh
```

## Automated benchmark matrix

```bash
./scripts/run_opal_tests.sh --provider anthropic --model haiku
```

- Logs: `logs/opal/<model>/<level>/opal_testN.log`
- Stats: `stats/opal/opal_<provider>_<model>__server_<codegen>_*.jsonl`
- Goldens: [`scripts/opal/expected/`](../../scripts/opal/expected/)
- Standard suite grading for `opal/test1`–`opal/test3` is based on strict
  fenced final JSON plus golden match only
- Tool choice, first tool, and `extension_triggered` are still exported as
  diagnostics so L0 vs L1–L4 comparisons stay visible
- Stats rows also carry normalized token/cost aliases plus L0 delta fields
- The runner resets benchmark state before each standard-suite job via
  `POST /symphony/benchmark/reset` unless `--no-reset-state` or
  `OPAL_BENCHMARK_RESET=0` is used

Remote / EKS mode:

```bash
OPAL_BASE_URL=http://<load-balancer>:8080 \
OPAL_ADMIN_BASE_URL=http://127.0.0.1:8001 \
OPAL_NAMESPACE=symphony-opal \
OPAL_KUBE_CONTEXT=<context> \
./scripts/run_opal_tests.sh anthropic haiku
```

## GoEx suite

```bash
./scripts/run_opal_goex_tests.sh anthropic haiku
```

- Harness: [`agent_cli_goex.py`](agent_cli_goex.py)
- L0 uses `--execution-mode direct`
- L1–L4 use `--execution-mode goex`
- Validation is record-first for GoEx: create hotfix, capture and inspect the
  GoEx record, optionally confirm admin API readback, verify the hotfix reaches
  `opal-client-authz-a`, reverse it, and confirm the client no longer has the
  policy
- Unlike Tests 1–3, GoEx remains a stateful benchmark: success is based on
  hotfix state change, record capture, propagation, and reversal

Disable the websocket worker with `OPAL_GOEX_DISABLE_CODEGEN_WORKER=1`.

## Extension reminder

Instrumented OPAL routes run extensions only when the request includes
`extension_code` or `task_description`, or when the agent uses L4
`code_extension`.

## Test 1 — `opal/test1` (incident-time policy lookup, L0–L4)

Prompt core from `run_opal_tests.sh`:

```text
This is an OPAL benchmark lookup task, not an incident-response simulation. Benchmark auth is already configured.

An SRE is triaging a sev-1 outage in the production payments cluster and needs the exact break-glass policy module that should be applied. Fetch the live policy bundle from the OPAL control plane and return only the single matching module path.

The required behavior is:
- Temporary break-glass access for on-call responders.
- Only during a sev-1 incident.
- Only outside business hours.
- Only when an emergency_override flag is present.

If a candidate module has an empty `package_name` field, derive the package name from its `rego` source by reading the `package ...` declaration. Do not emit an empty package name or `unknown` when the Rego source contains the package.

Do not return shared helpers, tenant-specific overrides, sandbox or dev-only policies, readonly responder policies, or test files.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `policy_modules` array with exactly one object. That object must include `path`, `package_name`, and `why`.
```

System prompt used for Tests 1–3:

```text
You are running the OPAL benchmark.
- Benchmark auth is already configured and valid.
- Do not ask for credentials, tokens, or elevated permissions.
- Treat auth/bootstrap as out of scope for Tests 1–3.
- Do not provide incident-management advice, rollout recommendations, or extra narrative.
- Use the OPAL benchmark tools and return only the requested fenced JSON.
```

Test 1 extra system prompt:

```text
This is a benchmark lookup task. Select the single best matching module from the live OPAL bundle. If the selected module has an empty `package_name`, derive it from the module `rego` source instead of returning empty or `unknown`.
```

Golden:

- exactly one module
- `policy_modules[0].path == "incident/break_glass.rego"`
- `policy_modules[0].package_name == "app.incident.break_glass"`

Fixture distractors include:

- `incident/oncall_readonly.rego`
- `incident/vendor_emergency_access.rego`
- `sandbox/break_glass_dev.rego`
- `tenants/playground/break_glass_override.rego`
- `shared/*` and `tests/*`

## Test 2 — `opal/test2` (fleet blast-radius report, L0–L4)

Prompt core from `run_opal_tests.sh`:

```text
This is an OPAL benchmark extraction task, not a dashboard-writing exercise. Benchmark auth is already configured.

Get the current control-plane statistics and analyze these topics: ["policy_data", "incident_access", "feature_flags", "directory_sync", "audit_logs", "compliance_audit"]. Use the normalized `benchmark_stats` view to work with stable client IDs and per-topic membership, and compute the requested aggregates from that input.

Return:
- the unique subscribed client count for each topic
- any zero-subscriber topics from that list
- the exact subscriber client IDs for "audit_logs"
- the exact subscriber client IDs for "incident_access"
- total client_count and server_count

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `summary` object with keys `topic_counts`, `zero_subscriber_topics`, `audit_subscribers`, `incident_access_subscribers`, `client_count`, and `server_count`.
```

Test 2 extra system prompt:

```text
This is a benchmark extraction task. Use the normalized OPAL statistics response to compute the requested counts and subscriber lists instead of relying on pre-solved answers.
```

Current golden values:

- `policy_data`: 4
- `incident_access`: 4
- `feature_flags`: 2
- `directory_sync`: 2
- `audit_logs`: 2
- `compliance_audit`: 0
- `client_count`: 8
- `server_count`: 2

Expected subscriber lists:

- `audit_logs`: `opal-client-audit-a-01`, `opal-client-sre-a-01`
- `incident_access`: `opal-client-authz-a-01`, `opal-client-authz-b-01`, `opal-client-directory-b-01`, `opal-client-sre-a-01`

## Test 3 — `opal/test3` (callback-aware emergency data rollout, L0–L4)

Prompt core from `run_opal_tests.sh`:

```text
This is an OPAL benchmark mutation task, not a rollout-planning exercise. Benchmark auth is already configured.

Execute the rollout now by publishing a data update with this one-time callback: "https://ops.internal/v1/opal/update-report".

Entries:
1. topic "incident_access", url "http://opal-benchmark-data:8081/v1/incidents/sev1/access-grants", dst_path "/incident/access"
2. topic "incident_access", url "http://opal-benchmark-data:8081/v1/incidents/sev1/access-grants", dst_path "/incident/access"
3. topic "directory_sync", url "http://opal-benchmark-data:8081/v1/directory/emergency/groups", dst_path "/directory/emergency/groups"
4. topic "audit_logs", url "http://opal-benchmark-data:8081/v1/audit/incident/stream", dst_path "/audit/incident/raw"
5. topic "incident_access", url "http://staging-opal-benchmark-data:8081/v1/incidents/sev1/access-grants", dst_path "/incident/access-staging"
6. topic "feature_flags", url "http://opal-benchmark-data:8081/v1/feature-flags/cache-failover", dst_path "/feature_flags/cache_failover"
7. topic "directory_sync", url "opal-benchmark-data:8081/v1/directory/emergency/bad", dst_path "/directory/bad"
8. topic "incident_access", url "http://opal-benchmark-data:8081/v1/incidents/sev1/access-grants?source=secondary", dst_path "/incident/access"

Before publishing to production OPAL clients:
- Allow only topics "incident_access", "directory_sync", and "feature_flags".
- Exclude any entries that target "audit_logs".
- Keep only entries whose URL starts with "http://" or "https://".
- Exclude any entry whose URL host contains "staging".
- Deduplicate by (topic + dst_path), keeping the first valid production entry.
- Set save_method to "PUT" for all remaining entries.
- Preserve the one-time callback on the update.

For automated grading, end your reply with exactly one Markdown fenced JSON block (```json ... ```), with no prose or extra text before or after the fence. The JSON must contain a top-level `result` object with keys `entries_published`, `published`, `removed`, and `callback_urls`. Each published entry must include `topic`, `dst_path`, and `save_method`. Each removed entry must include `index` and `reason`.
```

Test 3 extra system prompt:

```text
This is a benchmark mutation task. Sanitize the candidate rollout batch and publish only the entries that satisfy the production constraints while preserving the callback.
```

Golden:

- `entries_published == 3`
- published:
  - `incident_access` → `/incident/access` with `PUT`
  - `directory_sync` → `/directory/emergency/groups` with `PUT`
  - `feature_flags` → `/feature_flags/cache_failover` with `PUT`
- removed:
  - index 2: `duplicate`
  - index 4: `forbidden_topic`
  - index 5: `staging_host`
  - index 7: `invalid_url`
  - index 8: `duplicate_dst_path`
- `callback_urls == ["https://ops.internal/v1/opal/update-report"]`

Live verification:

- `opal-client-authz-a` must contain the `incident_access` payload in OPA
- `opal-client-directory-a` must contain the `directory_sync` payload in OPA
- `opal-client-web-a` must contain the `feature_flags` payload in OPA

## Test 4 — `opal/goex/test1` (guarded policy hotfix rollback)

Prompt core from `agent_cli_goex.py`:

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

GoEx system prompts used by level:

`L0`

```text
You are running the OPAL GoEx baseline at L0. Do not call code_extension and do not call any nonexistent apply_policy_hotfix endpoint. First call list_policy_modules. If the hotfix module already exists, call update_policy_module; otherwise call create_policy_module. Use module_path and commit_message from the task, and provide rego_content that implements the requested cache failover hotfix.
```

`L1`

```text
You are running an OPAL GoEx test at L1. Do not call apply_policy_hotfix because that endpoint does not exist. Call code_extension exactly once with prompt equal to the task, extension_point='policy_hotfix', execution_mode='goex', explicit Python code, and explicit reversal_code. The code must build the requested Rego source and assign result = upsert_policy_module(context['repo_path'], context['module_path'], rego_source, context['commit_message']). Add any extra fields directly onto that result dict instead of wrapping it under another key such as upsert_result. The reversal_code must delete the module if it was newly created, or restore context['current_rego'] with upsert_policy_module(...) if it already existed.
```

`L2`

```text
You are running an OPAL GoEx test at L2. Use the existing code_extension tool, not any benchmark-specific hotfix endpoint. Call code_extension exactly once with extension_point='policy_hotfix' and execution_mode='goex'. Write a precise prompt telling the server to create or replace the requested hotfix module, return the dict from upsert_policy_module(...) as the final result, add any metadata directly onto that dict instead of nesting it, and generate real reversal logic that restores context['current_rego'] or deletes a newly created module.
```

`L3`

```text
You are running an OPAL GoEx test at L3. Use the existing code_extension tool, not any benchmark-specific hotfix endpoint. Call code_extension exactly once with extension_point='policy_hotfix' and execution_mode='goex'. Use a detailed prompt that tells the server to use the provided policy bundle and current module context to generate the cache failover hotfix and matching reversal logic. The final result should stay flat: return the upsert_policy_module dict directly, with any extra metadata added onto that dict.
```

`L4`

```text
You are running an OPAL GoEx test at L4. Call code_extension with extension_point='policy_hotfix' and execution_mode='goex'. Ask it to create or replace the requested hotfix module and generate real reversal logic that restores the previous file or deletes the new one. Ask for the final result to stay flat: return the upsert_policy_module dict directly, with any metadata added onto that dict.
```

Harness pass conditions:

- L0 creates the hotfix directly, then cleanup removes it
- L1–L4 create or update `incident/cache_failover_hotfix.rego`
- the resulting module contains package `app.incident.cache_failover_hotfix`
- exactly one GoEx record is captured
- the record includes `reversal_code`
- `POST /symphony/goex/records/<id>/reverse` returns `status=reversed`
- `opal-client-authz-a` receives the hotfix before reversal
- the hotfix module no longer exists after reversal
- `opal-client-authz-a` no longer has the hotfix after reversal

Implementation note:

- no benchmark-specific `/policy/hotfix` endpoint is used
- L0 uses the existing `/policy/modules` CRUD tools
- L1–L4 use the existing `/symphony/code_extension` endpoint scoped to
  `extension_point="policy_hotfix"`

## Manual checks

```bash
curl -s http://127.0.0.1:8000/healthcheck
curl -s http://127.0.0.1:8000/policy/modules | python3 -m json.tool
curl -s http://127.0.0.1:8000/symphony/goex/records/<RECORD_ID> | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8000/symphony/goex/records/<RECORD_ID>/reverse
```
