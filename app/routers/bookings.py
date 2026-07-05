from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models import Booking
from app.schemas import BookingCreate, BookingOut, RecurringBookingCreate, RecurringBookingOut, CancelOut
from app.services import booking_service

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.get("", response_model=list[BookingOut])
def list_bookings(
    room_id: Optional[int] = Query(None),
    user: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    series_id: Optional[int] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(Booking)
    if room_id is not None:
        query = query.filter(Booking.room_id == room_id)
    if user is not None:
        query = query.filter(Booking.user == user)
    if status is not None:
        query = query.filter(Booking.status == status)
    if series_id is not None:
        query = query.filter(Booking.series_id == series_id)
    if from_date is not None:
        query = query.filter(Booking.start_time >= from_date)
    if to_date is not None:
        query = query.filter(Booking.start_time <= to_date)
    return query.order_by(Booking.start_time).all()


@router.post("", response_model=BookingOut, status_code=201)
def create_booking(payload: BookingCreate, db: Session = Depends(get_db)):
    return booking_service.create_single_booking(
        db,
        room_id=payload.room_id,
        user=payload.user,
        start_time=payload.start_time,
        end_time=payload.end_time,
        timezone=payload.timezone,
    )


@router.post("/recurring", response_model=RecurringBookingOut, status_code=201)
def create_recurring_booking(payload: RecurringBookingCreate, db: Session = Depends(get_db)):
    return booking_service.create_recurring_booking(
        db,
        room_id=payload.room_id,
        user=payload.user,
        start_time=payload.start_time,
        end_time=payload.end_time,
        timezone=payload.timezone,
        repeat_until=payload.repeat_until,
    )


@router.delete("/{booking_id}", response_model=CancelOut)
def cancel_booking(booking_id: int, db: Session = Depends(get_db)):
    return booking_service.cancel_booking(db, booking_id)
