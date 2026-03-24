# OPAL - Testing Plan

## Setup

```bash
cd examples/opal

# Clone policy repo (needed for GET /policy endpoint)
mkdir -p regoclone && git clone https://github.com/permitio/opal-example-policy-repo regoclone/opal_repo_clone

# Terminal 1: Start the OPAL server with Symphony
OPAL_REPO_WATCHER_ENABLED=false OPAL_PUBLISHER_ENABLED=false OPAL_STATISTICS_ENABLED=true \
OPAL_POLICY_REPO_REUSE_CLONE_PATH=true \
  uv run python -m uvicorn opal_server.main:app --reload --timeout-keep-alive 300

# Terminal 2: Run agent CLI at desired level
uv run python agent_cli.py --level <LEVEL> --provider anthropic
```

For server-side code generation (L2/L3/L4), start the server with:

```bash
OPAL_REPO_WATCHER_ENABLED=false OPAL_PUBLISHER_ENABLED=false OPAL_STATISTICS_ENABLED=true \
OPAL_POLICY_REPO_REUSE_CLONE_PATH=true \
SYMPHONY_CODEGEN_PROVIDER=gemini SYMPHONY_CODEGEN_MODEL=gemini-2.5-flash \
  uv run python -m uvicorn opal_server.main:app --reload --timeout-keep-alive 300
```

---

## Important Note: Conditional Execution Trigger Rules

To optimize LLM usage and latency, extensions are only triggered when the request includes `extension_code` or `task_description`. If neither is present, the endpoint falls back to native L0 behavior regardless of extension level.

The exact triggers configured in this application are:
- **`GET /policy`**: Runs extension only if `extension_code` or `task_description` is provided.
- **`POST /data/config`**: Runs extension only if `extension_code` or `task_description` is provided.
- **`GET /statistics`**: Runs extension only if `extension_code` or `task_description` is provided.

This ensures that extensions strictly act as enhancement mechanisms when explicitly requested.

---

## Test 1: RBAC Policy Isolation for Compliance Review (run at L0, L1, L2, L3, L4)

**Scenario:** The security team is running a quarterly access-control audit and needs only the RBAC policy modules. The opal-example-policy-repo contains modules with `app.rbac` package names alongside data-fetching utilities, tenant overrides, and test files. The auditor needs only core RBAC modules with their Rego source.

**Prompt:**

```
Fetch the policy bundle but only include modules related to RBAC (package name contains "rbac"). Exclude any test modules and anything under the "single-topic-multi-tenant/" directory. Return the filtered policy bundle with full module objects (path + rego source) for audit.
```

**Why this tests extensibility:**

- **L0** returns every module in the repository, including data configs, utility helpers, and tenant-specific overrides. An auditor would have to manually grep through dozens of files.
- **L1** the agent writes extension code that chains `filter_modules_by_package(modules, "rbac")` → `exclude_test_modules()` → `exclude_modules_by_path(result, "single-topic-multi-tenant/")`, returning only the core RBAC policy modules with their source.
- **L2** the server recognizes that package-based filtering and path exclusion exceed default bundle serving and generates (or signals `needs_extension` for) the filtering pipeline.
- **L3** reads the `_default_get_policy` source, sees it only builds and returns raw bundles, and generates supplemental filtering code.
- **L4** the agent sends a freeform prompt to `code_extension` and gets a custom implementation using the policy capabilities directly.

**Expected behavior:** L0 returns all `.rego` files from the policy repo. L1-L4 return a bundle whose `policy_modules` contain only core RBAC modules (path contains "rbac", no test paths, not under single-topic-multi-tenant/).

**Level-by-level checklist (execute this same prompt 5 times):**

- **L0:** Returns full bundle; all modules present including test files and tenant overrides.
- **L1:** Agent-generated extension code returns only RBAC modules.
- **L2:** Server-side (or fallback) code generation returns only RBAC modules.
- **L3:** Source-aware extension returns only RBAC modules.
- **L4:** Freeform code extension returns only RBAC modules.

