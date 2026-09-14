"""Cloud entrypoint for the QWeather MCP server.

Adds deployment-safe defaults around the upstream MCP implementation:
- binds to 0.0.0.0 and honors PORT
- requires a static Bearer token for remote MCP access
- exposes an unauthenticated /health endpoint
- hides paid QWeather tropical-cyclone tools by default
- suppresses upstream INFO logs during import so API key prefixes are not logged
- stays alive with a diagnostic health endpoint when deployment config is incomplete
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Iterable

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("hefeng_qweather_mcp.cloud")

_PAID_TOOL_NAMES = {
    "get_storm_list",
    "get_storm_track",
    "get_storm_forecast",
}


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _deployment_config_errors() -> list[str]:
    """Return human-readable deployment configuration problems without secrets."""

    errors: list[str] = []

    if not os.environ.get("HEFENG_API_HOST", "").strip():
        errors.append("HEFENG_API_HOST is missing")

    api_key = os.environ.get("HEFENG_API_KEY", "").strip()
    if not api_key:
        project_id = os.environ.get("HEFENG_PROJECT_ID", "").strip()
        key_id = os.environ.get("HEFENG_KEY_ID", "").strip()
        private_key = os.environ.get("HEFENG_PRIVATE_KEY", "").strip()
        private_key_path = os.environ.get("HEFENG_PRIVATE_KEY_PATH", "").strip()
        if not (project_id and key_id and (private_key or private_key_path)):
            errors.append("HEFENG_API_KEY is missing (or JWT credentials are incomplete)")

    token = os.environ.get("MCP_ACCESS_TOKEN", "").strip()
    if len(token) < 24:
        errors.append("MCP_ACCESS_TOKEN must be at least 24 characters")

    return errors


def _host_port() -> tuple[str, int]:
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        port = 8000
        logger.error("Invalid PORT value; falling back to 8000")
    return host, port


def _diagnostic_app(errors: list[str], status_code: int = 503) -> Starlette:
    """Serve a stable diagnostic endpoint instead of entering a restart loop."""

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "misconfigured" if status_code == 503 else "startup_error",
                "service": "hefeng-qweather-mcp",
                "errors": errors,
            },
            status_code=status_code,
        )

    async def unavailable(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "error": "service_unavailable",
                "details": errors,
            },
            status_code=status_code,
        )

    return Starlette(
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Route(
                "/{path:path}",
                endpoint=unavailable,
                methods=["GET", "POST", "DELETE", "OPTIONS"],
            ),
        ]
    )


def _load_upstream():
    """Import upstream while preventing its API-key-prefix INFO log from escaping."""

    previous_disable_level = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        from . import main as upstream
    finally:
        logging.disable(previous_disable_level)
    return upstream


def _hide_tools(mcp, tool_names: Iterable[str]) -> list[str]:
    """Remove selected tools from FastMCP's registered tool table.

    FastMCP v1 does not expose a public remove_tool() API. This adapter therefore
    uses the v1 ToolManager registry deliberately and fails closed if that internal
    shape changes rather than exposing paid tools unexpectedly.
    """

    tool_manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", None)
    if not isinstance(tools, dict):
        raise RuntimeError(
            "Unsupported FastMCP tool registry; refusing to start with paid tools enabled"
        )

    removed: list[str] = []
    for name in tool_names:
        if tools.pop(name, None) is not None:
            removed.append(name)
    return removed


class StaticBearerAuthMiddleware:
    """Minimal static Bearer-token guard for a private remote MCP endpoint."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    @staticmethod
    def _authorization_header(scope: Scope) -> str:
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                return value.decode("latin-1")
        return ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return

        auth = self._authorization_header(scope)
        scheme, separator, supplied = auth.partition(" ")
        authorized = (
            bool(separator)
            and scheme.lower() == "bearer"
            and hmac.compare_digest(supplied.strip(), self.token)
        )
        if not authorized:
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _build_app() -> ASGIApp:
    config_errors = _deployment_config_errors()
    if config_errors:
        logger.error("Deployment configuration incomplete: %s", "; ".join(config_errors))
        return _diagnostic_app(config_errors)

    try:
        upstream = _load_upstream()
        mcp = upstream.mcp

        if not _env_true("ENABLE_PAID_WEATHER_TOOLS", default=False):
            removed = _hide_tools(mcp, _PAID_TOOL_NAMES)
            if removed:
                logger.info("Paid QWeather tools disabled: %s", ", ".join(sorted(removed)))

        @mcp.custom_route("/health", methods=["GET"])
        async def health(_: Request) -> JSONResponse:
            return JSONResponse({"status": "ok", "service": "hefeng-qweather-mcp"})

        token = os.environ["MCP_ACCESS_TOKEN"].strip()
        return StaticBearerAuthMiddleware(mcp.streamable_http_app(), token)
    except Exception as exc:  # keep the container alive so /health can reveal startup state
        logger.exception("Failed to initialize QWeather MCP")
        return _diagnostic_app(
            [f"MCP initialization failed: {type(exc).__name__}; check service logs"],
            status_code=500,
        )


def main() -> None:
    host, port = _host_port()
    app = _build_app()
    logger.info("Starting cloud service on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
