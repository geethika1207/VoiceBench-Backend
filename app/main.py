from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .db.database import engine, Base
from .routers import auth, interview, interview_result, history, delete_history
from fastapi.staticfiles import StaticFiles
from pathlib import Path

Base.metadata.create_all(bind=engine)

app = FastAPI()

print("========== NEW BACKEND DEPLOYED ==========")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent

app.mount(
    "/audio",
    StaticFiles(directory=BASE_DIR / "audio"),
    name="audio"
)

app.include_router(auth.router)
app.include_router(interview.router)
app.include_router(interview_result.router)
app.include_router(history.router)
app.include_router(delete_history.router)