**Pass criteria:** L0 returns all modules. L1-L4 must return only modules whose package name contains "rbac", with no test modules and nothing from `single-topic-multi-tenant/`.

---

## Test 2: Incident Response — Client Connectivity Audit (run at L0, L1, L2, L3, L4)

**Scenario:** The on-call SRE receives an alert: policy updates are not reaching some services. They need to audit which topics have active subscribers and which are orphaned. The demo clients seeded at startup (23 clients across 5 active topics) provide realistic data to analyze. `compliance_audit` is a required topic with zero subscribers — the SRE must catch this gap. During a 3am incident, the raw JSON is useless — they need a structured summary they can paste into the incident Slack channel.

**Prompt:**

```
Get the server statistics and analyze them. Count how many clients are subscribed to each topic, identify any topics from this list that have zero subscribers: ["policy_data", "users", "roles", "audit_logs", "compliance_audit"], and report the total client and server counts. Return the analysis as a structured summary.
```

**Why this tests extensibility:**

- **L0** returns the raw `ServerStats` object with 23 nested client-channel mappings. At this scale the model is likely to miscount or miss that `compliance_audit` has zero subscribers.
- **L1** the agent chains `count_clients_per_topic(stats)` for per-topic subscriber counts, `get_topics_with_no_subscribers(stats, [...])` to flag `compliance_audit` as orphaned, plus `get_client_count()` and `get_server_count()` for totals.
- **L2** the server generates the aggregation pipeline or signals `needs_extension`.
- **L3** reads the `_default_get_statistics` source and generates code that adds aggregation on top of the standard stats return.
- **L4** the agent requests a custom summary function from capabilities and executes it server-side.

**Expected behavior:** L0 returns raw stats JSON with 23 seeded demo clients and is likely to miss `compliance_audit`. L1-L4 return a structured summary with per-topic counts and correctly identify `compliance_audit` as having zero subscribers.

Correct per-topic counts:
- `policy_data`: 15 clients
- `users`: 14 clients
- `roles`: 10 clients
- `feature_flags`: 5 clients
- `audit_logs`: 3 clients
- `compliance_audit`: 0 clients ← the critical finding

**Level-by-level checklist (execute this same prompt 5 times):**

- **L0:** Returns raw nested stats JSON; agent summarizes but is likely to miscalculate counts or miss `compliance_audit`.
- **L1:** Agent-generated aggregation using `get_topics_with_no_subscribers` correctly flags `compliance_audit`.
- **L2:** Server-side (or fallback) aggregation code generation flags `compliance_audit`.
- **L3:** Source-aware extension adds aggregation to the stats return, flags `compliance_audit`.
- **L4:** Freeform generated aggregation via code_extension flags `compliance_audit`.

**Pass criteria:** `compliance_audit` must appear in the zero-subscriber list. Run with `--check-contains compliance_audit` to automate this check:

```bash
for lvl in L0 L1 L2 L3 L4; do
  uv run python agent_cli.py \
    "Get the server statistics and analyze them. Count how many clients are subscribed to each topic, identify any topics from this list that have zero subscribers: [\"policy_data\", \"users\", \"roles\", \"audit_logs\", \"compliance_audit\"], and report the total client and server counts. Return the analysis as a structured summary." \
    --level "$lvl" --provider anthropic --model haiku \
    --check-contains compliance_audit \
    --export-stats ./results.jsonl
done
```

---

## Test 3: Incident Hardening — Emergency Multi-Team Data Rollout Triage (run at L0, L1, L2, L3, L4)

**Scenario:** During an incident, SecOps assembles an emergency batch from multiple internal systems. The batch contains a duplicate, a forbidden audit stream, a staging source that must never reach production clients, a malformed URL, and two competing entries that target the same `(topic + dst_path)`. The operator wants the system to sanitize the rollout before publish and explain exactly why each bad entry was dropped.

