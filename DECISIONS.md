# DECISIONS.md

## (a) Decisions made where the brief did not specify

1. **REST API, not CLI.** The integration constraints (C1: a nightly reporting job that parses timestamps from the API, C2: a facilities dashboard calling `GET /rooms`) both describe live HTTP consumers. A CLI cannot satisfy those contracts, so FastAPI was the obvious choice.

2. **R1 vs R2 conflict resolution.** R1 says "all-or-nothing if it cannot be fully created"; R2 says "skip one or two conflicts and create the rest." These appear contradictory. My interpretation: R2 is a *business exception* to R1 — a series with ≤2 conflicting instances is still considered "creatable." If >2 instances conflict (or zero instances remain), the entire series is aborted and nothing is saved (R1 applies). This threshold of 2 matches the brief's wording exactly and is documented in `services/booking_service.py`.

3. **DST / wall-clock fix for Denver.** The brief says recurring bookings must repeat at the same *wall-clock* time (R3) and flags that Denver bookings were appearing an hour off. The root cause is UTC-based weekly arithmetic: adding 7×24 hours to a UTC timestamp gives the wrong local time after a DST transition. The fix is to work in *naive local* datetimes and add `timedelta(weeks=1)` directly — this always lands on the same clock time. Constraint C1 (store timestamps as naive local ISO strings) makes this the natural approach.

4. **Naive local strings for conflict detection, enforced at the boundary.** Since a room is a single physical location, all its bookings are in the same local timezone. Comparing naive ISO strings lexicographically is therefore safe and avoids any offset arithmetic. To protect this (and C1), the API rejects offset-bearing timestamps (`...T09:00:00+02:00`) with a 422 and normalizes short forms (`T09:00` → `T09:00:00`) before storage. The `timezone` field is stored on each booking for auditing and future migration.

5. **SQLite as the datastore.** I chose SQLite because the expected deployment is a small internal service (~200 employees), making it simple to run and evaluate. For a production deployment with higher concurrency, I would migrate to PostgreSQL and use database-level mechanisms (e.g., exclusion constraints or stronger transactional guarantees) to prevent overlapping bookings. Swapping is a one-line `DATABASE_URL` change with SQLAlchemy.

6. **Double-booking race fixed with `BEGIN IMMEDIATE`.** Check-then-insert is not atomic by default: SQLite's DEFERRED transactions allow concurrent readers, so two simultaneous requests could both pass the conflict-check `SELECT` before either `INSERT`s — a silent double-booking. The engine now issues `BEGIN IMMEDIATE` at transaction start (see `app/database.py`), acquiring the write lock *before* the conflict check so writers are fully serialized. The trade-off is that all transactions serialize behind writers — fine for ~200 employees. On a PostgreSQL migration this must be replaced with `SELECT FOR UPDATE` or, better, a range exclusion constraint (`EXCLUDE USING gist (room_id WITH =, tsrange(start_time, end_time) WITH &&) WHERE (status = 'active')`) which rejects overlapping inserts at the DB level regardless of timing.

7. **`POST /rooms` is not in the brief.** The spec only requires `GET /rooms` (C2) and treats rooms as pre-existing data managed elsewhere (likely the facilities team). I added `POST /rooms` purely as a convenience for local setup and evaluation — it is not part of the specified API surface. For the number of rooms, the brief gives no fixed count; R5 says rooms are numbered 1..N sequentially. The C2 sample shows 4 rooms (Aurora, Basalt, Cinder, Dune), so I seeded those same 4 in `seed_data.py` as a concrete baseline. In production, N would be whatever the facilities team configures.

8. **Cancellation is keyed on booking ID.** The brief says "cancel a booking" but does not specify what identifier to use. I chose `DELETE /bookings/{booking_id}` — standard REST convention for targeting a specific resource. For a single booking it cancels just that one. For a recurring booking, the ID is used to look up the `series_id` and cancel all future instances in that series, so any booking ID in the series acts as a handle to the whole group.

