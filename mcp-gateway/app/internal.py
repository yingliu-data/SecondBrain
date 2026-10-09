"""`/internal/*` routes (M4-CONTRACTS.md §3). The bearer and identity checks
run in `main.AuthMiddleware` before these handlers."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.gateway import Gateway, result_text
from app.identity import identity_from_headers
from app.upstreams import unwrap


def routes(gateway: Gateway) -> list[Route]:
    async def tools(request: Request) -> JSONResponse:
        ident, _ = identity_from_headers(request.headers)
        return JSONResponse({"tools": await gateway.hello_entries(ident)})

    async def upstream_tools(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in gateway.upstreams:
            return JSONResponse({"error": f"unknown upstream '{name}'"}, status_code=404)
        try:
            gateway.upstreams[name].check_url()
            defs = await gateway.upstream_openai_tools(name)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"MCP server '{name}' unreachable or failed: {unwrap(e)}"},
                                status_code=502)
        return JSONResponse({"tools": defs})

    async def call(request: Request) -> JSONResponse:
        ident, _ = identity_from_headers(request.headers)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        name = body.get("name") if isinstance(body, dict) else None
        arguments = body.get("arguments") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            return JSONResponse({"error": "'name' is required"}, status_code=400)
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return JSONResponse({"error": "'arguments' must be an object"}, status_code=400)
        result = await gateway.call_legacy(ident, name, arguments)
        return JSONResponse({"content": result_text(result), "is_error": bool(result.isError)})

    return [
        Route("/internal/tools", tools, methods=["GET"]),
        Route("/internal/upstreams/{name}/tools", upstream_tools, methods=["GET"]),
        Route("/internal/call", call, methods=["POST"]),
    ]
