"""Small helpers shared by every route module."""
from fastapi import Query, Request
from fastapi.responses import JSONResponse

API_PREFIX = "/api/v1"
MAX_PAGE = 200


def rid_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error(status: int, code: str, message: str, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "request_id": request_id}})


def page(limit: int = Query(50, ge=1, le=MAX_PAGE), offset: int = Query(0, ge=0, le=1_000_000)) -> tuple[int, int]:
    return limit, offset


def route_group(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return parts[2] if len(parts) > 2 and parts[0] == "api" else (parts[0] if parts else "root")
