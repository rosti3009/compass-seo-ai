import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.database import Base, engine, ensure_sqlite_schema_compatibility


class UTF8JSONResponse(JSONResponse):
    """JSON response that keeps Hebrew readable in API and PowerShell output."""

    media_type = "application/json; charset=utf-8"

    def render(self, content: object) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize application resources on startup."""
    configure_logging(settings.log_level)
    Base.metadata.create_all(bind=engine)
    ensure_sqlite_schema_compatibility(engine)
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="SEO crawler MVP with Google Search Console and GA4 integration stubs.",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