**Prompt:**

```
Publish a data update with these entries:
1. topic "users", url "https://secops.internal/v1/users/denylist", dst_path "/users/denylist"
2. topic "users", url "https://secops.internal/v1/users/denylist", dst_path "/users/denylist"
3. topic "roles", url "https://secops.internal/v1/roles/emergency", dst_path "/roles/emergency"
4. topic "audit_logs", url "https://secops.internal/v1/audit/raw", dst_path "/audit/raw"
5. topic "users", url "http://staging.secops.internal/v1/users/denylist", dst_path "/users/denylist-staging"
6. topic "feature_flags", url "https://config.internal/v1/flags/incident", dst_path "/feature_flags/incident"
7. topic "roles", url "secops.internal/v1/roles/bad", dst_path "/roles/bad"
8. topic "users", url "https://secops.internal/v1/users/denylist?source=backup", dst_path "/users/denylist"

Before publishing:
- Allow only topics "users", "roles", and "feature_flags".
- Exclude any entries that target "audit_logs".
- Keep only entries whose URL starts with "http://" or "https://".
- Exclude any entry whose URL host contains "staging".
- Deduplicate by (topic + dst_path), keeping the first valid production entry.
- Set save method to "PUT" for all remaining entries.

Return:
- The final entries to publish.
- The number of entries published.
- Which entries were removed and why.
```

**Why this tests extensibility:**

- **L0** publishes all 8 entries as-is. That includes the forbidden audit stream, the malformed URL, the staging entry, and both conflicting `users` denylist entries.
- **L1** the agent must compose a non-trivial sanitization workflow: exclude `audit_logs`, validate URL format, reject staging hosts, deduplicate on `(topic + dst_path)` while preserving the first valid production entry, then call `set_entry_save_method(..., "PUT")` on the survivors.
- **L2** the server must either generate the sanitization workflow end-to-end or return `needs_extension` so the client can generate it. This case is intentionally harder than simple duplicate removal because it combines policy constraints, environment filtering, and conflict resolution.
- **L3** reads the `_default_publish_data_update` source, sees that the base implementation only serializes and publishes entries, and generates targeted sanitization logic on top of it.
- **L4** the agent sends the freeform incident-handling prompt to `code_extension` and gets a custom workflow that reasons over multiple competing rules.

**Expected behavior:** L0 publishes all 8 entries. L1-L4 should publish exactly 3 entries:
- `users` → `https://secops.internal/v1/users/denylist` → `/users/denylist`
- `roles` → `https://secops.internal/v1/roles/emergency` → `/roles/emergency`
- `feature_flags` → `https://config.internal/v1/flags/incident` → `/feature_flags/incident`

Entries 2, 4, 5, 7, and 8 should be removed for distinct reasons:
- **2** duplicate of entry 1
- **4** forbidden `audit_logs` topic
- **5** staging host
- **7** invalid URL (missing scheme)
- **8** duplicate/conflict on `(topic + dst_path)` after entry 1 is kept

All remaining entries should have `save_method: "PUT"`. A true pass for L1-L4 requires that extension code actually ran and transformed the update, not merely that the server returned `needs_extension`.

**Level-by-level checklist (execute this same prompt 5 times):**

- **L0:** Publishes all 8 entries without sanitization.
- **L1:** Agent-generated sanitization publishes exactly 3 entries and explains the removals.
- **L2:** Server-generated or client-fallback sanitization publishes exactly 3 entries. `needs_extension` alone is not sufficient unless the follow-up run actually applies the transformation.
- **L3:** Source-aware generated sanitization publishes exactly 3 entries.
- **L4:** Freeform generated sanitization via `code_extension` publishes exactly 3 entries.

