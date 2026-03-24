# OPAL (Open Policy Administration Layer) — Symphony Integration

**Source:** [permitio/opal](https://github.com/permitio/opal)
**Primary testing focus:** L0–L4 level comparison, cross-LLM evaluation, GoEx modes for mutation safety

## Overview

OPAL is an administration layer for Open Policy Agent (OPA) that detects changes to both policy and policy data in real time, pushing live updates to policy agents. This example integrates Symphony's extension framework directly into the OPAL server, adding three extensible endpoints that cover the core use cases: policy bundle serving, data update publishing, and server statistics.

Three Symphony-extended endpoints provide rich test cases covering read-only bundle filtering, statistics aggregation, and mutating data updates. Additionally, a set of **policy CRUD endpoints** allow LLM agents to create, update, and delete `.rego` modules directly in the tracked Git clone — each operation commits locally so changes are immediately visible in subsequent bundle fetches and properly reported in differential bundles.

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
| `deduplicate_entries` | `(entries) → list[dict]` | `False` | Remove duplicates by url+dst_path |
| `filter_entries_by_dst_path` | `(entries, path_prefix) → list[dict]` | `False` | Entries whose dst_path starts with prefix |
| `validate_entry_urls` | `(entries) → list[dict]` | `False` | Keep only entries with valid http(s) URLs |
| `set_entry_save_method` | `(entries, save_method) → list[dict]` | **`True`** | Set save_method (PUT/PATCH) on all entries |

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
- Mutating code (`set_entry_save_method`) → **held for SRE review**

## Extension Points

| Extension Point | Description | Endpoint |
|---|---|---|
| `post_policy_bundle` | Filter, transform, or augment policy bundles before serving | `GET /policy` |
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

### Case A: Policy Bundle Filtering (read-only)

**Prompt:** `"Fetch the policy bundle but only include modules related to RBAC (package containing 'rbac'). Exclude any test modules and anything under the single-topic-multi-tenant/ directory. Return the filtered policy bundle with full module objects (path + rego source)."`

```bash
uv run python agent_cli.py --level L0 "Fetch the policy bundle but only include modules related to RBAC (package containing 'rbac'). Exclude any test modules and anything under the single-topic-multi-tenant/ directory. Return the filtered policy bundle with full module objects (path + rego source)."
uv run python agent_cli.py --level L1 "Fetch the policy bundle but only include modules related to RBAC (package containing 'rbac'). Exclude any test modules and anything under the single-topic-multi-tenant/ directory. Return the filtered policy bundle with full module objects (path + rego source)."
uv run python agent_cli.py --level L2 "Fetch the policy bundle but only include modules related to RBAC (package containing 'rbac'). Exclude any test modules and anything under the single-topic-multi-tenant/ directory. Return the filtered policy bundle with full module objects (path + rego source)."
uv run python agent_cli.py --level L3 "Fetch the policy bundle but only include modules related to RBAC (package containing 'rbac'). Exclude any test modules and anything under the single-topic-multi-tenant/ directory. Return the filtered policy bundle with full module objects (path + rego source)."
uv run python agent_cli.py --level L4 "Fetch the policy bundle but only include modules related to RBAC (package containing 'rbac'). Exclude any test modules and anything under the single-topic-multi-tenant/ directory. Return the filtered policy bundle with full module objects (path + rego source)."
```

| Level | Expected behavior | Features exercised |
|-------|------------------|-------------------|
| **L0** | Returns entire policy bundle with all modules from the repo — test files, tenant overrides, data modules, everything. | Vanilla API, MCP routing |
| **L1** | Sends extension_code that chains `filter_modules_by_package(modules, "rbac")` → `exclude_test_modules()` → `exclude_modules_by_path(result, "single-topic-multi-tenant/")`. Returns only core RBAC modules. | Sandbox execution, capability chaining, read-only auto-approve |
| **L2** | Sends request with task_description. Server generates the same filtering pipeline or signals `needs_extension`. CLI generates code using capability docs. | Two-phase flow, `needs_extension`, server codegen |
| **L3** | Server reads `_default_get_policy` source, generates supplemental filtering code that adds RBAC isolation on top of standard bundle building. | Source code reading, `server_generate_and_execute()` |
| **L4** | Agent sends freeform prompt to `/symphony/code_extension`: filter by rbac package, exclude tests and tenant overrides. | Freeform code generation from capabilities |

### Case B: Statistics Analysis (read-only)

**Prompt:** `"Get server statistics and analyze them. Count how many clients are subscribed to each topic, identify topics with zero subscribers from [policy_data, users, roles, audit_logs], and report total client and server counts. Return a structured summary."`

```bash
uv run python agent_cli.py --level L0 "Get server statistics and analyze them. Count how many clients are subscribed to each topic, identify topics with zero subscribers from [policy_data, users, roles, audit_logs], and report total client and server counts. Return a structured summary."
uv run python agent_cli.py --level L1 "Get server statistics and analyze them. Count how many clients are subscribed to each topic, identify topics with zero subscribers from [policy_data, users, roles, audit_logs], and report total client and server counts. Return a structured summary."
uv run python agent_cli.py --level L2 "Get server statistics and analyze them. Count how many clients are subscribed to each topic, identify topics with zero subscribers from [policy_data, users, roles, audit_logs], and report total client and server counts. Return a structured summary."
```

| Level | Expected behavior | Features exercised |
|-------|------------------|-------------------|
| **L0** | Returns raw `ServerStats` JSON with nested client-channel mappings. The LLM must parse this manually in its response. | Raw nested JSON, manual parsing by agent |
| **L1** | Extension code calls `count_clients_per_topic(stats)` and `get_topics_with_no_subscribers(stats, [...])` plus `get_client_count()` / `get_server_count()` for totals. Returns structured summary dict. | Multi-capability composition, aggregate computation |
| **L2** | Server determines raw stats is insufficient for structured analysis, generates aggregation pipeline or signals `needs_extension`. | Two-phase with semantic analysis |

### Case C: Data Update Sanitization (mutating)

**Prompt:** `"Publish a data update with these entries: 1) topic 'users', url 'https://api.example.com/users', dst_path '/users'. 2) topic 'users', url 'https://api.example.com/users', dst_path '/users' (duplicate). 3) topic 'audit_logs', url 'api.example.com/audit-logs', dst_path '/audit_logs' (invalid URL). 4) topic 'roles', url 'https://api.example.com/roles', dst_path '/roles'. Before publishing: exclude audit_logs topic, deduplicate by url+dst_path, validate URLs (http/https only), set save_method to PUT. Explain what was filtered."`

```bash
uv run python agent_cli.py --level L0 "Publish a data update with these entries: 1) topic users, url https://api.example.com/users, dst_path /users. 2) topic users, url https://api.example.com/users, dst_path /users (duplicate). 3) topic audit_logs, url api.example.com/audit-logs (invalid), dst_path /audit_logs. 4) topic roles, url https://api.example.com/roles, dst_path /roles. Before publishing: exclude audit_logs, deduplicate, validate URLs, set save_method to PUT."
uv run python agent_cli.py --level L1 "Publish a data update with these entries: 1) topic users, url https://api.example.com/users, dst_path /users. 2) topic users, url https://api.example.com/users, dst_path /users (duplicate). 3) topic audit_logs, url api.example.com/audit-logs (invalid), dst_path /audit_logs. 4) topic roles, url https://api.example.com/roles, dst_path /roles. Before publishing: exclude audit_logs, deduplicate, validate URLs, set save_method to PUT."
uv run python agent_cli.py --level L2 "Publish a data update with these entries: 1) topic users, url https://api.example.com/users, dst_path /users. 2) topic users, url https://api.example.com/users, dst_path /users (duplicate). 3) topic audit_logs, url api.example.com/audit-logs (invalid), dst_path /audit_logs. 4) topic roles, url https://api.example.com/roles, dst_path /roles. Before publishing: exclude audit_logs, deduplicate, validate URLs, set save_method to PUT."
```

| Level | Expected behavior | Features exercised |
|-------|------------------|-------------------|
| **L0** | Publishes all 4 entries as-is. Invalid URL and duplicate reach OPA clients; audit_logs suppression is ignored. | Vanilla publish, no sanitization |
| **L1** | Extension code chains `exclude_entries_by_topic(entries, "audit_logs")` → `deduplicate_entries()` → `validate_entry_urls()` → `set_entry_save_method(result, "PUT")`. Publishes only 2 safe entries. | Capability chaining, sanitization pipeline, `mutates=True` detection |
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

## Test Plan 3: GoEx Modes Comparison (Mutation Safety Cases)

These prompts test GoEx's safety mechanisms for mutating data updates.

### Case G1: Audit Topic Suppression (safe filtering)

**Prompt:** `"Publish a data update that excludes the audit_logs topic and validates all URLs. Use GoEx mode."`

```bash
uv run python agent_cli.py --level L1 "Publish a data update with entries for users, roles, and audit_logs topics. Exclude audit_logs, validate all URLs are http/https, use GoEx mode."
```

**What to observe:**
- Extension code calls `exclude_entries_by_topic()` and `validate_entry_urls()` — no `set_entry_save_method` mutation
- `_code_is_readonly()` returns `True` (filtering caps are all `mutates=False`)
- GoEx status: `auto_approved` (not held for review)

### Case G2: Save Method Change (mutation safety)

**Prompt:** `"Publish a data update. Change the save_method to PUT for all entries. Use GoEx for safety."`

```bash
uv run python agent_cli.py --level L1 "Publish a data update with entries for users and roles topics. Set save_method to PUT on all entries. Use GoEx mode."
```

**What to observe:**
- Extension code calls `set_entry_save_method(entries, "PUT")` which is `mutates=True`
- `_code_is_readonly()` returns `False` because `set_entry_save_method` is referenced
- GoEx status: `executed` (held for SRE review, NOT auto-approved)
- Reversal code is available

### Case G3: Read-Only Statistics Analysis (safe aggregation)

**Prompt:** `"Get server statistics and count clients per topic. Use GoEx mode."`

```bash
uv run python agent_cli.py --level L1 "Get server statistics and count clients per topic. Use GoEx mode."
```

**What to observe:**
- Code only calls `count_clients_per_topic(stats)` — all `mutates=False`
- `_code_is_readonly()` returns `True`
- GoEx status: `auto_approved`

### Case G4: Emergency Data Sanitization (full pipeline + GoEx)

**Prompt:** `"Publish an emergency data update. Allow only users and roles topics, deduplicate, validate URLs, set save_method to PUT. Use GoEx for audit trail."`

```bash
uv run python agent_cli.py --level L1 "Publish an emergency data update: 1) topic users, url https://secops.internal/v1/users, dst_path /users. 2) topic roles, url https://secops.internal/v1/roles, dst_path /roles. 3) topic audit_logs, url https://secops.internal/v1/audit, dst_path /audit (must be excluded). Allow only users/roles, deduplicate, validate URLs, set save_method to PUT. Use GoEx mode."
```

**What to observe:**
- Code chains multiple caps including `set_entry_save_method` (`mutates=True`)
- GoEx status: `executed` (held because of mutation)
- `goex_reversal_code` available for rollback if needed

### GoEx SRE Endpoint Verification

After running GoEx cases, verify records via the API:

```bash
# List all GoEx records
curl http://127.0.0.1:8000/symphony/goex/records

# Filter by status
curl "http://127.0.0.1:8000/symphony/goex/records?status=executed"

# Get specific record
curl http://127.0.0.1:8000/symphony/goex/records/<RECORD_ID>

# Approve a record
curl -X POST http://127.0.0.1:8000/symphony/goex/records/<RECORD_ID>/approve

# Reverse a record
curl -X POST http://127.0.0.1:8000/symphony/goex/records/<RECORD_ID>/reverse
```
