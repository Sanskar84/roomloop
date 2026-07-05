from fastapi import FastAPI
from app.database import engine, Base
from app.routers import rooms, bookings

Base.metadata.create_all(bind=engine)

app = FastAPI(title="RoomLoop", description="Meeting room booking service", version="1.0.0")

app.include_router(rooms.router)
app.include_router(bookings.router)


@app.get("/health")
def health():
    return {"status": "ok"}