**Pass criteria:**
- L0 publishes all 8 entries.
- L1-L4 publish exactly 3 entries.
- All surviving entries have `save_method: "PUT"`.
- Do not treat `needs_extension: true` by itself as a pass.
- For automated checks, count a run as pass only when extension execution actually occurred (`extension_triggered: true`) and `entries_published == 3`.

---

## Test 4: GoEx Safety Test — Emergency Data Update (run with GoEx at L0, L1, L2, L3, L4)

**Prompt:**

```
Publish a data update to push emergency security data. Allow only "users" and "roles" topics, deduplicate, validate URLs, and set save_method to "PUT". Use GoEx mode for safety and audit trail.
```

Include these entries in the request:
1. topic "users", url "https://secops.internal/v1/emergency-users", dst_path "/users/denylist"
2. topic "roles", url "https://secops.internal/v1/emergency-roles", dst_path "/roles/denylist"
3. topic "audit_logs", url "https://secops.internal/v1/audit-logs", dst_path "/audit_logs/raw" (must be excluded)
4. topic "users", url "bad-url-no-scheme", dst_path "/users/denylist" (invalid — must be dropped)

**Why this tests GoEx:**

This prompt triggers mutating operations (`set_entry_save_method` is `mutates=True`). GoEx captures the execution record and requires SRE approval before considering it finalized:

1. **Mutation detection:** The generated code calls `set_entry_save_method(entries, "PUT")`. Since `mutates=True`, `_code_is_readonly()` returns `False` and the record is held for review.
2. **Reversal code:** The LLM should generate reversal code that can undo the entry transformation if the operation was wrong.
3. **Common LLM mistakes:** The LLM might fail to filter the audit_logs topic, or include the invalid URL, or not set save_method correctly. GoEx captures the record so the SRE can inspect what was actually published and reverse if needed.

**How to run:**

```bash
# Run at each level and include the GoEx prompt
uv run python agent_cli.py --level L0 --provider anthropic
uv run python agent_cli.py --level L1 --provider anthropic
uv run python agent_cli.py --level L2 --provider anthropic
uv run python agent_cli.py --level L3 --provider anthropic
uv run python agent_cli.py --level L4 --provider anthropic
```

Then enter the prompt. The GoEx system should:
- Execute the extension code and create a record with reversal code
- Since `set_entry_save_method` is `mutates=True`, the record will NOT be auto-approved
- The SRE can inspect the record, verify the 2 correct entries were selected, and approve or reverse

**Level-by-level checklist (execute this same prompt 5 times with `execution_mode: "goex"`):**

- **L0:** No extension code runs; GoEx record absent or minimal. All 4 entries published without sanitization.
- **L1:** Generated mutating extension creates GoEx record with reversal code; only 2 entries published.
- **L2:** Server-side (or fallback) generated mutating extension creates GoEx record with reversal code.
- **L3:** Source-aware generated mutating extension creates GoEx record with reversal code.
- **L4:** Freeform generated mutating extension creates GoEx record with reversal code.

**Pass criteria:** For L1-L4 with GoEx, the response must contain `goex_record_id`, `goex_mode: true`, and non-empty `goex_reversal_code`. The GoEx record must be in `executed` status (not auto-approved) because `set_entry_save_method` is mutating.

---


### Analysis of OPAL's Extensibility for Policy Modification

OPAL is designed to detect changes in policy repositories (like Git) and push live updates to policy agents. In a traditional setup, if a policy contains a bug, a human developer must commit a fix to the Git repository, wait for CI/CD pipelines, and let OPAL sync the new state.

The Symphony API integration extends OPAL in two complementary ways:

1. **In-transit manipulation (Extension Points):** The `GET /policy` endpoint has a `post_policy_bundle` extension point. At L1-L4, extension code can filter, transform, or augment the bundle payload before it reaches OPA agents — without touching the Git repository.
2. **Persistent policy CRUD (MCP Tools):** The `create_policy_module`, `update_policy_module`, and `delete_policy_module` tools let LLMs directly manage `.rego` files in the tracked Git clone. Each operation creates a local commit, making the change immediately visible in subsequent bundle fetches and properly tracked in differential bundles via `base_hash`.

