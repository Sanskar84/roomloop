"""
Seed script: populates 4 rooms and a set of demo bookings that exercise
each feature path — single, recurring (with one skip), and cancellation.

Run:
    python seed_data.py
"""
import httpx

BASE = "http://localhost:8000"


def post(path, payload):
    r = httpx.post(f"{BASE}{path}", json=payload)
    r.raise_for_status()
    return r.json()


def main():
    # Rooms (matching the C2 sample names)
    rooms = [
        {"name": "Aurora",  "capacity": 8,  "timezone": "Europe/Berlin"},
        {"name": "Basalt",  "capacity": 4,  "timezone": "Europe/Berlin"},
        {"name": "Cinder",  "capacity": 12, "timezone": "America/Denver"},
        {"name": "Dune",    "capacity": 6,  "timezone": "America/Denver"},
    ]
    room_ids = {}
    for room in rooms:
        r = post("/rooms", room)
        room_ids[room["name"]] = r["id"]
        print(f"  Room '{room['name']}' → id={r['id']}")

    print("\n── Single bookings ──")
    # Two back-to-back in Aurora (demonstrates R4)
    post("/bookings", {
        "room_id": room_ids["Aurora"],
        "user": "alice@company.com",
        "start_time": "2026-07-06T09:00:00",
        "end_time": "2026-07-06T10:00:00",
        "timezone": "Europe/Berlin",
    })
    post("/bookings", {
        "room_id": room_ids["Aurora"],
        "user": "bob@company.com",
        "start_time": "2026-07-06T10:00:00",
        "end_time": "2026-07-06T11:00:00",
        "timezone": "Europe/Berlin",
    })
    print("  Aurora: back-to-back 09:00-10:00 and 10:00-11:00 → both created")

    # Block one Monday slot in Basalt so the recurring booking skips it
    post("/bookings", {
        "room_id": room_ids["Basalt"],
        "user": "charlie@company.com",
        "start_time": "2026-07-13T14:00:00",
        "end_time": "2026-07-13T15:00:00",
        "timezone": "Europe/Berlin",
    })
    print("  Basalt: blocked 2026-07-13 14:00-15:00 (will cause 1 skip in recurring series)")

    print("\n── Recurring booking (with 1 skip) ──")
    r = httpx.post(f"{BASE}/bookings/recurring", json={
        "room_id": room_ids["Basalt"],
        "user": "diana@company.com",
        "start_time": "2026-07-06T14:00:00",
        "end_time": "2026-07-06T15:00:00",
        "timezone": "Europe/Berlin",
        "repeat_until": "2026-08-24",
    })
    r.raise_for_status()
    data = r.json()
    print(f"  Basalt weekly 14:00-15:00 → created={data['created']}, skipped={data['skipped']}")
    print(f"  Skipped dates: {data['skipped_dates']}")

    print("\n── Recurring booking in Denver (DST demo) ──")
    r = httpx.post(f"{BASE}/bookings/recurring", json={
        "room_id": room_ids["Cinder"],
        "user": "eve@company.com",
        "start_time": "2027-03-01T09:00:00",
        "end_time": "2027-03-01T10:00:00",
        "timezone": "America/Denver",
        "repeat_until": "2027-04-30",
    })
    r.raise_for_status()
    data = r.json()
    print(f"  Cinder weekly 09:00-10:00 (Denver) → {data['created']} bookings")
    for b in data["bookings"]:
        print(f"    {b['start_time']} – {b['end_time']}")

    print("\n── Cancel a single booking ──")
    all_bookings = httpx.get(f"{BASE}/bookings?room_id={room_ids['Aurora']}").json()
    bid = all_bookings[0]["id"]
    r = httpx.delete(f"{BASE}/bookings/{bid}")
    r.raise_for_status()
    print(f"  Cancelled booking id={bid}: {r.json()}")

    print("\nSeed complete.")


if __name__ == "__main__":
    main()
