"""Cloud entrypoint for the QWeather MCP server.

Adds deployment-safe defaults around the upstream MCP implementation:
- binds to 0.0.0.0 and honors PORT
- requires a static Bearer token for remote MCP access
- exposes an unauthenticated /health endpoint
- hides paid QWeather tropical-cyclone tools by default
- suppresses upstream INFO logs during import so API key prefixes are not logged
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Iterable

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# The upstream module logs the first 10 characters of HEFENG_API_KEY at import time.
# Keep credentials unchanged, but suppress INFO output just for that import.
_previous_disable_level = logging.root.manager.disable
logging.disable(logging.INFO)
try:
    from . import main as upstream
finally:
    logging.disable(_previous_disable_level)

logger = logging.getLogger("hefeng_qweather_mcp.cloud")
mcp = upstream.mcp

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


def _hide_tools(tool_names: Iterable[str]) -> list[str]:
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


if not _env_true("ENABLE_PAID_WEATHER_TOOLS", default=False):
    removed = _hide_tools(_PAID_TOOL_NAMES)
    if removed:
        logger.info("Paid QWeather tools disabled: %s", ", ".join(sorted(removed)))


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "hefeng-qweather-mcp"})


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


def main() -> None:
    token = os.environ.get("MCP_ACCESS_TOKEN", "").strip()
    if len(token) < 24:
        raise RuntimeError(
            "MCP_ACCESS_TOKEN must be set to a random secret of at least 24 characters"
        )

    host = os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError as exc:
        raise RuntimeError("PORT must be an integer") from exc

    app = StaticBearerAuthMiddleware(mcp.streamable_http_app(), token)

    logger.info(
        "Starting private Streamable HTTP MCP on %s:%s (endpoint /mcp, health /health)",
        host,
        port,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