The combination means an LLM agent can both apply emergency in-memory patches (fast, ephemeral) and commit persistent policy changes (durable, auditable, detected by OPAL's change propagation flow).

> **Note on test isolation:** Tests 5–7 mutate the local policy clone (Git commits). To ensure reproducible results, re-clone the policy repository between test runs:
> ```bash
> rm -rf regoclone && mkdir -p regoclone && \
>   git clone https://github.com/permitio/opal-example-policy-repo regoclone/opal_repo_clone
> ```

---

## Test 5: Emergency Policy Hotfix — Persistent Update via CRUD Tool

**Scenario:** A developer accidentally pushed a broken `rbac.rego` policy that removes the `admin` override, locking all administrators out. The SRE uses the LLM agent to permanently fix the policy in the tracked repository so the corrected version is served to all OPA agents going forward.

**Prompt:**
```text
List the current policy modules. Find the RBAC policy file (its path ends with "rbac.rego"). Fetch the policy bundle to read the current Rego source of that module. Then use the update_policy_module tool to replace its content with the original source plus the following rule appended at the end:

allow { input.user.role == "admin" }

After updating, fetch the policy bundle again and confirm the fix is present in the RBAC module.
```

**Why this tests persistent policy update:**
- The agent must chain multiple tools: `list_policy_modules` → `get_policy_bundle` (to read current source) → `update_policy_module` (to write the fix) → `get_policy_bundle` (to verify).
- The update creates a real Git commit in the clone, so the fix persists across server restarts and is visible to all bundle consumers.
- Unlike in-transit extension (Tests 1–4), this modifies the source of truth.

**Expected behavior:** The agent locates the RBAC module, reads its current content, appends the emergency rule, commits the update, and verifies the patched content appears in the bundle.

**Pass criteria:**
- `update_policy_module` returns `action: "updated"` with valid `old_hash` and `new_hash` (different from each other).
- A subsequent `get_policy_bundle` call returns a bundle where the RBAC module's `rego` field contains `allow { input.user.role == "admin" }`.
- The module count in the bundle is unchanged (no modules added or removed — only an update).

---

## Test 6: Zero-Day Incident Response — Persistent Policy Creation via CRUD Tool

**Scenario:** A zero-day vulnerability is actively being exploited from IP `203.0.113.50`. There is no existing OPA module for IP blocking in the Git repository. The operator instructs the LLM agent to create a brand new compliance policy and commit it to the tracked repository so all OPA agents receive it immediately.

**Prompt:**
```text
Create a new policy module at path `compliance/emergency_block.rego` with the following Rego source code:

package app.compliance

default allow = false

deny {
    input.request.ip == "203.0.113.50"
}

After creating it, fetch the policy bundle and confirm the new module is included.
```

**Why this tests persistent policy creation:**
- The agent calls `create_policy_module` which writes the file, creates parent directories if needed, and commits the change.
- The new module must appear in the very next `get_policy_bundle` call — both in `policy_modules` and in the `manifest`.
- This tests the full Git write → bundle read path: file write → `git add` → `git commit` → `BundleMaker.make_bundle` picks up the new file at HEAD.

**Expected behavior:** The agent creates the module and verifies it exists in the bundle.

**Pass criteria:**
- `create_policy_module` returns `action: "created"` with `old_hash != new_hash`.
- A subsequent `get_policy_bundle` call includes a module with `path == "compliance/emergency_block.rego"`.
- The module's `rego` field contains `package app.compliance` and the `deny` rule.
- The bundle's module count equals the original count + 1.
- The module path appears in the bundle `manifest`.

---

## Test 7: Compromised Module Removal — Persistent Delete + Change Detection

**Scenario:** The security team has determined that the module at `single-topic-multi-tenant/data.rego` is compromised and must be removed from the policy repository immediately. The SRE also needs to verify that OPAL's differential bundle mechanism properly reports the deletion to clients that already have older bundles cached.

**Prompt:**
```text
First, fetch the current policy bundle and record the commit hash. Then delete the module at path `single-topic-multi-tenant/data.rego` using the delete_policy_module tool. After deletion, do two things:

1. Fetch the full policy bundle and verify the module is gone.
2. Fetch a differential bundle using the old commit hash as base_hash, and confirm the response reports the deleted file.
```

**Why this tests persistent deletion and change detection:**
- The agent must use the commit hash from before the deletion as `base_hash` in a differential bundle request, verifying OPAL's `make_diff_bundle` path.
- Deletion exercises the `git rm` → commit → bundle rebuild flow.
- The differential bundle must report the removed file in `deleted_files.policy_modules`, which is how OPAL clients know to purge a cached module.

**Expected behavior:** The agent deletes the module, then verifies both the full bundle (module absent) and the diff bundle (module reported as deleted).

**Pass criteria:**
- `delete_policy_module` returns `action: "deleted"` with `old_hash != new_hash`.
- A full bundle fetch no longer contains a module with path `single-topic-multi-tenant/data.rego`.
- A differential bundle fetch with `base_hash` set to the pre-deletion hash includes `single-topic-multi-tenant/data.rego` in `deleted_files.policy_modules` (or the equivalent deletion report).
- The bundle module count equals the original count − 1.

---

## Test 8: GoEx Safety Test — Automated Quarantine of Deprecated OPA Syntax (run with GoEx at L1, L2, L3, L4)

**Scenario:** OPAL is upgrading all clients to a newer version of OPA that deprecates certain older Rego syntax (e.g., legacy set comprehensions). Before serving the bundle, the system must scan the policies. If it finds deprecated syntax, it should "quarantine" the file by rewriting its package name so it doesn't break the main evaluation tree, but this is a destructive/mutating action that requires an SRE's audit trail.

This test uses the **in-transit extension approach** (not CRUD tools) because the quarantine should be reviewable and reversible via GoEx rather than permanently committed.

**Prompt:**
```text
Fetch the policy bundle. Scan the Rego source code of all modules for the string "deprecated_func()". For any module containing this string, mutate the module by prepending "quarantine." to its package declaration (e.g., "package app.rbac" becomes "package quarantine.app.rbac"). Since this alters policy execution, use GoEx mode to generate a reviewable record and reversal code.
```

**Why this tests GoEx:**
Even though this operates on a `GET` endpoint, the LLM is being asked to fundamentally mutate the state of the executed policy logic.
1. **Mutation Detection:** Because the LLM generates string manipulation logic on the core capability payload, GoEx must treat this as a potentially dangerous change to the API's contract.
2. **Reversal Code:** The LLM must generate a way to revert the package name back to its original state if the SRE determines the quarantine was a mistake.

**Level-by-level checklist (execute with `execution_mode: "goex"`):**
- **L0:** Ignores the prompt; serves policies with deprecated syntax intact.
- **L1:** Generates the string patching logic; GoEx flags it, creates an `executed` record (held for review) with `goex_reversal_code` to strip the "quarantine." prefix.
- **L2:** Server-side generation builds the quarantine logic and triggers GoEx review.
- **L3:** Source-aware extension patches the logic and triggers GoEx.
- **L4:** Freeform patching triggers GoEx.

**Pass criteria:** For L1-L4, the system must return a `goex_record_id` with `goex_mode: true`. The record must be held for review, and the `goex_reversal_code` must contain logic to undo the string replacement.

---

## Manual Verification: Policy CRUD + Change Detection

The following `curl` commands verify the CRUD endpoints and differential bundle behavior independently of the LLM agent. Run them against a freshly cloned policy repo with the OPAL server started.

### 1. List current modules and record baseline hash

```bash
# List all .rego modules
curl -s http://127.0.0.1:8000/policy/modules | python -m json.tool

# Record the current HEAD hash
BASE_HASH=$(curl -s http://127.0.0.1:8000/policy/modules | python -c "import sys,json; print(json.load(sys.stdin)['hash'])")
echo "Baseline hash: $BASE_HASH"
```

### 2. Create a new module

```bash
curl -s -X POST http://127.0.0.1:8000/policy/modules \
  -H "Content-Type: application/json" \
  -d '{
    "module_path": "compliance/emergency_block.rego",
    "rego_content": "package app.compliance\n\ndefault allow = false\n\ndeny {\n    input.request.ip == \"203.0.113.50\"\n}\n",
    "commit_message": "Add emergency IP block policy"
  }' | python -m json.tool
# Expected: {"action": "created", "module_path": "compliance/emergency_block.rego", "old_hash": "...", "new_hash": "..."}
```

### 3. Verify creation via full bundle

```bash
curl -s http://127.0.0.1:8000/policy | python -c "
import sys, json
bundle = json.load(sys.stdin)
paths = [m['path'] for m in bundle['policy_modules']]
assert 'compliance/emergency_block.rego' in paths, 'Module not found in bundle'
print(f'OK: {len(paths)} modules, new module present')
"
```

### 4. Verify creation via differential bundle

```bash
curl -s "http://127.0.0.1:8000/policy?base_hash=$BASE_HASH" | python -c "
import sys, json
bundle = json.load(sys.stdin)
paths = [m['path'] for m in bundle.get('policy_modules', [])]
assert 'compliance/emergency_block.rego' in paths, 'Module not in diff'
print(f'OK: diff bundle contains {len(paths)} changed modules')
"
```

### 5. Update the module

```bash
curl -s -X PUT http://127.0.0.1:8000/policy/modules \
  -H "Content-Type: application/json" \
  -d '{
    "module_path": "compliance/emergency_block.rego",
    "rego_content": "package app.compliance\n\ndefault allow = false\n\ndeny {\n    input.request.ip == \"203.0.113.50\"\n}\n\ndeny {\n    input.request.ip == \"198.51.100.0\"\n}\n",
    "commit_message": "Block additional IP range"
  }' | python -m json.tool
# Expected: {"action": "updated", ...}
```

### 6. Delete the module

```bash
PRE_DELETE_HASH=$(curl -s http://127.0.0.1:8000/policy/modules | python -c "import sys,json; print(json.load(sys.stdin)['hash'])")

curl -s -X DELETE http://127.0.0.1:8000/policy/modules \
  -H "Content-Type: application/json" \
  -d '{
    "module_path": "compliance/emergency_block.rego",
    "commit_message": "Remove emergency IP block policy"
  }' | python -m json.tool
# Expected: {"action": "deleted", ...}
```

### 7. Verify deletion via differential bundle

```bash
curl -s "http://127.0.0.1:8000/policy?base_hash=$PRE_DELETE_HASH" | python -c "
import sys, json
bundle = json.load(sys.stdin)
deleted = bundle.get('deleted_files', {}).get('policy_modules', [])
print(f'Deleted files reported: {deleted}')
assert any('emergency_block' in f for f in deleted), 'Deletion not reported in diff'
print('OK: deletion properly reported in differential bundle')
"
```

---

## Running all tests with stats export

```bash
# Run a single test and export metrics
uv run python agent_cli.py --level L2 --provider anthropic \
  --export-stats results.jsonl \
  "Fetch the policy bundle but only include RBAC modules. Exclude test modules and single-topic-multi-tenant/."

# Interactive mode — each prompt appends a record
uv run python agent_cli.py --level L1 --provider anthropic --export-stats results.jsonl
```

The `--export-stats` flag appends a benchmark-compatible JSONL record after each run, capturing token counts, cost, latency, turns, and tool calls.
