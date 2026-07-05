# RoomLoop

Meeting room booking service for RoomLoop. Built with FastAPI + SQLite.

## Why REST API?

Integration constraints in the brief describe two live HTTP consumers: a nightly reporting job that reads timestamps from the API and a facilities dashboard that calls `GET /rooms`. A CLI cannot satisfy either constraint, so a REST API is the required choice.

## Setup

```bash
cd roomloop
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

API is available at `http://localhost:8000`.
Interactive docs: `http://localhost:8000/docs`

## Run Tests

```bash
pytest tests/ -v
```

## Seed Demo Data

With the server running:

```bash
python seed_data.py
```

This creates 4 rooms (Aurora, Basalt, Cinder, Dune), several single bookings, a recurring series with one skipped conflict, and a Denver recurring series spanning the DST transition.

---

## API Reference

### Rooms

| Method | Path | Description |
|--------|------|-------------|
| GET | `/rooms` | List all rooms |
| POST | `/rooms` | Create a room |
| GET | `/rooms/{id}` | Get a single room |

**Create room body:**
```json
{
  "name": "Aurora",
  "capacity": 8,
  "timezone": "Europe/Berlin"
}
```

---

### Bookings

| Method | Path | Description |
|--------|------|-------------|
| GET | `/bookings` | List bookings (filters: room_id, user, status, series_id, from_date, to_date) |
| POST | `/bookings` | Create a single booking |
| POST | `/bookings/recurring` | Create a weekly recurring booking |
| DELETE | `/bookings/{id}` | Cancel a booking (or whole future series) |

**Single booking:**
```bash
curl -X POST http://localhost:8000/bookings \
  -H "Content-Type: application/json" \
  -d '{
    "room_id": 1,
    "user": "alice@company.com",
    "start_time": "2026-07-06T09:00:00",
    "end_time": "2026-07-06T10:00:00",
    "timezone": "Europe/Berlin"
  }'
```

**Recurring booking:**
```bash
curl -X POST http://localhost:8000/bookings/recurring \
  -H "Content-Type: application/json" \
  -d '{
    "room_id": 1,
    "user": "alice@company.com",
    "start_time": "2026-07-07T09:00:00",
    "end_time": "2026-07-07T10:00:00",
    "timezone": "Europe/Berlin",
    "repeat_until": "2026-12-31"
  }'
```

Response includes `series_id`, `created`, `skipped`, and `skipped_dates` (up to 2 conflicting slots that were skipped).

**Cancel (single or series):**
```bash
curl -X DELETE http://localhost:8000/bookings/42
```

If the booking belongs to a recurring series, all *future* instances in that series are cancelled. Past instances are preserved.

---

## Key Design Decisions

See [DECISIONS.md](DECISIONS.md) for full reasoning. Short summary:

- **Timestamps stored as naive local ISO strings** — required by C1 (reporting job compatibility). Offset-bearing timestamps (`+02:00`) are rejected with 422; short forms are normalized to `YYYY-MM-DDTHH:MM:SS`.
- **DST / wall-clock recurrence** — weekly occurrences use `timedelta(weeks=1)` on naive local datetimes, not UTC arithmetic. This is the fix for the Denver "hour off" bug (R3).
- **R1 vs R2** — ≤2 conflicting instances are skipped; the rest of the series is created. If >2 conflict the whole series is aborted (nothing saved).
- **No double-booking under concurrency** — SQLite transactions use `BEGIN IMMEDIATE`, so the conflict check and insert run under the write lock. Two simultaneous booking requests cannot both pass the conflict check.
- **SQLite** — zero-config, swap to Postgres via one `DATABASE_URL` change (would then need an exclusion constraint for the concurrency guarantee — see DECISIONS.md).
