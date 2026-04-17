# Benchmark policy repository (Symphony OPAL)

Deterministic Rego modules for the OPAL focused benchmark suite. Seeded by
`scripts/start_opal.sh` into `examples/opal/regoclone/opal_repo_clone/`.

This fixture is intentionally shaped like a small production policy repo:

- `incident/break_glass.rego` is the single correct module for the sev-1
  break-glass lookup benchmark.
- `incident/oncall_readonly.rego` and
  `incident/vendor_emergency_access.rego` are realistic near-misses.
- `sandbox/*` and `tenants/playground/*` simulate non-production or
  tenant-scoped overrides that operators should exclude.
- `shared/*` and `tests/*` provide helpers and test modules that should not
  be returned as the final answer.

Do not rely on network clones for CI or same-VPS runs; this tree is the source
of truth for the benchmark.
