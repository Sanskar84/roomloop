"""
Test suite focused on the behaviors most likely to be wrong:
  - back-to-back boundary (R4)
  - conflict detection
  - R1/R2 recurring all-or-nothing vs skip logic
  - cancel future-only for a series
  - DST wall-clock preservation (the Denver hour-off bug)
  - GET /rooms response shape (C2)
"""
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db

# ── in-memory DB for tests ──────────────────────────────────────────────────
# StaticPool ensures all connections share the same in-memory SQLite instance.

TEST_DB_URL = "sqlite://"

test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client():
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def room(client):
    r = client.post("/rooms", json={"name": "Alpha", "capacity": 8, "timezone": "Europe/Berlin"})
    assert r.status_code == 201
    return r.json()


# ── helpers ─────────────────────────────────────────────────────────────────

def book(client, room_id, start, end, user="alice", tz="Europe/Berlin"):
    return client.post("/bookings", json={
        "room_id": room_id,
        "user": user,
        "start_time": start,
        "end_time": end,
        "timezone": tz,
    })


def book_recurring(client, room_id, start, end, repeat_until, user="alice", tz="Europe/Berlin"):
    return client.post("/bookings/recurring", json={
        "room_id": room_id,
        "user": user,
        "start_time": start,
        "end_time": end,
        "timezone": tz,
        "repeat_until": repeat_until,
    })


# ── Test 1: back-to-back is NOT a conflict (R4) ─────────────────────────────

