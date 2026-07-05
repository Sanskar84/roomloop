from __future__ import annotations

from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional
from sqlalchemy import or_
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models import Booking, Series, Room


MAX_SKIPPABLE_CONFLICTS = 2


def validate_timezone(tz_str: str) -> None:
    try:
        ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError):
        raise HTTPException(status_code=422, detail=f"Unknown timezone: {tz_str}")


def check_conflict(
    db: Session,
    room_id: int,
    start: datetime,
    end: datetime,
    exclude_series_id: Optional[int] = None,
) -> bool:
    """Return True if any active booking in the room overlaps [start, end).
    Back-to-back (existing.end == new.start) is NOT a conflict (R4).
    ISO string lexicographic comparison is valid because all bookings in a room
    share the same local timezone (naive local strings sort correctly).
    """
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    query = (
        db.query(Booking)
        .filter(
            Booking.room_id == room_id,
            Booking.status == "active",
            Booking.start_time < end_iso,
            Booking.end_time > start_iso,
        )
    )
    if exclude_series_id is not None:
        # SQL `!=` drops NULL rows, so we must explicitly keep them (single bookings have series_id=NULL)
        query = query.filter(
            or_(Booking.series_id == None, Booking.series_id != exclude_series_id)  # noqa: E711
        )
    return query.count() > 0


def generate_weekly_occurrences(
    start_naive: datetime,
    end_naive: datetime,
    repeat_until: date,
) -> list[tuple[datetime, datetime]]:
    """Generate weekly occurrences at the same wall-clock time.

    Using naive +7 days preserves wall-clock time across DST transitions —
    this is the correct fix for the Denver "hour off" bug (R3).  UTC-based
    arithmetic (+7*24h) would shift by ±1 h when clocks change.
    """
    duration = end_naive - start_naive
    occurrences: list[tuple[datetime, datetime]] = []
    current = start_naive
    while current.date() <= repeat_until:
        occurrences.append((current, current + duration))
        current += timedelta(weeks=1)
    return occurrences


def create_single_booking(db: Session, room_id: int, user: str, start_time: str, end_time: str, timezone: str) -> Booking:
    validate_timezone(timezone)

    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail=f"Room {room_id} not found")

    start = datetime.fromisoformat(start_time)
    end = datetime.fromisoformat(end_time)
    if start >= end:
        raise HTTPException(status_code=422, detail="start_time must be before end_time")

    if check_conflict(db, room_id, start, end):
        raise HTTPException(status_code=409, detail="Booking conflicts with an existing booking")

    booking = Booking(
        room_id=room_id,
        user=user,
        start_time=start_time,
        end_time=end_time,
        status="active",
        series_id=None,
        timezone=timezone,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return booking


def create_recurring_booking(
    db: Session,
    room_id: int,
    user: str,
    start_time: str,
    end_time: str,
    timezone: str,
    repeat_until: date,
):
    validate_timezone(timezone)

    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail=f"Room {room_id} not found")

    start_naive = datetime.fromisoformat(start_time)
    end_naive = datetime.fromisoformat(end_time)
    if start_naive >= end_naive:
        raise HTTPException(status_code=422, detail="start_time must be before end_time")
    if start_naive.date() > repeat_until:
        raise HTTPException(status_code=422, detail="First occurrence is after repeat_until")
    if end_naive - start_naive > timedelta(weeks=1):
        # occurrences are 1 week apart, so a longer duration would make the
        # series' own instances overlap each other
        raise HTTPException(
            status_code=422,
            detail="Recurring booking duration cannot exceed one week (occurrences would overlap each other)",
        )

    occurrences = generate_weekly_occurrences(start_naive, end_naive, repeat_until)

    conflicted: list[tuple[datetime, datetime]] = []
    available: list[tuple[datetime, datetime]] = []
    for occ_start, occ_end in occurrences:
        if check_conflict(db, room_id, occ_start, occ_end):
            conflicted.append((occ_start, occ_end))
        else:
            available.append((occ_start, occ_end))

    # R1: if too many conflicts, abort entirely
    if len(conflicted) > MAX_SKIPPABLE_CONFLICTS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(conflicted)} occurrences conflict with existing bookings "
                f"(max skippable: {MAX_SKIPPABLE_CONFLICTS}). No bookings created."
            ),
        )

    if not available:
        raise HTTPException(
            status_code=409,
            detail="All occurrences conflict with existing bookings. No bookings created.",
        )

    # Commit atomically (R1)
    now_iso = datetime.now().isoformat()
    series = Series(
        room_id=room_id,
        user=user,
        timezone=timezone,
        repeat_until=repeat_until.isoformat(),
        created_at=now_iso,
    )
    db.add(series)
    db.flush()  # get series.id without committing

    bookings = []
    for occ_start, occ_end in available:
        b = Booking(
            room_id=room_id,
            user=user,
            start_time=occ_start.isoformat(),
            end_time=occ_end.isoformat(),
            status="active",
            series_id=series.id,
            timezone=timezone,
        )
        db.add(b)
        bookings.append(b)

    db.commit()
    for b in bookings:
        db.refresh(b)

    return {
        "series_id": series.id,
        "created": len(available),
        "skipped": len(conflicted),
        "skipped_dates": [s.isoformat() for s, _ in conflicted],
        "bookings": bookings,
    }


def cancel_booking(db: Session, booking_id: int):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status == "cancelled":
        raise HTTPException(status_code=409, detail="Booking is already cancelled")

    if booking.series_id is None:
        # Single booking — cancel just this one
        booking.status = "cancelled"
        db.commit()
        return {"cancelled_count": 1}

    # "Future" must be evaluated in the room's own local timezone, since stored
    # timestamps are naive local (a Denver room compared against Berlin server
    # time would be off by hours).
    now_iso = datetime.now(ZoneInfo(booking.timezone)).replace(tzinfo=None).isoformat()

    # Recurring series — cancel all future instances (>= now), keep past ones
    future_bookings = (
        db.query(Booking)
        .filter(
            Booking.series_id == booking.series_id,
            Booking.status == "active",
            Booking.start_time >= now_iso,
        )
        .all()
    )
    for b in future_bookings:
        b.status = "cancelled"
    db.commit()
    return {"cancelled_count": len(future_bookings)}
