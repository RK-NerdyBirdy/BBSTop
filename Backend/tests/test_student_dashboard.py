from sqlalchemy import select

from app.core.database import async_session_maker
from app.models import Student, StudentSemesterAssignment, SwapRequest

ABSTRACT = "A long enough abstract describing the project in some detail."


async def test_dashboard_summarises_swaps_projects_and_invites(client, world):
    # Swaps open in the fixture semester; S01 asks S02 (different batch, same CGPA)
    await client.patch(f"/api/admin/semesters/{world.semester_id}/swap-allowed", headers=world.hod, json={"swap_allowed": True})
    async with async_session_maker() as db:
        rows = {a.register_number: a for a in (await db.execute(
            select(StudentSemesterAssignment).where(StudentSemesterAssignment.semester_id == world.semester_id)
        )).scalars().all()}
        db.add(SwapRequest(requester_assignment_id=rows["S01"].assignment_id, target_assignment_id=rows["S02"].assignment_id, status="PENDING"))
        (await db.get(Student, "S02")).phone_number = None
        await db.commit()
    # S02 is invited to S03's team
    await client.post("/api/student/projects/inhouse", headers=world.student(3), json={
        "semester_id": world.semester_id, "project_subtype": "CAPSTONE", "title": "Team project",
        "abstract": ABSTRACT, "faculty_id": 1, "teammates": ["S02"],
    })

    d = (await client.get("/api/student/dashboard", headers=world.student(2))).json()
    assert d["student"]["register_number"] == "S02"
    assert d["student"]["has_phone"] is False and d["student"]["open_to_swap"] is True
    # Only current semesters; the fixture's closed semester is not archived so both show, newest first
    names = [s["semester_name"] for s in d["semesters"]]
    assert names == ["Closed", "Open"]
    assert [r["other_register_number"] for r in d["incoming_pending"]] == ["S01"]
    assert d["outgoing_active"] == []
    assert d["project_invites"] == 1
    assert d["eligible_count"] > 0

    sender = (await client.get("/api/student/dashboard", headers=world.student(1))).json()
    assert [r["other_register_number"] for r in sender["outgoing_active"]] == ["S02"]
    assert sender["incoming_pending"] == []

    leader = (await client.get("/api/student/dashboard", headers=world.student(3))).json()
    project = next(s["project"] for s in leader["semesters"] if s["semester_id"] == world.semester_id)
    assert project["status"] == "FORMING" and project["guide_name"] == "Guide One"


async def test_dashboard_hides_archived_semesters(client, world):
    await client.patch(f"/api/admin/semesters/{world.semester_id}/archive", headers=world.hod, json={"archived": True})
    d = (await client.get("/api/student/dashboard", headers=world.student(1))).json()
    assert [s["semester_id"] for s in d["semesters"]] == [world.closed_semester_id]
