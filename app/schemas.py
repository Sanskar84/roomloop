from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import datetime, date


class RoomCreate(BaseModel):
    name: str
    capacity: int
    timezone: str = "UTC"


class RoomOut(BaseModel):
    id: int
    name: str
    capacity: int

    model_config = {"from_attributes": True}


class BookingCreate(BaseModel):
    room_id: int
    user: str
    start_time: str  # naive local ISO string
    end_time: str
    timezone: str = "UTC"

    @field_validator("start_time", "end_time")
    @classmethod
    def must_be_naive_iso(cls, v: str) -> str:
        parsed = datetime.fromisoformat(v)
        if parsed.tzinfo is not None:
            raise ValueError(
                "timestamps must be naive local ISO strings without UTC offset, "
                "e.g. 2026-07-02T09:00:00 (C1 reporting constraint)"
            )
        # Normalize (e.g. "2026-07-06T09:00" -> "2026-07-06T09:00:00") so stored
        # strings compare correctly and match the C1 format exactly.
        return parsed.replace(microsecond=0).isoformat()


class BookingOut(BaseModel):
    id: int
    room_id: int
    user: str
    start_time: str
    end_time: str
    status: str
    series_id: Optional[int]
    timezone: str

    model_config = {"from_attributes": True}


class RecurringBookingCreate(BaseModel):
    room_id: int
    user: str
    start_time: str  # naive local ISO — first occurrence
    end_time: str
    timezone: str = "UTC"
    repeat_until: date  # inclusive end date for the series

    @field_validator("start_time", "end_time")
    @classmethod
    def must_be_naive_iso(cls, v: str) -> str:
        parsed = datetime.fromisoformat(v)
        if parsed.tzinfo is not None:
            raise ValueError(
                "timestamps must be naive local ISO strings without UTC offset, "
                "e.g. 2026-07-02T09:00:00 (C1 reporting constraint)"
            )
        return parsed.replace(microsecond=0).isoformat()


class RecurringBookingOut(BaseModel):
    series_id: int
    created: int
    skipped: int
    skipped_dates: list[str]
    bookings: list[BookingOut]


class CancelOut(BaseModel):
    cancelled_count: int
