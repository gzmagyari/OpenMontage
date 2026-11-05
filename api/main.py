"""FastAPI application factory. Run: ``uvicorn api.main:app --host 0.0.0.0 --port 8080 --workers 1``.

One worker per container on purpose: jobs run as asyncio tasks inside this
process and resource limits are process-local. Scale out with more containers.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

from engine import __version__
from engine.assets import AssetStore
from engine.config import Settings, get_settings
from engine.jobs import JobManager, JobStore, Limits, NoRunner, QueueFull

log = logging.getLogger("openmontage.api")


def create_app(settings: Settings | None = None, runners: dict | None = None, services: Any = None) -> FastAPI:
    """Build the app. Tests inject fake ``runners``; production registers the real ones."""
    cfg: Settings = settings or get_settings()
    return _build(cfg, runners, services)


def _build(settings: Settings, runners: dict | None, services: Any) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        if not settings.api_token:
            log.warning("OPENMONTAGE_API_TOKEN is not set: the API accepts unauthenticated requests")
        store = JobStore(settings.jobs_dir)
        limits = Limits(settings)
        engine_services = services
        job_runners = runners
        if job_runners is None:
            from engine.runners import build_runners
            from engine.services import EngineServices

            engine_services = EngineServices(settings)
            job_runners = build_runners()
        manager = JobManager(settings, store, limits, job_runners, engine_services)
        app.state.settings = settings
        app.state.store = store
        app.state.limits = limits
        app.state.manager = manager
        app.state.assets = AssetStore(settings.assets_dir)
        app.state.services = engine_services
        if engine_services is not None and hasattr(engine_services, "bind"):
            engine_services.bind(app.state.assets, store)
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(
        title="OpenMontage Video API",
        version=__version__,
        description="Async, job-based video generation on top of OpenMontage's agent pipelines. "
                    "Submit, then poll GET /v1/jobs/{id}.",
        lifespan=lifespan,
    )

    @app.exception_handler(QueueFull)
    async def _queue_full(_: Request, exc: QueueFull):
        return JSONResponse({"error": {"code": "queue_full", "message": str(exc)}}, status_code=429,
                            headers={"Retry-After": "30"})

    @app.exception_handler(NoRunner)
    async def _no_runner(_: Request, exc: NoRunner):
        return JSONResponse({"error": {"code": "not_implemented",
                                       "message": f"{exc} (the agent-based video runner is not built yet)"}},
                            status_code=501)

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
        return JSONResponse({"error": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    from api.routes import assets, jobs, meta, tools, videos

    app.include_router(meta.public)
    app.include_router(meta.router)
    app.include_router(videos.router)
    app.include_router(jobs.router)
    app.include_router(assets.router)
    if settings.enable_tool_api:
        app.include_router(tools.router)
    return app


def _create_default_app() -> FastAPI:
    from lib.env_loader import load_env

    load_env()  # populate os.environ from .env for local runs (Docker passes env vars directly)
    return create_app()


app = _create_default_app()