9. **`series_id` as a grouping mechanism for recurring bookings.** The brief requires cancelling all future instances of a recurring series, which means individual bookings must know they belong together. I introduced a `Series` table (one row per recurring group) and a `series_id` foreign key on each `Booking` row. Single bookings have `series_id = NULL`. This cleanly separates the shared series metadata (user, timezone, repeat_until) from the individual occurrence records, and makes the cancel query trivial: find all active bookings with the same `series_id` where `start_time >= now`. "Now" is evaluated in the booking's own timezone (not server time), since stored timestamps are naive local — a Denver series cancelled from a Berlin-hosted server would otherwise cut off at the wrong instant.

10. **Recurring duration capped at one week.** Occurrences are generated one week apart, so a duration longer than 7 days would make the series' own instances overlap each other. Rejected with 422. Exactly 7 days is allowed — each occurrence ends exactly when the next starts, which is back-to-back and not a conflict per R4.

---

## (b) Questions I would ask the PM before shipping

1. **R1/R2 threshold:** Is 2 the right number of skippable conflicts, or should it be configurable per room / per user role? (I hardcoded 2 to match the brief's wording.)

2. **Timezone source of truth:** Should the room carry a canonical timezone, or does the caller supply it per-booking? Right now both the room *and* the booking carry a timezone; they could diverge. Should the API enforce that booking.timezone == room.timezone?

3. **Cancel semantics — what is "now"?** I evaluate "future" in the *room's* local timezone (a Denver room compares against Denver-now, not server time). But should an in-progress meeting (started 10 minutes ago) count as past or future? Currently it counts as past and stays active.

4. **Partial-series cancel:** Can an admin cancel *one instance* from a recurring series without affecting the others? Currently `DELETE /bookings/{id}` on a series booking cancels the whole future tail. A "cancel single instance" path may be needed.

5. **Room numbering:** R5 says rooms are numbered 1..N sequentially. Does that mean IDs must be contiguous (no gaps)? My implementation uses auto-increment PKs, which could leave gaps if rooms are deleted.

6. **Authentication / authorisation:** Nothing in the brief mentions it. For an internal service this is a real concern — can any user cancel any booking?

---

## (c) Where AI helped, and anything it got wrong

- **AI helped with:** boilerplate (FastAPI/SQLAlchemy wiring, Pydantic schemas, pytest fixtures), structuring the project layout, and drafting this document.
- **Corrected:** An early AI draft used `existing.start_time <= new_start` for back-to-back detection, which would have falsely flagged back-to-back bookings as conflicts. Fixed to strict `<` / `>` comparisons per R4.
- **Corrected:** The initial plan proposed storing `datetime` objects; switched to storing naive ISO strings as required by C1.
- **Corrected:** AI initially claimed SQLite's file-level write lock made check-then-insert safe against concurrent double-booking. Wrong — DEFERRED transactions allow concurrent readers, so both requests can pass the conflict SELECT before either writes. Fixed properly with `BEGIN IMMEDIATE`.
- **Corrected:** AI's first validator accepted any `fromisoformat`-parseable string, which would have let offset-bearing timestamps (`+02:00`) into the DB, breaking C1. Tightened to reject aware datetimes and normalize short forms.

---

## (d) Deliberately left out, and what I would do next

- **Availability endpoint (R5).** R5 says "for availability checks you can iterate rooms by ID from 1 to N." This is a hint about room numbering, not a required feature — the brief never asks for a "find me a free room" endpoint. We didn't build it. Would add `GET /availability?date=...&duration=...` as a next step, iterating room IDs 1..N and returning which are free for the requested slot.
- **Authentication.** No auth layer — would add OAuth2/JWT for a real deployment.
- **Pagination on `GET /bookings`.** The endpoint returns all matching bookings; for production add `limit`/`offset` or cursor-based pagination.
- **`GET /bookings/{id}` single-booking fetch.** Not included; straightforward to add.
- **Async DB driver.** Using synchronous SQLAlchemy for simplicity; would migrate to `asyncpg` + async SQLAlchemy for higher throughput.
- **Migrations.** Tables are created with `create_all` on startup. For production I would use Alembic to manage schema changes.
- **OpenAPI client generation.** FastAPI already emits a spec at `/openapi.json`; the downstream teams (reporting, dashboard) could generate typed clients from it.
