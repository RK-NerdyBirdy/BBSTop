import io
import random

from openpyxl import Workbook
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models import SwapRequest, StudentSemesterAssignment
from app.services import batch_balancer as bb


# ---------------------------------------------------------------- balancer (pure)

def _students(n=195, seed=1):
    r = random.Random(seed)
    return [bb.StudentCgpa(f"R{i:03d}", round(min(10, max(2.4, r.gauss(7.8, 1.1))), 2)) for i in range(n)]


def test_fresh_assignment_is_balanced_and_reproducible():
    students = _students()
    cgpas = {s.register_number: s.cgpa for s in students}
    a = bb.fresh_assignment(students, [1, 2, 3], seed=7)
    sizes = [s.size for s in bb.batch_stats(a, cgpas, [1, 2, 3])]
    assert max(sizes) - min(sizes) <= 1
    assert bb.average_gap(a, cgpas, [1, 2, 3]) <= 0.05
    assert bb.fresh_assignment(students, [1, 2, 3], seed=7) == a
    assert bb.fresh_assignment(students, [1, 2, 3], seed=8) != a


def test_min_moves_fixes_skew_with_few_moves():
    students = _students()
    cgpas = {s.register_number: s.cgpa for s in students}
    balanced = bb.fresh_assignment(students, [1, 2, 3], seed=1)
    # knock five students out of place
    nudged = dict(balanced)
    for reg in [r for r, b in balanced.items() if b == 1][:5]:
        nudged[reg] = 2
    result = bb.rebalance_min_moves(nudged, cgpas, [1, 2, 3], seed=1)
    assert bb.average_gap(result, cgpas, [1, 2, 3]) <= 0.05
    sizes = [s.size for s in bb.batch_stats(result, cgpas, [1, 2, 3])]
    assert max(sizes) - min(sizes) <= 2
    assert len(bb.moved(nudged, result)) <= 10


# ---------------------------------------------------------------- helpers

async def set_cohort(client, world, semester_id, year):
    r = await client.patch(f"/api/admin/semesters/{semester_id}/cohort", headers=world.hod, json={"cohort_year": year})
    assert r.status_code == 200, r.text


async def rollover(client, world, **overrides):
    body = {"source_semester_id": world.semester_id, "semester_name": "Next term", "batch_names": ["B1", "B2", "B3"]}
    body.update(overrides)
    return await client.post("/api/admin/semesters/rollover", headers=world.hod, json=body)


def csv_file(rows, header="register_number,cgpa"):
    text = header + "\n" + "\n".join(f"{r},{c}" for r, c in rows)
    return {"file": ("cgpa.csv", text.encode(), "text/csv")}


