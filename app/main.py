"""FastAPI application. Owner: B.

All routers are registered here once and for all, so nobody else needs to edit this file.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from neo4j.exceptions import ServiceUnavailable

from app.routers import agent, clubs, health, network, players, rumours


def create_app() -> FastAPI:
    """Build the application: routers and error handlers."""
    application = FastAPI(
        title="Football Scout Agent",
        description="Graphe Neo4j des transferts européens et agent de recrutement LangGraph.",
        version="0.1.0",
    )
    # network before clubs and players: its fixed paths (/clubs/hubs) must win over path parameters.
    for module in (health, network, players, clubs, rumours, agent):
        application.include_router(module.router)

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
