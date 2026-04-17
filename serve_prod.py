#!/usr/bin/env python
"""Production ASGI launcher for the OPAL example."""

from __future__ import annotations

import os

import uvicorn


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_mount_path(path: str | None) -> str:
    if not path:
        return "/mcp"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


def create_app():
    port = int(os.getenv("PORT", "8000"))
    os.environ.setdefault("API_URL", f"http://127.0.0.1:{port}")

    from opal_server.main import app as api_app

    if _env_flag("MCP_HTTP_ENABLED", True):
        from mcp_server import mcp

        mount_path = _normalize_mount_path(os.getenv("MCP_MOUNT_PATH"))
        api_app.mount(mount_path, mcp.sse_app())

    return api_app


def main() -> None:
    os.environ.setdefault("SYMPHONY_CODEGEN_PROVIDER", "websocket")
    uvicorn.run(
        "serve_prod:create_app",
        factory=True,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        workers=max(1, int(os.getenv("WEB_CONCURRENCY", "1"))),
        log_level=os.getenv("LOG_LEVEL", "info"),
        timeout_keep_alive=int(os.getenv("TIMEOUT_KEEP_ALIVE", "300")),
        proxy_headers=_env_flag("PROXY_HEADERS", True),
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
        access_log=_env_flag("ACCESS_LOG", True),
        server_header=False,
    )


if __name__ == "__main__":
    main()