def test_back_to_back_not_a_conflict(client, room):
    rid = room["id"]
    r1 = book(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    r2 = book(client, rid, "2026-07-06T10:00:00", "2026-07-06T11:00:00")
    assert r1.status_code == 201
    assert r2.status_code == 201


# ── Test 2: overlapping bookings DO conflict ─────────────────────────────────

def test_overlap_is_conflict(client, room):
    rid = room["id"]
    r1 = book(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    r2 = book(client, rid, "2026-07-06T09:30:00", "2026-07-06T10:30:00")
    assert r1.status_code == 201
    assert r2.status_code == 409


def test_contained_booking_conflicts(client, room):
    rid = room["id"]
    book(client, rid, "2026-07-06T09:00:00", "2026-07-06T11:00:00")
    r = book(client, rid, "2026-07-06T09:30:00", "2026-07-06T10:30:00")
    assert r.status_code == 409


def test_wrapping_booking_conflicts(client, room):
    rid = room["id"]
    book(client, rid, "2026-07-06T09:30:00", "2026-07-06T10:30:00")
    r = book(client, rid, "2026-07-06T09:00:00", "2026-07-06T11:00:00")
    assert r.status_code == 409


# ── Test 3: recurring all-or-nothing when >2 conflicts (R1) ─────────────────

def test_recurring_aborts_when_more_than_two_conflicts(client, room):
    rid = room["id"]
    # Pre-fill 3 of the Monday slots
    for delta_weeks in range(3):
        d = datetime(2026, 7, 6) + timedelta(weeks=delta_weeks)
        start = d.strftime("%Y-%m-%dT09:00:00")
        end = d.strftime("%Y-%m-%dT10:00:00")
        r = book(client, rid, start, end)
        assert r.status_code == 201

    # Try to create a recurring series — 3 conflicts > 2, must abort entirely
    r = book_recurring(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00", "2026-07-27")
    assert r.status_code == 409

    # Nothing from the series should have been saved
    bookings = client.get(f"/bookings?room_id={rid}").json()
    series_bookings = [b for b in bookings if b["series_id"] is not None]
    assert len(series_bookings) == 0


# ── Test 4: recurring skips ≤2 conflicts and creates the rest (R2) ──────────

def test_recurring_skips_one_conflict(client, room):
    rid = room["id"]
    # Block only week 2
    book(client, rid, "2026-07-13T09:00:00", "2026-07-13T10:00:00")

    r = book_recurring(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00", "2026-07-27")
    assert r.status_code == 201
    data = r.json()
    assert data["skipped"] == 1
    assert data["created"] == 3  # weeks 1, 3, 4
    assert "2026-07-13T09:00:00" in data["skipped_dates"]


def test_recurring_skips_two_conflicts(client, room):
    rid = room["id"]
    book(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    book(client, rid, "2026-07-13T09:00:00", "2026-07-13T10:00:00")

    r = book_recurring(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00", "2026-07-27")
    assert r.status_code == 201
    data = r.json()
    assert data["skipped"] == 2
    assert data["created"] == 2


# ── Test 5: cancel series — future cancelled, past preserved ─────────────────

def test_cancel_series_future_only(client, room):
    rid = room["id"]
    # Series: 4 Mondays, two in the "past" (we fake past by using old dates), two in the future
    # We use dates in the past and future relative to "now"
    past1 = "2020-01-06T09:00:00"
    past2 = "2020-01-13T09:00:00"
    future1 = "2099-01-06T09:00:00"
    future2 = "2099-01-13T09:00:00"

    # Create the series by direct DB manipulation via the service is complex in tests;
    # instead create them individually and fake the series_id via a recurring booking
    # with a start in the past. SQLite stores strings so this works fine.
    r = book_recurring(client, rid, past1, past1.replace("09:00", "10:00"), "2020-01-20")
    assert r.status_code == 201
    series_id = r.json()["series_id"]
    past_booking_ids = [b["id"] for b in r.json()["bookings"]]

    # Create future bookings in same series is not directly possible via API once series is created;
    # use a second recurring booking to demonstrate the cancel-future logic cleanly
    r2 = book_recurring(client, rid, future1, future1.replace("09:00", "10:00"), "2099-01-20")
    assert r2.status_code == 201
    series_id2 = r2.json()["series_id"]
    future_booking_ids = [b["id"] for b in r2.json()["bookings"]]

    # Cancel the future series using any of its booking IDs
    cancel_r = client.delete(f"/bookings/{future_booking_ids[0]}")
    assert cancel_r.status_code == 200
    assert cancel_r.json()["cancelled_count"] == len(future_booking_ids)

    # Verify past series bookings are untouched
    for bid in past_booking_ids:
        b = client.get(f"/bookings?room_id={rid}").json()
        matching = [x for x in b if x["id"] == bid]
        assert matching[0]["status"] == "active"

    # Verify future series bookings are all cancelled
    future_status = client.get(f"/bookings?room_id={rid}&series_id={series_id2}").json()
    future_active = [b for b in future_status if b["series_id"] == series_id2 and b["status"] == "active"]
    assert len(future_active) == 0


def test_cancel_single_booking(client, room):
    rid = room["id"]
    r = book(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    bid = r.json()["id"]

    cancel_r = client.delete(f"/bookings/{bid}")
    assert cancel_r.status_code == 200
    assert cancel_r.json()["cancelled_count"] == 1

    # Slot should now be free
    r2 = book(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    assert r2.status_code == 201


# ── Test 6: DST wall-clock preservation (Denver "hour off" bug) ──────────────

def test_dst_wall_clock_preservation_denver(client, room):
    """
    DST in America/Denver in 2027: clocks spring forward on 2027-03-14 at 02:00.
    A recurring Monday 09:00–10:00 booking spanning that date must stay at 09:00
    on every occurrence — not shift to 08:00 or 10:00 after the transition.

    We verify this by checking that all generated booking start times have T09:00:00.
    Naive +7days arithmetic preserves wall-clock time; UTC-based arithmetic would not.
    """
    rid = room["id"]
    # Monday 2027-03-08 through 2027-03-29 — straddles the March 14 spring-forward
    r = book_recurring(
        client, rid,
        "2027-03-08T09:00:00", "2027-03-08T10:00:00",
        "2027-03-29",
        tz="America/Denver",
    )
    assert r.status_code == 201
    bookings = r.json()["bookings"]
    assert len(bookings) == 4  # 4 Mondays: Mar 8, 15, 22, 29
    for b in bookings:
        assert b["start_time"].endswith("T09:00:00"), (
            f"Expected 09:00:00 but got {b['start_time']} — DST shift bug"
        )


# ── Test 7: GET /rooms matches C2 response shape exactly ─────────────────────

def test_rooms_response_shape(client):
    client.post("/rooms", json={"name": "Aurora", "capacity": 8, "timezone": "Europe/Berlin"})
    client.post("/rooms", json={"name": "Basalt", "capacity": 4, "timezone": "Europe/Berlin"})

    r = client.get("/rooms")
    assert r.status_code == 200
    rooms = r.json()
    assert len(rooms) == 2
    for room in rooms:
        assert set(room.keys()) == {"id", "name", "capacity"}  # no extra fields like 'timezone'
    assert rooms[0]["name"] == "Aurora"
    assert rooms[1]["name"] == "Basalt"


# ── Test 8: invalid room returns 404 ─────────────────────────────────────────

def test_booking_invalid_room(client):
    r = book(client, 9999, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    assert r.status_code == 404


# ── Test 9: start_time >= end_time is rejected ────────────────────────────────

def test_start_after_end_rejected(client, room):
    r = book(client, room["id"], "2026-07-06T10:00:00", "2026-07-06T09:00:00")
    assert r.status_code == 422


def test_start_equals_end_rejected(client, room):
    r = book(client, room["id"], "2026-07-06T09:00:00", "2026-07-06T09:00:00")
    assert r.status_code == 422


# ── Test 10: cancelled slot becomes available again ───────────────────────────

def test_cancelled_slot_is_rebook_able(client, room):
    rid = room["id"]
    r = book(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    bid = r.json()["id"]
    client.delete(f"/bookings/{bid}")

    # Same slot should now succeed
    r2 = book(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    assert r2.status_code == 201


# ── Test 11: different rooms at the same time do NOT conflict ─────────────────

def test_same_time_different_rooms_no_conflict(client):
    r1 = client.post("/rooms", json={"name": "RoomA", "capacity": 4, "timezone": "Europe/Berlin"})
    r2 = client.post("/rooms", json={"name": "RoomB", "capacity": 4, "timezone": "Europe/Berlin"})
    rid1, rid2 = r1.json()["id"], r2.json()["id"]

    b1 = book(client, rid1, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    b2 = book(client, rid2, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    assert b1.status_code == 201
    assert b2.status_code == 201


# ── Test 12: double-cancel returns 409 ────────────────────────────────────────

def test_double_cancel_returns_409(client, room):
    rid = room["id"]
    r = book(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    bid = r.json()["id"]

    client.delete(f"/bookings/{bid}")
    r2 = client.delete(f"/bookings/{bid}")
    assert r2.status_code == 409


# ── Test 13: cancel via past instance ID — 0 future instances cancelled ───────

def test_cancel_series_via_past_instance_cancels_nothing(client, room):
    """
    Cancelling a series using a past booking ID has no future instances to cancel.
    The cancelled_count should be 0 (not an error).  This is a documented edge case:
    users must use a current/future booking ID to cancel the remaining series.
    """
    rid = room["id"]
    past_start = "2020-01-06T09:00:00"
    r = book_recurring(client, rid, past_start, past_start.replace("09:00", "10:00"), "2020-01-20")
    assert r.status_code == 201
    past_bid = r.json()["bookings"][0]["id"]

    cancel_r = client.delete(f"/bookings/{past_bid}")
    assert cancel_r.status_code == 200
    # 0 future instances — nothing in the series starts >= now
    assert cancel_r.json()["cancelled_count"] == 0


# ── Test 14: exact threshold — 2 of 2 occurrences conflict → abort (R1) ──────

def test_recurring_all_conflict_two_of_two_aborts(client, room):
    """
    A series with only 2 possible occurrences where both conflict:
    len(conflicted) == 2 which is NOT > MAX_SKIPPABLE_CONFLICTS (2),
    but available == [] so the second guard must fire and abort.
    """
    rid = room["id"]
    book(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00")
    book(client, rid, "2026-07-13T09:00:00", "2026-07-13T10:00:00")

    r = book_recurring(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00", "2026-07-13")
    assert r.status_code == 409

    # Nothing saved
    bookings = client.get(f"/bookings?room_id={rid}").json()
    assert all(b["series_id"] is None for b in bookings)


# ── Test 15: midnight-crossing booking conflict ───────────────────────────────

def test_midnight_crossing_conflict(client, room):
    rid = room["id"]
    # 23:00–01:00 next day
    r1 = book(client, rid, "2026-07-06T23:00:00", "2026-07-07T01:00:00")
    assert r1.status_code == 201

    # Overlapping with the midnight crossing
    r2 = book(client, rid, "2026-07-07T00:30:00", "2026-07-07T02:00:00")
    assert r2.status_code == 409

    # Starting exactly at end — back-to-back, not a conflict
    r3 = book(client, rid, "2026-07-07T01:00:00", "2026-07-07T02:00:00")
    assert r3.status_code == 201


# ── Test 16: GET /bookings series_id filter works ─────────────────────────────

def test_get_bookings_filter_by_series_id(client, room):
    rid = room["id"]
    r = book_recurring(client, rid, "2026-07-06T09:00:00", "2026-07-06T10:00:00", "2026-07-27")
    series_id = r.json()["series_id"]

    # Single booking in a different slot
    book(client, rid, "2026-07-06T11:00:00", "2026-07-06T12:00:00")

    filtered = client.get(f"/bookings?series_id={series_id}").json()
    assert len(filtered) == 4
    assert all(b["series_id"] == series_id for b in filtered)


# ── Test 17: offset-bearing timestamps are rejected (C1 enforcement) ──────────

def test_offset_timestamp_rejected(client, room):
    """C1 requires naive local ISO strings. An offset-bearing timestamp must be
    rejected, not silently stored (it would break the reporting job's parser
    and corrupt naive string comparison)."""
    r = book(client, room["id"], "2026-07-06T09:00:00+02:00", "2026-07-06T10:00:00+02:00")
    assert r.status_code == 422


# ── Test 18: short ISO forms are normalized before storage ────────────────────

def test_short_iso_form_normalized(client, room):
    """fromisoformat accepts '2026-07-06T09:00' (no seconds). Stored strings
    must be normalized to full form or lexicographic conflict comparison breaks."""
    rid = room["id"]
    r = book(client, rid, "2026-07-06T09:00", "2026-07-06T10:00")
    assert r.status_code == 201
    assert r.json()["start_time"] == "2026-07-06T09:00:00"
    assert r.json()["end_time"] == "2026-07-06T10:00:00"

    # Conflict detection must still work against the normalized form
    r2 = book(client, rid, "2026-07-06T09:30:00", "2026-07-06T10:30:00")
    assert r2.status_code == 409


# ── Test 19: recurring duration > 1 week rejected (self-overlap guard) ────────

def test_recurring_duration_over_one_week_rejected(client, room):
    """Occurrences are one week apart, so a duration longer than a week would
    make the series' own instances overlap each other."""
    r = book_recurring(
        client, room["id"],
        "2026-07-06T09:00:00", "2026-07-15T09:00:00",  # 9 days
        "2026-08-31",
    )
    assert r.status_code == 422


def test_recurring_exactly_one_week_duration_allowed(client, room):
    """Exactly 7 days means each occurrence ends exactly when the next starts —
    back-to-back, which is not a conflict per R4."""
    r = book_recurring(
        client, room["id"],
        "2026-07-06T09:00:00", "2026-07-13T09:00:00",  # exactly 7 days
        "2026-07-27",
    )
    assert r.status_code == 201
    assert r.json()["created"] == 4
    assert r.json()["skipped"] == 0
