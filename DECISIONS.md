# DECISIONS.md

## (a) Decisions made where the brief did not specify

1. **REST API, not CLI.** The integration constraints (C1: a nightly reporting job that parses timestamps from the API, C2: a facilities dashboard calling `GET /rooms`) both describe live HTTP consumers. A CLI cannot satisfy those contracts, so FastAPI was the obvious choice.

2. **R1 vs R2 conflict resolution.** R1 says "all-or-nothing if it cannot be fully created"; R2 says "skip one or two conflicts and create the rest." These appear contradictory. My interpretation: R2 is a *business exception* to R1 — a series with ≤2 conflicting instances is still considered "creatable." If >2 instances conflict (or zero instances remain), the entire series is aborted and nothing is saved (R1 applies). This threshold of 2 matches the brief's wording exactly and is documented in `services/booking_service.py`.

3. **DST / wall-clock fix for Denver.** The brief says recurring bookings must repeat at the same *wall-clock* time (R3) and flags that Denver bookings were appearing an hour off. The root cause is UTC-based weekly arithmetic: adding 7×24 hours to a UTC timestamp gives the wrong local time after a DST transition. The fix is to work in *naive local* datetimes and add `timedelta(weeks=1)` directly — this always lands on the same clock time. Constraint C1 (store timestamps as naive local ISO strings) makes this the natural approach.

4. **Naive local strings for conflict detection.** Since a room is a single physical location, all its bookings are in the same local timezone. Comparing naive ISO strings lexicographically is therefore safe and avoids any offset arithmetic. The `timezone` field is stored on each booking for auditing and future migration (e.g., if the company ever needs UTC-normalised queries).

5. **SQLite as the datastore.** I chose SQLite because the expected deployment is a small internal service (~200 employees), making it simple to run and evaluate. For a production deployment with higher concurrency, I would migrate to PostgreSQL and use database-level mechanisms (e.g., exclusion constraints or stronger transactional guarantees) to prevent overlapping bookings. Swapping is a one-line `DATABASE_URL` change with SQLAlchemy.

6. **Conflict detection is application-level only.** The check-then-insert is not atomic. SQLite's file-level write lock serializes concurrent requests and makes this safe here, but it is accidental safety — not an explicit guarantee. A production PostgreSQL deployment would need either `SELECT FOR UPDATE` to lock conflicting rows at read time, or a range exclusion constraint (`EXCLUDE USING gist (room_id WITH =, tsrange(start_time, end_time) WITH &&) WHERE (status = 'active')`) to enforce non-overlap at the database level. Without one of these, two concurrent booking requests can both pass the conflict check before either commits, resulting in a silent double-booking.

7. **Cancel-series endpoint re-uses `DELETE /bookings/{id}`.** The brief says "cancelling a recurring booking cancels all future instances." Rather than a separate endpoint, I detect whether the targeted booking belongs to a series and handle both cases. Any booking ID in a series acts as a handle to cancel that series' future instances. This keeps the API surface minimal.

---

## (b) Questions I would ask the PM before shipping

1. **R1/R2 threshold:** Is 2 the right number of skippable conflicts, or should it be configurable per room / per user role? (I hardcoded 2 to match the brief's wording.)

2. **Timezone source of truth:** Should the room carry a canonical timezone, or does the caller supply it per-booking? Right now both the room *and* the booking carry a timezone; they could diverge. Should the API enforce that booking.timezone == room.timezone?

3. **Cancel semantics — what is "now"?** "Future instances" means `start_time >= current timestamp at cancel time`. Is server wall-clock time correct, or should this be done relative to the user's local timezone?

4. **Partial-series cancel:** Can an admin cancel *one instance* from a recurring series without affecting the others? Currently `DELETE /bookings/{id}` on a series booking cancels the whole future tail. A "cancel single instance" path may be needed.

5. **Room numbering:** R5 says rooms are numbered 1..N sequentially. Does that mean IDs must be contiguous (no gaps)? My implementation uses auto-increment PKs, which could leave gaps if rooms are deleted.

6. **Authentication / authorisation:** Nothing in the brief mentions it. For an internal service this is a real concern — can any user cancel any booking?

---

## (c) Where AI helped, and anything it got wrong

- **AI helped with:** boilerplate (FastAPI/SQLAlchemy wiring, Pydantic schemas, pytest fixtures), structuring the project layout, and drafting this document.
- **Corrected:** An early AI draft used `existing.start_time <= new_start` for back-to-back detection, which would have falsely flagged back-to-back bookings as conflicts. Fixed to strict `<` / `>` comparisons per R4.
- **Corrected:** The initial plan proposed storing `datetime` objects; switched to storing naive ISO strings as required by C1.

---

## (d) Deliberately left out, and what I would do next

- **Authentication.** No auth layer — would add OAuth2/JWT for a real deployment.
- **Pagination on `GET /bookings`.** The endpoint returns all matching bookings; for production add `limit`/`offset` or cursor-based pagination.
- **`GET /bookings/{id}` single-booking fetch.** Not included; straightforward to add.
- **Async DB driver.** Using synchronous SQLAlchemy for simplicity; would migrate to `asyncpg` + async SQLAlchemy for higher throughput.
- **Migrations.** Tables are created with `create_all` on startup. For production I would use Alembic to manage schema changes.
- **OpenAPI client generation.** FastAPI already emits a spec at `/openapi.json`; the downstream teams (reporting, dashboard) could generate typed clients from it.
