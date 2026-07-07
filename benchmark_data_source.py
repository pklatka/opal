from __future__ import annotations

from fastapi import FastAPI


app = FastAPI(title="OPAL Benchmark Data Source", version="1.0.0")


_BOOTSTRAP = {
    "policy_data": {"seed": "policy_data", "ready": True},
    "incident_access": {"seed": "incident_access", "ready": True},
    "feature_flags": {"seed": "feature_flags", "ready": True},
    "directory_sync": {"seed": "directory_sync", "ready": True},
    "audit_logs": {"seed": "audit_logs", "ready": True},
}


@app.get("/")
@app.get("/healthcheck")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/bootstrap/{topic}")
def bootstrap_topic(topic: str) -> dict:
    return _BOOTSTRAP.get(topic, {"seed": topic, "ready": True})


@app.get("/v1/incidents/sev1/access-grants")
def incident_access_grants() -> dict:
    return {
        "incident_id": "sev1-cache-failover",
        "approver": "sre-control-plane",
        "grants": [
            {"actor": "oncall-primary", "operation": "cache_failover"},
            {"actor": "oncall-secondary", "operation": "cache_failover"},
        ],
    }


@app.get("/v1/directory/emergency/groups")
def directory_groups() -> dict:
    return {
        "groups": [
            "payments-oncall",
            "cache-failover-admins",
        ],
        "source": "directory-sync",
    }


@app.get("/v1/feature-flags/cache-failover")
def cache_failover_flag() -> dict:
    return {
        "flag": "cache_failover",
        "enabled": True,
        "rollout": "global",
    }
