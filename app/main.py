"""FastAPI application. Owner: B.

All routers are registered here once and for all, so nobody else needs to edit this file.
"""

from http import HTTPStatus

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from neo4j.exceptions import ServiceUnavailable

from app.models.common import ErrorResponse, FieldError
from app.routers import agent, clubs, health, network, players, rumours

# Every router whose endpoints take parameters documents the real body of its 422 responses: an
# `ErrorResponse` (see `invalid_request` below) instead of FastAPI's default `HTTPValidationError`.
INVALID_REQUEST = {HTTPStatus.UNPROCESSABLE_ENTITY.value: {"model": ErrorResponse, "description": "Requête invalide"}}


def field_errors(exc: RequestValidationError) -> list[FieldError]:
    """One `FieldError` per Pydantic error: where (query, path, body), which field, and why."""
    errors = []
    for error in exc.errors():
        location, *field = [str(part) for part in error.get("loc", ())] or ["request"]
        errors.append(FieldError(location=location, field=".".join(field) or location, message=error.get("msg", "")))
    return errors


def create_app() -> FastAPI:
    """Build the application: routers and error handlers."""
    application = FastAPI(
        title="Football Scout Agent",
        description="Graphe Neo4j des transferts européens et agent de recrutement LangGraph.",
        version="0.1.0",
    )
    # network before clubs and players: its fixed paths (/clubs/hubs) must win over path parameters.
    for module in (health, network, players, clubs, rumours, agent):
        application.include_router(module.router, responses=None if module is health else INVALID_REQUEST)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = field_errors(exc)
        summary = " ; ".join(f"{error.field} ({error.location}) : {error.message}" for error in errors)
        body = ErrorResponse(detail=f"Requête invalide : {summary}", errors=errors)
        return JSONResponse(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, content=body.model_dump())

    @application.exception_handler(NotImplementedError)
    async def not_implemented(_: Request, __: NotImplementedError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_501_NOT_IMPLEMENTED, content={"detail": "Pas encore implémenté"})

    @application.exception_handler(ServiceUnavailable)
    async def neo4j_unavailable(_: Request, __: ServiceUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": "Neo4j est injoignable"}
        )

    return application


app = create_app()
