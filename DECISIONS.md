# DECISIONS.md

## (a) Decisions the brief left open

1. **REST API, not CLI.** The integration constraints describe live HTTP consumers — a nightly reporting job parsing API timestamps (C1) and a facilities dashboard calling `GET /rooms` (C2). A CLI cannot satisfy either contract, so FastAPI. `POST /rooms` is a seeding convenience only; the spec treats rooms as pre-existing data (the C2 sample's 4 rooms are seeded in `seed_data.py`).

2. **R1 vs R2 resolution.** They appear contradictory: R1 says all-or-nothing, R2 says skip clashes. My reading: R2 is a business exception to R1 — if ≤2 instances conflict, skip them and create the rest; if >2 conflict (or zero instances remain), abort the whole series and save nothing. Relatedly, recurring duration is capped at 7 days, otherwise a series' own instances would overlap each other (exactly 7 days is allowed — back-to-back per R4).

3. **DST / the Denver "hour off" bug.** Recurrence uses naive local datetimes with `+timedelta(weeks=1)`, which always lands on the same wall-clock time (R3). The old bug was almost certainly UTC arithmetic (+7×24h), which shifts an hour at DST transitions. To protect C1 and naive string comparison, the API rejects offset-bearing timestamps (`+02:00` → 422) and normalizes short forms (`T09:00` → `T09:00:00`) before storage.

4. **Double-booking under concurrency.** Check-then-insert is not atomic by default — SQLite's DEFERRED transactions allow concurrent readers, so two simultaneous requests could both pass the conflict SELECT before either inserts. The engine issues `BEGIN IMMEDIATE` at transaction start (`app/database.py`), serializing writers so the check and insert run under the write lock. Fine for ~200 employees; a PostgreSQL migration should replace this with a range exclusion constraint (`EXCLUDE USING gist (room_id WITH =, tsrange(start, end) WITH &&) WHERE (status = 'active')`).

5. **Cancellation model.** `DELETE /bookings/{id}` handles both cases: a single booking cancels itself; a series booking is a handle to its `series_id`, cancelling all future instances (`start_time >= now`) while past ones stay intact. "Now" is evaluated in the booking's own timezone — a Denver series cancelled from a Berlin-hosted server would otherwise cut off hours wrong. Single bookings have `series_id = NULL`.

## (b) Questions for the PM before shipping

6. Is the skip threshold of 2 (R2) fixed, or configurable per room/role? Should the room's timezone be the source of truth instead of the caller supplying one per booking (they can currently diverge)? Does an in-progress meeting count as past (current behavior: yes, it survives a series cancel)?

7. Can someone cancel a *single* instance of a series without killing the future tail? And who is authorized to cancel whose bookings — the brief has no auth model at all.

## (c) Where AI helped, and what it got wrong

8. AI wrote most boilerplate (FastAPI/SQLAlchemy wiring, schemas, test fixtures) and drafted this document. Three corrections worth noting: (1) its first conflict check used `<=`, which would have flagged back-to-back bookings as conflicts, violating R4; (2) it claimed SQLite's write lock made check-then-insert safe against races — wrong, DEFERRED transactions allow concurrent readers, fixed with `BEGIN IMMEDIATE`; (3) its first validator accepted any `fromisoformat`-parseable string, letting offset timestamps into the DB in violation of C1.

## (d) Deliberately left out, and next steps

9. **Left out:** an availability endpoint (R5's "iterate rooms 1..N" is a hint, not a required feature), authentication, pagination on `GET /bookings`, Alembic migrations (tables come from `create_all`), and an async DB driver.

10. **Next:** migrate to PostgreSQL with the exclusion constraint from (4), add `GET /availability?start=...&end=...` iterating rooms 1..N, and add auth so cancellation can be restricted to the booking owner or an admin.
