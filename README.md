# OPAL (Open Policy Administration Layer) — Symphony Integration

**Source:** [permitio/opal](https://github.com/permitio/opal)
**Primary testing focus:** L0–L4 level comparison, cross-LLM evaluation, GoEx modes for mutation safety

## Overview

OPAL is an administration layer for Open Policy Agent (OPA) that detects changes to both policy and policy data in real time, pushing live updates to policy agents. This example integrates Symphony's extension framework directly into the OPAL server, adding four benchmark-oriented control-plane workflows: policy bundle triage, guarded policy hotfixes, data update publishing, and server statistics.

The benchmark now mirrors real operator tasks more closely: incident-time policy lookup, fleet blast-radius reporting, callback-aware data rollout sanitization, and a GoEx-protected emergency policy hotfix. A set of **policy CRUD endpoints** still allows LLM agents to create, update, and delete `.rego` modules directly in the tracked Git clone — each operation commits locally so changes are immediately visible in subsequent bundle fetches and properly reported in differential bundles.

## Capabilities

### Policy Bundle Capabilities

| Capability | Signature | `mutates` | Description |
|---|---|---|---|
| `filter_modules_by_path` | `(modules, path_prefix) → list[dict]` | `False` | Modules whose path starts with prefix |
| `exclude_modules_by_path` | `(modules, path_prefix) → list[dict]` | `False` | Exclude modules whose path starts with prefix |
| `filter_modules_by_package` | `(modules, package_substring) → list[dict]` | `False` | Modules whose package name contains substring |
| `search_module_content` | `(modules, query) → list[dict]` | `False` | Modules whose Rego source contains query |
| `get_module_paths` | `(modules) → list[str]` | `False` | Extract all file paths from modules |
| `get_package_names` | `(modules) → list[str]` | `False` | Extract unique package names |
| `exclude_test_modules` | `(modules) → list[dict]` | `False` | Exclude modules whose path contains "test" |
| `filter_modules_by_suffix` | `(modules, suffix) → list[dict]` | `False` | Modules whose path ends with suffix |
| `exclude_modules_by_suffix` | `(modules, suffix) → list[dict]` | `False` | Exclude modules whose path ends with suffix |

### Data Update Capabilities

| Capability | Signature | `mutates` | Description |
|---|---|---|---|
| `filter_entries_by_topic` | `(entries, topic) → list[dict]` | `False` | Entries belonging to a specific topic |
| `exclude_entries_by_topic` | `(entries, topic) → list[dict]` | `False` | Exclude entries for a specific topic |
| `get_all_entry_topics` | `(entries) → list[str]` | `False` | All unique topics across entries |
| `deduplicate_entries` | `(entries) → list[dict]` | `False` | Remove duplicates by topics+dst_path |
| `filter_entries_by_dst_path` | `(entries, path_prefix) → list[dict]` | `False` | Entries whose dst_path starts with prefix |
| `validate_entry_urls` | `(entries) → list[dict]` | `False` | Keep only entries with valid http(s) URLs |
| `set_entry_save_method` | `(entries, save_method) → list[dict]` | **`True`** | Set save_method (PUT/PATCH) on all entries |

### Policy Hotfix Capabilities

| Capability | Signature | `mutates` | Description |
|---|---|---|---|
| `read_policy_module` | `(repo_path, module_path) → str \| null` | `False` | Read the current module contents from the tracked Git clone |
| `module_exists` | `(repo_path, module_path) → bool` | `False` | Check whether a module exists in the tracked clone |
| `upsert_policy_module` | `(repo_path, module_path, rego_content, commit_message) → dict` | **`True`** | Create or replace a module and commit it |
| `delete_policy_module` | `(repo_path, module_path, commit_message, missing_ok=False) → dict` | **`True`** | Delete a module and commit the removal |

### Statistics Capabilities

| Capability | Signature | `mutates` | Description |
|---|---|---|---|
| `get_client_list` | `(stats) → list[dict]` | `False` | Flat list of all client records |
| `get_clients_by_topic` | `(stats, topic) → list[str]` | `False` | Client IDs subscribed to a topic |
| `count_clients_per_topic` | `(stats) → dict` | `False` | Client count per topic |
| `get_topics_with_no_subscribers` | `(stats, known_topics) → list[str]` | `False` | Topics with zero subscribers |
| `get_server_count` | `(stats) → int` | `False` | Number of active server replicas |
| `get_client_count` | `(stats) → int` | `False` | Total number of connected clients |

## GoEx Configuration

```python
goex_registry = GoExRegistry(auto_approve=False, auto_approve_readonly=True)
```

- Read-only code (bundle filtering, statistics analysis, entry inspection) → **auto-approved**
- Mutating code (`set_entry_save_method`, `upsert_policy_module`, `delete_policy_module`) → **held for SRE review**

## Extension Points

| Extension Point | Description | Endpoint |
|---|---|---|
| `post_policy_bundle` | Filter, transform, or augment policy bundles before serving | `GET /policy` |
| `policy_hotfix` | Create or revise an emergency policy module via the tracked Git repo | `POST /symphony/code_extension` |
| `post_data_update` | Validate, filter, deduplicate, or transform data entries before publishing | `POST /data/config` |
| `post_statistics` | Compute aggregates, detect anomalies, or reformat statistics | `GET /statistics` |

## Policy CRUD Tools

LLM agents can manage Rego policy modules directly via MCP tools that map to the `/policy/modules` REST endpoints. Each mutation writes to the local Git clone and commits the change, so the standard `GET /policy` bundle-serving path picks it up immediately.

| MCP Tool | HTTP Method | Description |
|---|---|---|
| `list_policy_modules` | `GET /policy/modules` | List all `.rego` files with paths, sizes, and current HEAD hash |
| `create_policy_module` | `POST /policy/modules` | Write a new `.rego` file and commit; returns `old_hash` / `new_hash` |
| `update_policy_module` | `PUT /policy/modules` | Overwrite an existing `.rego` file and commit |
| `delete_policy_module` | `DELETE /policy/modules` | Remove a `.rego` file and commit; shows in `deleted_files` for diff bundles |
**Request body fields (create / update):** `module_path` (repo-relative, e.g. `compliance/block.rego`), `rego_content` (raw Rego source), `commit_message` (optional).

**Change detection:** After a CRUD commit, fetching a differential bundle with `base_hash` set to the pre-mutation hash correctly reports additions, modifications, and deletions through OPAL's standard `BundleMaker.make_diff_bundle` mechanism.

## Endpoints

| Method | Path | Levels | Description |
|---|---|---|---|
| `GET` | `/policy` | L0–L3 | Fetch policy bundle from tracked Git repository |
| `GET` | `/policy/modules` | — | List all Rego policy modules in the repository |
| `POST` | `/policy/modules` | — | Create a new Rego policy module (Git commit) |
| `PUT` | `/policy/modules` | — | Update an existing Rego policy module (Git commit) |
| `DELETE` | `/policy/modules` | — | Delete a Rego policy module (Git commit) |
| `POST` | `/data/config` | L0–L3 | Publish data update to OPAL clients |
| `GET` | `/data/config` | — | Get base data source configuration |
| `GET` | `/statistics` | L0–L3 | Get server statistics (clients, topics, replicas) |
| `GET` | `/stats` | — | Get brief statistics (client/server counts) |
| `POST` | `/token` | — | Generate JWT access token |
| `GET` | `/healthcheck` | — | Server health check |
| `POST` | `/symphony/code_extension` | L4 | Freeform code generation from capabilities (L4 entry point) |
| `GET` | `/symphony/goex/records` | — | GoEx SRE endpoints |

> **L4 note:** At L4 the agent sends a freeform prompt to `POST /symphony/code_extension`, which generates and executes code using the registered capabilities directly. The utility endpoints remain visible at L4 for exploration.

## Quick Start

```bash
cd examples/opal

# Clone policy repo (needed for GET /policy endpoint)
mkdir -p regoclone && git clone https://github.com/permitio/opal-example-policy-repo regoclone/opal_repo_clone

# Terminal 1: Start the OPAL server with Symphony
OPAL_REPO_WATCHER_ENABLED=false OPAL_PUBLISHER_ENABLED=false OPAL_STATISTICS_ENABLED=true \
OPAL_POLICY_REPO_REUSE_CLONE_PATH=true \
  uv run python -m uvicorn opal_server.main:app --reload --timeout-keep-alive 300
# API docs: http://127.0.0.1:8000/docs

# Terminal 2: Run the agent CLI
uv run python agent_cli.py --level L0 "Get the policy bundle"
uv run python agent_cli.py --level L1 "Fetch the policy bundle but only return modules related to RBAC"
uv run python agent_cli.py --level L2 "Get server statistics and summarize client subscription counts per topic"
```

For server-side code generation (L2/L3/L4), start the server with:

```bash
OPAL_REPO_WATCHER_ENABLED=false OPAL_PUBLISHER_ENABLED=false OPAL_STATISTICS_ENABLED=true \
OPAL_POLICY_REPO_REUSE_CLONE_PATH=true \
SYMPHONY_CODEGEN_PROVIDER=gemini SYMPHONY_CODEGEN_MODEL=gemini-2.5-flash \
  uv run python -m uvicorn opal_server.main:app --reload --timeout-keep-alive 300
```

## Running On Kubernetes (kind / EKS)

The benchmark source of truth is now a real OPAL deployment:

- Postgres broadcast backbone
- 2 `opal_server` replicas
- 8 `opal_client` workloads with inline OPA
- a benchmark data-source service
- embedded Symphony MCP + `/symphony/code_extension`

Local `kind` bootstrap from repo root:

```bash
./scripts/start_opal.sh
```

This defaults to:

```bash
OPAL_POLICY_REPO_URL=https://github.com/pklatka/opal-example-policy-repo
```

Override it only if you want to benchmark against a different public policy repo.

That path creates or reuses a `kind` cluster, deploys the stack, and
port-forwards:

- public API + embedded MCP to `http://127.0.0.1:8000`
- single-writer admin API to `http://127.0.0.1:8001`

The deployer writes a generated env file under `/tmp/` only after the full
stack is ready, containing `OPAL_BASE_URL`, `OPAL_ADMIN_BASE_URL`,
`OPAL_NAMESPACE`, `OPAL_KUBE_CONTEXT`, `OPAL_CLIENT_TOKEN`, and
`OPAL_DATA_SOURCE_TOKEN`.

Existing EKS cluster deployment:

```bash
export AWS_REGION=us-east-1
./scripts/opal/deploy_opal_k8.sh --target eks --namespace symphony-opal
```

Run the benchmark against the deployed cluster:

```bash
OPAL_BASE_URL=http://127.0.0.1:8000 \
OPAL_ADMIN_BASE_URL=http://127.0.0.1:8001 \
OPAL_NAMESPACE=symphony-opal \
./scripts/run_opal_tests.sh anthropic haiku

OPAL_BASE_URL=http://127.0.0.1:8000 \
OPAL_ADMIN_BASE_URL=http://127.0.0.1:8001 \
OPAL_NAMESPACE=symphony-opal \
./scripts/run_opal_goex_tests.sh anthropic haiku
```

`run_opal_tests.sh` now resets benchmark state before each standard-suite job
via `POST /symphony/benchmark/reset`. Use `--no-reset-state` or
`OPAL_BENCHMARK_RESET=0` only when you explicitly want to reuse prior state.

## Legacy Single-Process Mode

The old single-process control-plane path is still available for debugging:

```bash
./scripts/start_opal_single_process.sh
```

Use `serve_prod.py` only when you explicitly want the old single-node flow.

### LLM Provider (required for L1+)

```bash
# Ollama (default)
ollama pull qwen3:8b
ollama serve

# Or cloud providers
export ANTHROPIC_API_KEY=sk-...
export GEMINI_API_KEY=...

# Use a specific provider
uv run python agent_cli.py --provider anthropic --level L1 "Get RBAC policy modules only"
uv run python agent_cli.py --provider gemini --level L2 "Analyze which topics have zero subscribers"
```

---

## Test Plan 1: L0 vs L1 vs L2 vs L3 vs L4

The exact benchmark prompt strings live in
[`TESTING_PLAN.md`](TESTING_PLAN.md) and
[`scripts/run_opal_tests.sh`](../../scripts/run_opal_tests.sh). The examples
below describe the same scenarios at a higher level.

### Case A: Incident Policy Triage (read-only)

**Prompt:** see the exact multi-line `opal/test1` prompt in [`TESTING_PLAN.md`](TESTING_PLAN.md).

```bash
uv run python agent_cli.py --level L0 "This is an OPAL benchmark lookup task, not an incident-response simulation. Benchmark auth is already configured. An SRE is triaging a sev-1 outage in the production payments cluster and needs the exact break-glass policy module that should be applied. Fetch the live policy bundle from the OPAL control plane and return only the single matching module path."
uv run python agent_cli.py --level L1 "This is an OPAL benchmark lookup task, not an incident-response simulation. Benchmark auth is already configured. An SRE is triaging a sev-1 outage in the production payments cluster and needs the exact break-glass policy module that should be applied. Fetch the live policy bundle from the OPAL control plane and return only the single matching module path."
uv run python agent_cli.py --level L2 "This is an OPAL benchmark lookup task, not an incident-response simulation. Benchmark auth is already configured. An SRE is triaging a sev-1 outage in the production payments cluster and needs the exact break-glass policy module that should be applied. Fetch the live policy bundle from the OPAL control plane and return only the single matching module path."
uv run python agent_cli.py --level L3 "This is an OPAL benchmark lookup task, not an incident-response simulation. Benchmark auth is already configured. An SRE is triaging a sev-1 outage in the production payments cluster and needs the exact break-glass policy module that should be applied. Fetch the live policy bundle from the OPAL control plane and return only the single matching module path."
uv run python agent_cli.py --level L4 "This is an OPAL benchmark lookup task, not an incident-response simulation. Benchmark auth is already configured. An SRE is triaging a sev-1 outage in the production payments cluster and needs the exact break-glass policy module that should be applied. Fetch the live policy bundle from the OPAL control plane and return only the single matching module path."
```

| Level | Expected behavior | Features exercised |
|-------|------------------|-------------------|
| **L0** | Returns the full policy bundle; the model still has to identify the correct break-glass module from realistic distractors. | Vanilla API, MCP routing |
| **L1** | Sends extension code that filters the bundle down to the single incident policy. | Sandbox execution, capability chaining, read-only auto-approve |
| **L2** | Sends a task description and lets the server generate the filtering logic. | Two-phase flow, `needs_extension`, server codegen |
| **L3** | Server reads `_default_get_policy` and generates source-aware bundle triage code. | Source code reading, `server_generate_and_execute()` |
| **L4** | Uses the same `get_policy_bundle` route with `extension_level="L4"` so the benchmark stays on the domain endpoint instead of drifting to generic tooling. | Freeform extension on the benchmarked route |

### Case B: Fleet Blast-Radius Report (read-only)

**Prompt:** see the exact multi-line `opal/test2` prompt in [`TESTING_PLAN.md`](TESTING_PLAN.md).

```bash
uv run python agent_cli.py --level L0 "This is an OPAL benchmark extraction task, not a dashboard-writing exercise. Benchmark auth is already configured. Analyze topics [policy_data, incident_access, feature_flags, directory_sync, audit_logs, compliance_audit]. Use the normalized benchmark_stats view to compute client counts per topic, zero-subscriber topics, exact audit_logs subscribers, exact incident_access subscribers, and total client/server counts."
uv run python agent_cli.py --level L1 "This is an OPAL benchmark extraction task, not a dashboard-writing exercise. Benchmark auth is already configured. Analyze topics [policy_data, incident_access, feature_flags, directory_sync, audit_logs, compliance_audit]. Use the normalized benchmark_stats view to compute client counts per topic, zero-subscriber topics, exact audit_logs subscribers, exact incident_access subscribers, and total client/server counts."
uv run python agent_cli.py --level L2 "This is an OPAL benchmark extraction task, not a dashboard-writing exercise. Benchmark auth is already configured. Analyze topics [policy_data, incident_access, feature_flags, directory_sync, audit_logs, compliance_audit]. Use the normalized benchmark_stats view to compute client counts per topic, zero-subscriber topics, exact audit_logs subscribers, exact incident_access subscribers, and total client/server counts."
```

| Level | Expected behavior | Features exercised |
|-------|------------------|-------------------|
| **L0** | Returns normalized benchmark statistics so the model can extract exact counts and subscriber lists without reverse-engineering control-plane channels. | Benchmark-safe normalization, exact extraction |
| **L1** | Extension code still triggers, but the final answer is expected to use the normalized benchmark view for exact fields. | Multi-capability composition plus normalized stats |
| **L2** | Server generates benchmark-focused statistics logic while preserving the same `/statistics` route shape. | Two-phase with benchmark-focused analytics |

### Case C: Callback-Aware Emergency Data Rollout (mutating)

**Prompt:** see the exact multi-line `opal/test3` prompt in [`TESTING_PLAN.md`](TESTING_PLAN.md).

```bash
uv run python agent_cli.py --level L0 "This is an OPAL benchmark mutation task, not a rollout-planning exercise. Benchmark auth is already configured. Publish a data update with a one-time callback to https://ops.internal/v1/opal/update-report. Allow only incident_access, directory_sync, and feature_flags topics; exclude audit_logs; reject staging hosts; validate URLs; deduplicate by topic+dst_path; and force save_method=PUT."
uv run python agent_cli.py --level L1 "This is an OPAL benchmark mutation task, not a rollout-planning exercise. Benchmark auth is already configured. Publish a data update with a one-time callback to https://ops.internal/v1/opal/update-report. Allow only incident_access, directory_sync, and feature_flags topics; exclude audit_logs; reject staging hosts; validate URLs; deduplicate by topic+dst_path; and force save_method=PUT."
uv run python agent_cli.py --level L2 "This is an OPAL benchmark mutation task, not a rollout-planning exercise. Benchmark auth is already configured. Publish a data update with a one-time callback to https://ops.internal/v1/opal/update-report. Allow only incident_access, directory_sync, and feature_flags topics; exclude audit_logs; reject staging hosts; validate URLs; deduplicate by topic+dst_path; and force save_method=PUT."
```

| Level | Expected behavior | Features exercised |
|-------|------------------|-------------------|
| **L0** | Publishes the raw batch and callback as-is. | Vanilla publish, no sanitization |
| **L1** | Extension code sanitizes the rollout batch before publish and preserves the one-time callback. | Capability chaining, sanitization pipeline, `mutates=True` detection |
| **L2** | Server generates the sanitization pipeline or signals `needs_extension`. | Two-phase + mutation handling |

---

## Test Plan 2: Cross-LLM Comparison (Reasoning vs No-Reasoning)

Run each Case (A, B, C) across providers and reasoning modes:

```bash
# Ollama with reasoning (default)
uv run python agent_cli.py --provider ollama --level L1 "<PROMPT>"

# Ollama without reasoning
uv run python agent_cli.py --provider ollama --level L1 --no-reasoning "<PROMPT>"

# Anthropic (Claude) with reasoning
uv run python agent_cli.py --provider anthropic --level L1 "<PROMPT>"

# Anthropic without reasoning
uv run python agent_cli.py --provider anthropic --level L1 --no-reasoning "<PROMPT>"

# Gemini with reasoning
uv run python agent_cli.py --provider gemini --level L1 "<PROMPT>"

# Gemini without reasoning
uv run python agent_cli.py --provider gemini --level L1 --no-reasoning "<PROMPT>"
```

### Comparison Matrix

| Provider | Reasoning | Prompt | Metrics to Capture |
|----------|-----------|--------|-------------------|
| Ollama (qwen3:8b) | ✅ on | Case A, B, C | Tokens, time, correctness, code quality |
| Ollama (qwen3:8b) | ❌ off | Case A, B, C | Tokens, time, correctness, code quality |
| Anthropic (haiku) | ✅ on | Case A, B, C | Tokens, time, correctness, code quality |
| Anthropic (haiku) | ❌ off | Case A, B, C | Tokens, time, correctness, code quality |
| Gemini (2.5-flash) | ✅ on | Case A, B, C | Tokens, time, correctness, code quality |
| Gemini (2.5-flash) | ❌ off | Case A, B, C | Tokens, time, correctness, code quality |

---

## Test Plan 3: GoEx Modes Comparison (Guarded Policy Hotfix)

The GoEx benchmark now exercises a realistic control-plane mutation without
adding a benchmark-specific OPAL API route: it uses the existing
`code_extension` endpoint plus the existing policy module CRUD endpoints.

### Case G1: L0 Baseline (direct hotfix apply)

```bash
uv run python agent_cli_goex.py \
  --api-url http://127.0.0.1:8000 \
  --mcp-url http://127.0.0.1:8000/mcp/sse \
  --codegen-provider ws://127.0.0.1:8000/symphony/codegen/ws \
  --provider anthropic \
  --level L0 \
  --execution-mode direct
```

What to observe:

- the harness uses `list_policy_modules` and then `create_policy_module` or
  `update_policy_module`
- `incident/cache_failover_hotfix.rego` is created or updated
- the harness performs explicit cleanup after verification
- no GoEx record is created in direct mode

### Case G2: L1–L3 GoEx hotfix apply

```bash
uv run python agent_cli_goex.py \
  --api-url http://127.0.0.1:8000 \
  --mcp-url http://127.0.0.1:8000/mcp/sse \
  --codegen-provider ws://127.0.0.1:8000/symphony/codegen/ws \
  --provider anthropic \
  --level L1 \
  --execution-mode goex
```

What to observe:

- the agent uses the existing `code_extension` endpoint with
  `extension_point="policy_hotfix"`
- extension code or generated code calls `upsert_policy_module(...)`
- GoEx records the mutation because policy writes are `mutates=True`
- the created module contains package `app.incident.cache_failover_hotfix`
- the record includes executable `reversal_code`

### Case G3: L4 freeform hotfix apply

```bash
uv run python agent_cli_goex.py \
  --api-url http://127.0.0.1:8000 \
  --mcp-url http://127.0.0.1:8000/mcp/sse \
  --codegen-provider ws://127.0.0.1:8000/symphony/codegen/ws \
  --provider anthropic \
  --level L4 \
  --execution-mode goex
```

What to observe:

- the agent prefers `code_extension` with `extension_point="policy_hotfix"`
- generated code uses the hotfix capability set and the hotfix context provider
- GoEx still captures the mutation and reversal metadata

### GoEx SRE endpoint verification

After a GoEx run:

```bash
curl http://127.0.0.1:8000/symphony/goex/records
curl "http://127.0.0.1:8000/symphony/goex/records?status=executed"
curl http://127.0.0.1:8000/symphony/goex/records/<RECORD_ID>
curl -X POST http://127.0.0.1:8000/symphony/goex/records/<RECORD_ID>/reverse
```