def xlsx_file(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(["S.No", "Reg. No", "Name", "CGPA"])
    for i, (r, c) in enumerate(rows, start=1):
        ws.append([i, r, "x", c])
    buf = io.BytesIO()
    wb.save(buf)
    return {"file": ("results.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}


# ---------------------------------------------------------------- roll-over & archive

async def test_rollover_copies_students_balances_and_archives(client, world):
    await set_cohort(client, world, world.semester_id, 2024)
    r = await rollover(client, world, exclude=["S10"])
    assert r.status_code == 201, r.text
    new = r.json()["semester"]
    assert new["cohort_year"] == 2024 and not new["archived"] and not new["assignment_locked"]
    assert r.json()["students"] == 9
    assert sorted(b["size"] for b in r.json()["batches"]) == [3, 3, 3]

    semesters = {s["semester_id"]: s for s in (await client.get("/api/admin/semesters", headers=world.hod)).json()}
    assert semesters[world.semester_id]["archived"] is True

    # students only see the new semester
    batch = (await client.get("/api/student/me/batch", headers=world.student(1))).json()
    assert {b["semester_id"] for b in batch} == {new["semester_id"], world.closed_semester_id}
    reg = (await client.get("/api/student/projects/registration", headers=world.student(1))).json()
    assert world.semester_id not in {s["semester_id"] for s in reg}
    # S10 was excluded
    assert {b["semester_id"] for b in (await client.get("/api/student/me/batch", headers=world.student(10))).json()} == {world.closed_semester_id}


async def test_one_current_semester_per_year_group(client, world):
    await set_cohort(client, world, world.semester_id, 2024)
    r = await client.post("/api/admin/semesters", headers=world.hod, json={"semester_name": "Dup", "cohort_year": 2024})
    assert r.status_code == 409
    await rollover(client, world)
    # un-archiving the old one would make two current semesters
    r = await client.patch(f"/api/admin/semesters/{world.semester_id}/archive", headers=world.hod, json={"archived": False})
    assert r.status_code == 409


async def test_archived_semester_is_read_only(client, world):
    app = (await client.post("/api/student/projects/inhouse", headers=world.student(1), json={
        "semester_id": world.semester_id, "project_subtype": "SAP", "title": "Old",
        "abstract": "An abstract that is definitely long enough.", "faculty_id": 1, "teammates": [],
    })).json()
    await client.patch(f"/api/admin/semesters/{world.semester_id}/archive", headers=world.hod, json={"archived": True})

    r = await client.post(f"/api/guide/applications/{app['application_id']}/approve", headers=world.guide(1), json={})
    assert r.status_code == 400 and "archived" in r.json()["detail"]
    r = await client.patch(f"/api/admin/semesters/{world.semester_id}/registration-open", headers=world.hod, json={"registration_open": True})
    assert r.status_code == 400
    # HOD and guide still see the record; the student no longer does
    assert (await client.get(f"/api/admin/projects/{app['application_id']}", headers=world.hod)).status_code == 200
    assert any(a["application_id"] == app["application_id"] for a in (await client.get("/api/guide/applications", headers=world.guide(1))).json())
    assert (await client.get(f"/api/student/projects/{app['application_id']}", headers=world.student(1))).status_code == 404


async def test_archiving_cancels_open_swaps(client, world):
    async with async_session_maker() as db:
        rows = (await db.execute(select(StudentSemesterAssignment).where(StudentSemesterAssignment.semester_id == world.semester_id))).scalars().all()
        db.add(SwapRequest(requester_assignment_id=rows[0].assignment_id, target_assignment_id=rows[1].assignment_id, status="PENDING"))
        await db.commit()
    await client.patch(f"/api/admin/semesters/{world.semester_id}/archive", headers=world.hod, json={"archived": True})
    async with async_session_maker() as db:
        assert (await db.execute(select(SwapRequest.status))).scalar_one() == "CANCELLED"


# ---------------------------------------------------------------- batch assignment

async def test_fresh_assignment_preview_apply_and_undo(client, world):
    # the fixture semester starts unlocked
    r = await client.post(f"/api/admin/semesters/{world.semester_id}/assign-batches/preview", headers=world.hod, json={"mode": "fresh", "seed": 5})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert sorted(b["size"] for b in preview["after"]) == [2, 2, 3, 3]

    before = (await client.get(f"/api/admin/semesters/{world.semester_id}/students", headers=world.hod)).json()["students"]
    r = await client.post(f"/api/admin/semesters/{world.semester_id}/assign-batches/apply", headers=world.hod, json={
        "mode": "fresh", "seed": 5, "fingerprint": preview["fingerprint"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["after"] == preview["after"]

    r = await client.post(f"/api/admin/semesters/{world.semester_id}/undo/batches", headers=world.hod)
    assert r.json()["restored"] == len(preview["moves"])
    after_undo = (await client.get(f"/api/admin/semesters/{world.semester_id}/students", headers=world.hod)).json()["students"]
    assert {s["register_number"]: s["batch_id"] for s in after_undo} == {s["register_number"]: s["batch_id"] for s in before}
    assert (await client.post(f"/api/admin/semesters/{world.semester_id}/undo/batches", headers=world.hod)).status_code == 404


async def test_stale_preview_is_refused(client, world):
    preview = (await client.post(f"/api/admin/semesters/{world.semester_id}/assign-batches/preview", headers=world.hod, json={"mode": "fresh"})).json()
    await client.post(f"/api/admin/semesters/{world.semester_id}/cgpa/apply", headers=world.hod, files=csv_file([("S01", 9.99)]))
    r = await client.post(f"/api/admin/semesters/{world.semester_id}/assign-batches/apply", headers=world.hod, json={
        "mode": "fresh", "seed": preview["seed"], "fingerprint": preview["fingerprint"],
    })
    assert r.status_code == 409


async def test_opening_semester_locks_fresh_reshuffle(client, world):
    await client.patch(f"/api/admin/semesters/{world.semester_id}/registration-open", headers=world.hod, json={"registration_open": True})
    r = await client.post(f"/api/admin/semesters/{world.semester_id}/assign-batches/preview", headers=world.hod, json={"mode": "fresh"})
    assert r.status_code == 400
    r = await client.post(f"/api/admin/semesters/{world.semester_id}/assign-batches/preview", headers=world.hod, json={"mode": "min_moves"})
    assert r.status_code == 200


async def test_bulk_reassign_blocked_while_swaps_open(client, world):
    await client.patch(f"/api/admin/semesters/{world.semester_id}/swap-allowed", headers=world.hod, json={"swap_allowed": True})
    r = await client.post(f"/api/admin/semesters/{world.semester_id}/assign-batches/preview", headers=world.hod, json={"mode": "min_moves"})
    assert r.status_code == 400 and "Close swaps" in r.json()["detail"]


async def test_manual_move_cancels_that_students_swaps(client, world):
    data = (await client.get(f"/api/admin/semesters/{world.semester_id}/students", headers=world.hod)).json()
    s1, s2 = data["students"][0], data["students"][1]
    async with async_session_maker() as db:
        db.add(SwapRequest(requester_assignment_id=s1["assignment_id"], target_assignment_id=s2["assignment_id"], status="PENDING"))
        await db.commit()
    other = next(b["batch_id"] for b in data["batches"] if b["batch_id"] != s1["batch_id"])
    r = await client.patch(f"/api/admin/assignments/{s1['assignment_id']}", headers=world.hod, json={"new_batch_id": other})
    assert r.status_code == 200, r.text
    assert len(r.json()["cancelled_swap_requests"]) == 1


# ---------------------------------------------------------------- CGPA update

async def test_cgpa_preview_reports_problems_and_blocks_apply(client, world):
    files = csv_file([("S01", 9.1), ("s02", "8.4"), ("S01", 7), ("NOPE", 8), ("S03", 11), ("S04", "abc")])
    r = await client.post(f"/api/admin/semesters/{world.semester_id}/cgpa/preview", headers=world.hod, files=files)
    preview = r.json()
    assert {c["register_number"] for c in preview["changed"]} == {"S01", "S02"}
    assert len(preview["problems"]) == 4
    assert len(preview["missing"]) == 10 - 4  # S01..S04 appear in the file
    r = await client.post(f"/api/admin/semesters/{world.semester_id}/cgpa/apply", headers=world.hod, files=files)
    assert r.status_code == 400


async def test_cgpa_apply_from_excel_and_undo(client, world):
    r = await client.post(f"/api/admin/semesters/{world.semester_id}/cgpa/apply", headers=world.hod, files=xlsx_file([("S01", 9.5), ("S02", 6.25)]))
    assert r.status_code == 200, r.text
    students = {s["register_number"]: s for s in (await client.get(f"/api/admin/semesters/{world.semester_id}/students", headers=world.hod)).json()["students"]}
    assert students["S01"]["cgpa"] == 9.5 and students["S02"]["cgpa"] == 6.25

    r = await client.post(f"/api/admin/semesters/{world.semester_id}/undo/cgpa", headers=world.hod)
    assert r.json() == {"restored": 2, "skipped": 0}
    students = {s["register_number"]: s for s in (await client.get(f"/api/admin/semesters/{world.semester_id}/students", headers=world.hod)).json()["students"]}
    assert students["S01"]["cgpa"] == 8.0


async def test_swap_rejected_when_cgpa_no_longer_eligible(client, world):
    await client.patch(f"/api/admin/semesters/{world.semester_id}/swap-allowed", headers=world.hod, json={"swap_allowed": True})
    data = (await client.get(f"/api/admin/semesters/{world.semester_id}/students", headers=world.hod)).json()["students"]
    a = next(s for s in data if s["register_number"] == "S01")
    b = next(s for s in data if s["batch_id"] != a["batch_id"])
    r = await client.post("/api/student/swap-requests", headers=world.student(1), json={
        "requester_assignment_id": a["assignment_id"], "target_assignment_id": b["assignment_id"],
    })
    assert r.status_code == 201, r.text

    # CGPA changes after the request was sent
    async with async_session_maker() as db:
        row = await db.get(StudentSemesterAssignment, a["assignment_id"])
        row.cgpa = 4.0
        await db.commit()
    target_n = int(b["register_number"][1:])
    r = await client.patch(f"/api/student/swap-requests/{r.json()['request_id']}/accept", headers=world.student(target_n))
    assert r.status_code == 400 and "no longer eligible" in r.json()["detail"]


async def test_swap_tolerance_is_a_rule(client, world):
    r = await client.put("/api/admin/settings", headers=world.hod, json={"swap_cgpa_tolerance": 0.5})
    assert r.json()["swap_cgpa_tolerance"] == 0.5
    assert (await client.put("/api/admin/settings", headers=world.hod, json={"swap_cgpa_tolerance": -1})).status_code == 422
