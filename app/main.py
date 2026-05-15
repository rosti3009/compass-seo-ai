from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.db.database import Base, engine

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Compass SEO AI",
    version="1.0.0",
)

app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static",
)

app.include_router(router)