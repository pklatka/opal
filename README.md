# OPAL (Open Policy Administration Layer) — Symphony Integration

**Source:** [permitio/opal](https://github.com/permitio/opal)
**Primary testing focus:** L0–L4 level comparison, cross-LLM evaluation, GoEx modes for mutation safety

## Overview

OPAL is an administration layer for Open Policy Agent (OPA) that detects changes to both policy and policy data in real time, pushing live updates to policy agents. This example integrates Symphony's extension framework directly into the OPAL server and benchmarks it through stateful outage scenarios instead of toy extraction prompts.

The benchmark focuses on Symphony-on-OPAL behavior:

- applying an existing production break-glass policy to an active outage gate
- deciding whether to trigger an emergency rollout from live OPAL statistics
- creating a new tightly scoped outage policy when no existing module covers the action
- replaying the same mutation patterns through GoEx with record capture and reversal

A set of **policy CRUD endpoints** still allows LLM agents to create, update, and delete `.rego` modules directly in the tracked Git clone. Each operation commits locally so changes are immediately visible in subsequent bundle fetches and properly reported in differential bundles.

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
| `policy_hotfix` | Create or revise an emergency policy module via the tracked Git repo | Existing `POST`/`PUT /policy/modules` endpoints for L1-L3, `POST /symphony/code_extension` for L4 |
| `post_data_update` | Validate, filter, deduplicate, or transform data entries before publishing | `POST /data/config` |
| `post_statistics` | Compute aggregates, detect anomalies, or reformat statistics | `GET /statistics` |

## Policy CRUD Tools

LLM agents can manage Rego policy modules directly via MCP tools that map to the `/policy/modules` REST endpoints. Each mutation writes to the local Git clone and commits the change, so the standard `GET /policy` bundle-serving path picks it up immediately. L1-L3 reversible extension/goex behavior is carried by optional parameters on these same endpoints; no separate hotfix endpoint is added.

| MCP Tool | HTTP Method | Description |
|---|---|---|
| `list_policy_modules` | `GET /policy/modules` | List all `.rego` files with paths, sizes, and current HEAD hash |
| `create_policy_module` | `POST /policy/modules` | Write a new `.rego` file and commit; returns `old_hash` / `new_hash` |
| `update_policy_module` | `PUT /policy/modules` | Overwrite an existing `.rego` file and commit |
| `delete_policy_module` | `DELETE /policy/modules` | Remove a `.rego` file and commit; shows in `deleted_files` for diff bundles |
**Request body fields (create / update):** `module_path` (repo-relative, e.g. `compliance/block.rego`), `rego_content` (raw Rego source), `commit_message` (optional). Extension/goex runs may also include `extension_level`, `extension_code`, `task_description`, `execution_mode`, `reversal_code`, and `package_name`.

**Change detection:** After a CRUD commit, fetching a differential bundle with `base_hash` set to the pre-mutation hash correctly reports additions, modifications, and deletions through OPAL's standard `BundleMaker.make_diff_bundle` mechanism.

## Endpoints

| Method | Path | Levels | Description |
|---|---|---|---|
| `GET` | `/policy` | L0–L3 | Fetch policy bundle from tracked Git repository |
| `GET` | `/policy/modules` | — | List all Rego policy modules in the repository |
| `POST` | `/policy/modules` | L0–L3 | Create a new Rego policy module (Git commit); optional L1-L3 extension/goex parameters use the same endpoint |
| `PUT` | `/policy/modules` | L0–L3 | Update an existing Rego policy module (Git commit); optional L1-L3 extension/goex parameters use the same endpoint |
| `DELETE` | `/policy/modules` | — | Delete a Rego policy module (Git commit) |
| `POST` | `/data/config` | L0–L3 | Publish data update to OPAL clients |
| `GET` | `/data/config` | — | Get base data source configuration |
| `GET` | `/statistics` | L0–L3 | Get server statistics (clients, topics, replicas) |
| `GET` | `/stats` | — | Get brief statistics (client/server counts) |
| `POST` | `/token` | — | Generate JWT access token |
| `GET` | `/healthcheck` | — | Server health check |
| `POST` | `/symphony/code_extension` | L4 | Freeform code generation from capabilities (L4 entry point) |
| `GET` | `/symphony/goex/records` | — | GoEx SRE endpoints |

> **L4 note:** At L4 the agent sends a freeform prompt to `POST /symphony/code_extension`, which generates and executes code using registered capabilities directly. Ordinary endpoint and utility MCP tools are not exposed at L4; equivalent behavior must be reached through code-extension capabilities.

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

For server-side code generation (L2/L3 endpoint extensions and L4 code-extension flows), start the server with:

```bash
OPAL_REPO_WATCHER_ENABLED=false OPAL_PUBLISHER_ENABLED=false OPAL_STATISTICS_ENABLED=true \
OPAL_POLICY_REPO_REUSE_CLONE_PATH=true \
SYMPHONY_CODEGEN_PROVIDER=gemini SYMPHONY_CODEGEN_MODEL=gemini-2.5-flash \
  uv run python -m uvicorn opal_server.main:app --reload --timeout-keep-alive 300
```

## Running On Kubernetes (kind / EKS / GKE)

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
OPAL_BASE_URL=http://PUBLIC_LB:8080 \
OPAL_ADMIN_BASE_URL=http://127.0.0.1:8001 \
OPAL_NAMESPACE=symphony-opal \
./scripts/run_opal_tests.sh anthropic haiku

OPAL_BASE_URL=http://PUBLIC_LB:8080 \
OPAL_ADMIN_BASE_URL=http://127.0.0.1:8001 \
OPAL_NAMESPACE=symphony-opal \
./scripts/run_opal_goex_tests.sh anthropic haiku
```

`run_opal_tests.sh` now resets benchmark state before each standard-suite job
via `POST /symphony/benchmark/reset`. Use `--no-reset-state` or
`OPAL_BENCHMARK_RESET=0` only when you explicitly want to reuse prior state.

Terraform-based cloud deployments for both EKS and GKE now live under:

```bash
scripts/opal/terraform/
```

See [`scripts/opal/terraform/README.md`](../../scripts/opal/terraform/README.md) from the repo root for the full AWS and GCP flow, public URLs, admin port-forwarding, and benchmark env exports.

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

## Benchmark Cases

The exact prompts live in [`TESTING_PLAN.md`](TESTING_PLAN.md) and the runner
scripts. The benchmark now uses stateful outage scenarios instead of direct
answer extraction.

### Standard suite

Run the standard matrix:

```bash
./scripts/run_opal_tests.sh --level L0,L1,L2,L3,L4 anthropic haiku
```

Scenarios:

- `opal/test1`: the active outage gate is too restrictive; the agent must find
  the correct existing production break-glass policy and apply that logic to
  `incident/payments_outage_gate.rego`
- `opal/test2`: the agent must inspect live OPAL statistics, decide whether to
  trigger the emergency rollout, and publish the production-safe incident
  access, directory, and feature-flag updates
- `opal/test3`: no existing module covers the recovery action; the agent must
  create `incident/payments_replica_promote_hotfix.rego`

Standard-suite verification:

- benchmark state is reset before every job via `POST /symphony/benchmark/reset`
- the runner checks the broken baseline before launching the agent
- after the run, the runner verifies live client state directly
- strict fenced JSON is still required and compared against the expected
  summary fixture

### GoEx suite

Run the GoEx matrix:

```bash
./scripts/run_opal_goex_tests.sh --levels "L0 L1 L2 L3 L4" --cases "test2 test1 test3" anthropic haiku
```

GoEx cases:

- `test2`: existing cache-failover hotfix scenario
- `test1`: update `incident/payments_outage_gate.rego` through GoEx and then
  reverse it
- `test3`: create `incident/payments_replica_promote_hotfix.rego` through GoEx
  and then reverse it

GoEx verification:

- reset benchmark state before each case
- capture exactly one GoEx record in `L1` to `L4`
- verify the live state change on OPAL clients
- reverse the record
- verify the original baseline is restored

### Cross-model comparison

Both suites export benchmark diagnostics under `stats/opal/` and
`stats/opal/goex/`, including normalized token and latency fields plus L0 delta
fields. That lets you compare:

- L0 baseline vs extension-assisted levels
- providers and models
- reasoning vs no-reasoning
- direct mutation vs GoEx mutation

### GoEx SRE endpoints

After a GoEx run:

```bash
curl http://127.0.0.1:8000/symphony/goex/records
curl "http://127.0.0.1:8000/symphony/goex/records?status=executed"
curl http://127.0.0.1:8000/symphony/goex/records/<RECORD_ID>
curl -X POST http://127.0.0.1:8000/symphony/goex/records/<RECORD_ID>/reverse
```
