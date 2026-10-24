import asyncio

import pytest

PDF = b"%PDF-1.4\n% test proof\n"
ABSTRACT = "A long enough abstract describing the project in some detail."


async def create_team(client, w, leader: int, teammates: list[int] = (), faculty_id: int = 1, subtype="CAPSTONE"):
    r = await client.post("/api/student/projects/inhouse", headers=w.student(leader), json={
        "semester_id": w.semester_id,
        "project_subtype": subtype,
        "title": f"Project by {leader}",
        "abstract": ABSTRACT,
        "faculty_id": faculty_id,
        "teammates": [w.reg(t) for t in teammates],
    })
    return r


async def invite_id(client, w, student: int, application_id: int) -> int:
    r = await client.get("/api/student/projects/invites", headers=w.student(student))
    return next(i["member_id"] for i in r.json() if i["application"]["application_id"] == application_id)


async def approved_team(client, w, leader: int, teammates: list[int] = (), faculty_id: int = 1) -> dict:
    app = (await create_team(client, w, leader, teammates, faculty_id)).json()
    for t in teammates:
        r = await client.post(f"/api/student/projects/invites/{await invite_id(client, w, t, app['application_id'])}/accept", headers=w.student(t))
        assert r.status_code == 200, r.text
    r = await client.post(f"/api/guide/applications/{app['application_id']}/approve", headers=w.guide(faculty_id), json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "GUIDE_APPROVED"
    return r.json()


async def upload_proof(client, w, student: int) -> int:
    r = await client.post("/api/student/projects/documents", headers=w.student(student), files={"file": ("offer.pdf", PDF, "application/pdf")})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def statuses(app: dict) -> dict:
    return {m["register_number"]: m["status"] for m in app["members"]}


# ---------------------------------------------------------------- registration & consent

async def test_solo_project_goes_straight_to_guide(client, world):
    r = await create_team(client, world, 1)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "PENDING_GUIDE"
    assert r.json()["guide"]["employee_id"] == "E1"


async def test_team_waits_for_every_invitee(client, world):
    app = (await create_team(client, world, 1, [2, 3])).json()
    assert app["status"] == "FORMING"

    r = await client.post(f"/api/student/projects/invites/{await invite_id(client, world, 2, app['application_id'])}/accept", headers=world.student(2))
    assert r.json()["status"] == "FORMING"
    r = await client.post(f"/api/student/projects/invites/{await invite_id(client, world, 3, app['application_id'])}/accept", headers=world.student(3))
    assert r.json()["status"] == "PENDING_GUIDE"
    assert set(statuses(r.json()).values()) == {"ACCEPTED"}


async def test_team_size_capped(client, world):
    r = await create_team(client, world, 1, [2, 3, 4])
    assert r.status_code == 400
    assert "at most 3" in r.json()["detail"]


async def test_hod_can_change_team_size_rule(client, world):
    r = await client.put("/api/admin/settings", headers=world.hod, json={"max_team_size": 4})
    assert r.json()["max_team_size"] == 4
    assert (await create_team(client, world, 1, [2, 3, 4])).status_code == 201


async def test_one_active_application_per_student(client, world):
    await approved_team(client, world, 1, [2])
    assert (await create_team(client, world, 1)).status_code == 409
    r = await create_team(client, world, 3, [2])
    assert r.status_code == 409
    assert "already in a team" in r.json()["detail"]


async def test_accepting_one_invite_declines_the_others(client, world):
    a = (await create_team(client, world, 1, [3])).json()
    b = (await create_team(client, world, 2, [3])).json()
    await client.post(f"/api/student/projects/invites/{await invite_id(client, world, 3, a['application_id'])}/accept", headers=world.student(3))

    r = await client.get(f"/api/student/projects/{b['application_id']}", headers=world.student(2))
    assert statuses(r.json())[world.reg(3)] == "DECLINED"
    assert (await client.get("/api/student/projects/invites", headers=world.student(3))).json() == []


async def test_declined_invite_then_leader_submits_smaller_team(client, world):
    app = (await create_team(client, world, 1, [2])).json()
    r = await client.post(f"/api/student/projects/invites/{await invite_id(client, world, 2, app['application_id'])}/decline", headers=world.student(2))
    assert r.json()["status"] == "FORMING"

    r = await client.post(f"/api/student/projects/{app['application_id']}/submit", headers=world.student(1))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING_GUIDE"


async def test_cannot_submit_while_invites_pending(client, world):
    app = (await create_team(client, world, 1, [2])).json()
    r = await client.post(f"/api/student/projects/{app['application_id']}/submit", headers=world.student(1))
    assert r.status_code == 400


async def test_registration_closed(client, world):
    r = await client.post("/api/student/projects/inhouse", headers=world.student(1), json={
        "semester_id": world.closed_semester_id, "project_subtype": "SAP", "title": "Closed",
        "abstract": ABSTRACT, "faculty_id": 1, "teammates": [],
    })
    assert r.status_code == 400
    assert "closed" in r.json()["detail"]


async def test_lookup_autofills_name_and_availability(client, world):
    r = await client.get("/api/student/projects/lookup", headers=world.student(1), params={"semester_id": world.semester_id, "register_number": "s02"})
    assert r.json()["student_name"] == "Student 2"
    assert r.json()["available"] is True
    r = await client.get("/api/student/projects/lookup", headers=world.student(1), params={"semester_id": world.semester_id, "register_number": "NOPE"})
    assert r.status_code == 404


# ---------------------------------------------------------------- withdraw / leave

async def test_only_leader_withdraws_and_members_are_freed(client, world):
    app = (await create_team(client, world, 1, [2])).json()
    await client.post(f"/api/student/projects/invites/{await invite_id(client, world, 2, app['application_id'])}/accept", headers=world.student(2))

    r = await client.post(f"/api/student/projects/{app['application_id']}/withdraw", headers=world.student(2))
    assert r.status_code == 403
    r = await client.post(f"/api/student/projects/{app['application_id']}/withdraw", headers=world.student(1))
    assert r.json()["status"] == "WITHDRAWN"
    assert set(statuses(r.json()).values()) == {"RELEASED"}
    # both can register again
    assert (await create_team(client, world, 2, [1])).status_code == 201


async def test_cannot_withdraw_after_guide_approval(client, world):
    app = await approved_team(client, world, 1)
    r = await client.post(f"/api/student/projects/{app['application_id']}/withdraw", headers=world.student(1))
    assert r.status_code == 400


async def test_member_leaving_pending_application_sends_it_back_to_leader(client, world):
    app = (await create_team(client, world, 1, [2])).json()
    await client.post(f"/api/student/projects/invites/{await invite_id(client, world, 2, app['application_id'])}/accept", headers=world.student(2))
    r = await client.post(f"/api/student/projects/{app['application_id']}/leave", headers=world.student(2))
    assert r.json()["status"] == "FORMING"
    assert world.reg(2) not in statuses(r.json())


# ---------------------------------------------------------------- guide review & cap

async def test_guide_reject_then_resubmit(client, world):
    app = (await create_team(client, world, 1)).json()
    r = await client.post(f"/api/guide/applications/{app['application_id']}/reject", headers=world.guide(1), json={"remarks": "Too broad"})
    assert r.json()["status"] == "GUIDE_REJECTED"
    assert r.json()["guide_remarks"] == "Too broad"

    r = await client.patch(f"/api/student/projects/{app['application_id']}", headers=world.student(1), json={"title": "Narrower scope"})
    assert r.json()["title"] == "Narrower scope"
    r = await client.post(f"/api/student/projects/{app['application_id']}/submit", headers=world.student(1))
    assert r.json()["status"] == "PENDING_GUIDE"
    assert r.json()["guide_remarks"] is None


async def test_reject_requires_remarks(client, world):
    app = (await create_team(client, world, 1)).json()
    r = await client.post(f"/api/guide/applications/{app['application_id']}/reject", headers=world.guide(1), json={})
    assert r.status_code == 422


async def test_guide_cannot_review_other_guides_application(client, world):
    app = (await create_team(client, world, 1, faculty_id=1)).json()
    r = await client.post(f"/api/guide/applications/{app['application_id']}/approve", headers=world.guide(2), json={})
    assert r.status_code == 403


async def test_reaching_cap_closes_other_pending_applications(client, world):
    first = (await create_team(client, world, 1, faculty_id=2)).json()
    second = (await create_team(client, world, 2, faculty_id=2)).json()

    r = await client.post(f"/api/guide/applications/{first['application_id']}/approve", headers=world.guide(2), json={})
    assert r.json()["status"] == "GUIDE_APPROVED"
    r = await client.get(f"/api/student/projects/{second['application_id']}", headers=world.student(2))
    assert r.json()["status"] == "GUIDE_FULL"

    # full guide can't be picked; another guide can
    r = await client.post(f"/api/student/projects/{second['application_id']}/submit", headers=world.student(2))
    assert r.status_code == 409
    await client.patch(f"/api/student/projects/{second['application_id']}", headers=world.student(2), json={"faculty_id": 1})
    r = await client.post(f"/api/student/projects/{second['application_id']}/submit", headers=world.student(2))
    assert r.json()["status"] == "PENDING_GUIDE"

    guides = (await client.get("/api/student/projects/guides", headers=world.student(3), params={"semester_id": world.semester_id})).json()
    assert next(g for g in guides if g["faculty_id"] == 2)["slots_left"] == 0
    assert (await create_team(client, world, 3, faculty_id=2)).status_code == 409


async def test_pending_applications_do_not_count_toward_cap(client, world):
    for leader in (1, 2, 3):
        assert (await create_team(client, world, leader, faculty_id=2)).status_code == 201
    dash = (await client.get("/api/guide/dashboard", headers=world.guide(2))).json()[0]
    assert dash["cap"] == 1 and dash["approved_count"] == 0 and dash["pending_count"] == 3


async def test_concurrent_approvals_cannot_exceed_cap(client, world):
    # Guide 3 has cap 2, three pending applications, three simultaneous approvals
    ids = [(await create_team(client, world, leader, faculty_id=3)).json()["application_id"] for leader in (1, 2, 3)]
    results = await asyncio.gather(*[
        client.post(f"/api/guide/applications/{i}/approve", headers=world.guide(3), json={}) for i in ids
    ])
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 2, [r.text for r in results]
    projects = (await client.get("/api/admin/projects", headers=world.hod, params={"faculty_id": 3})).json()
    assert len(projects) == 2


async def test_hod_sees_only_approved_by_default(client, world):
    await approved_team(client, world, 1)
    await create_team(client, world, 2)
    projects = (await client.get("/api/admin/projects", headers=world.hod)).json()
    assert [p["leader_register_number"] for p in projects] == [world.reg(1)]


# ---------------------------------------------------------------- CDC

async def test_direct_cdc_needs_proof_and_hod_approval(client, world):
    doc = await upload_proof(client, world, 1)
    r = await client.post("/api/student/projects/cdc", headers=world.student(1), json={"semester_id": world.semester_id, "document_id": doc})
    assert r.json()["status"] == "PENDING_HOD"
    assert r.json()["guide"] is None and r.json()["title"] is None
    app_id = r.json()["application_id"]

    assert len((await client.get("/api/admin/cdc", headers=world.hod)).json()) == 1
    r = await client.post(f"/api/admin/cdc/{app_id}/reject", headers=world.hod, json={"remarks": "Offer letter unreadable"})
    assert r.json()["status"] == "CDC_REJECTED"
    r = await client.post(f"/api/student/projects/{app_id}/cdc-resubmit", headers=world.student(1), json={"document_id": await upload_proof(client, world, 1)})
    assert r.json()["status"] == "PENDING_HOD"
    r = await client.post(f"/api/admin/cdc/{app_id}/approve", headers=world.hod, json={})
    assert r.json()["status"] == "CDC_APPROVED"


async def test_proof_must_be_pdf_or_image_and_own(client, world):
    r = await client.post("/api/student/projects/documents", headers=world.student(1), files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400
    doc = await upload_proof(client, world, 1)
    r = await client.post("/api/student/projects/cdc", headers=world.student(2), json={"semester_id": world.semester_id, "document_id": doc})
    assert r.status_code == 400
    assert (await client.get(f"/api/student/projects/documents/{doc}", headers=world.student(2))).status_code == 404
    assert (await client.get(f"/api/admin/documents/{doc}", headers=world.hod)).content == PDF


async def convert(client, w, student: int, application_id: int) -> dict:
    r = await client.post(f"/api/student/projects/{application_id}/change-requests", headers=w.student(student), json={
        "request_type": "CDC_CONVERSION", "reason": "Got an internship offer", "document_id": await upload_proof(client, w, student),
    })
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/admin/change-requests/{r.json()['request_id']}/approve", headers=w.hod, json={})
    assert r.status_code == 200, r.text
    return r.json()


async def test_cdc_conversion_keeps_project_when_teammates_remain(client, world):
    app = await approved_team(client, world, 1, [2])
    await convert(client, world, 2, app["application_id"])

    project = (await client.get(f"/api/admin/projects/{app['application_id']}", headers=world.hod)).json()
    assert project["status"] == "GUIDE_APPROVED"
    assert [m["register_number"] for m in project["members"] if m["status"] == "ACCEPTED"] == [world.reg(1)]

    cdc = (await client.get("/api/admin/projects", headers=world.hod, params={"project_type": "CDC"})).json()
    assert [c["leader_register_number"] for c in cdc] == [world.reg(2)]
    assert cdc[0]["status"] == "CDC_APPROVED"

    activity = (await client.get("/api/guide/activity", headers=world.guide(1))).json()
    assert any(a["action"] == "application.cdc_conversion" for a in activity)


async def test_cdc_conversion_of_leader_hands_over_leadership(client, world):
    app = await approved_team(client, world, 1, [2, 3])
    await convert(client, world, 1, app["application_id"])
    project = (await client.get(f"/api/admin/projects/{app['application_id']}", headers=world.hod)).json()
    assert project["leader_register_number"] == world.reg(2)
    assert project["status"] == "GUIDE_APPROVED"


async def test_cdc_conversion_of_last_member_dissolves_and_frees_slot(client, world):
    app = await approved_team(client, world, 1, faculty_id=2)  # guide 2 has cap 1, now full
    assert (await create_team(client, world, 3, faculty_id=2)).status_code == 409

    await convert(client, world, 1, app["application_id"])
    project = (await client.get(f"/api/admin/projects/{app['application_id']}", headers=world.hod)).json()
    assert project["status"] == "DISSOLVED"
    assert (await create_team(client, world, 3, faculty_id=2)).status_code == 201


# ---------------------------------------------------------------- change requests

async def test_change_requests_only_after_approval(client, world):
    app = (await create_team(client, world, 1)).json()
    r = await client.post(f"/api/student/projects/{app['application_id']}/change-requests", headers=world.student(1), json={
        "request_type": "TITLE_ABSTRACT", "reason": "Scope changed a lot", "title": "New title",
    })
    assert r.status_code == 400


async def test_team_change_needs_new_member_consent(client, world):
    app = await approved_team(client, world, 1, [2])
    r = await client.post(f"/api/student/projects/{app['application_id']}/change-requests", headers=world.student(1), json={
        "request_type": "TEAM_CHANGE", "reason": "Replacing a teammate", "add": [world.reg(4)], "remove": [world.reg(2)],
    })
    assert r.json()["status"] == "PENDING_CONSENT"
    request_id = r.json()["request_id"]

    pending = (await client.get("/api/student/projects/change-requests/consents", headers=world.student(4))).json()
    assert [p["request_id"] for p in pending] == [request_id]
    r = await client.post(f"/api/student/projects/change-requests/{request_id}/consent", headers=world.student(4), params={"accept": True})
    assert r.json()["status"] == "PENDING_HOD"

    r = await client.post(f"/api/admin/change-requests/{request_id}/approve", headers=world.hod, json={})
    assert r.json()["status"] == "APPROVED"
    project = (await client.get(f"/api/admin/projects/{app['application_id']}", headers=world.hod)).json()
    assert sorted(m["register_number"] for m in project["members"] if m["status"] == "ACCEPTED") == [world.reg(1), world.reg(4)]


async def test_only_leader_raises_team_requests(client, world):
    app = await approved_team(client, world, 1, [2])
    r = await client.post(f"/api/student/projects/{app['application_id']}/change-requests", headers=world.student(2), json={
        "request_type": "TEAM_DELETION", "reason": "We want to stop the project",
    })
    assert r.status_code == 403


async def test_guide_change_respects_new_guide_cap(client, world):
    await approved_team(client, world, 5, faculty_id=2)  # guide 2 full
    app = await approved_team(client, world, 1)
    r = await client.post(f"/api/student/projects/{app['application_id']}/change-requests", headers=world.student(1), json={
        "request_type": "GUIDE_CHANGE", "reason": "Better domain match", "to_faculty_id": 2,
    })
    assert r.status_code == 409

    r = await client.post(f"/api/student/projects/{app['application_id']}/change-requests", headers=world.student(1), json={
        "request_type": "GUIDE_CHANGE", "reason": "Better domain match", "to_faculty_id": 3,
    })
    await client.post(f"/api/admin/change-requests/{r.json()['request_id']}/approve", headers=world.hod, json={})
    project = (await client.get(f"/api/admin/projects/{app['application_id']}", headers=world.hod)).json()
    assert project["guide"]["faculty_id"] == 3


async def test_team_deletion_dissolves(client, world):
    app = await approved_team(client, world, 1, [2])
    r = await client.post(f"/api/student/projects/{app['application_id']}/change-requests", headers=world.student(1), json={
        "request_type": "TEAM_DELETION", "reason": "We want to stop the project",
    })
    r = await client.post(f"/api/admin/change-requests/{r.json()['request_id']}/reject", headers=world.hod, json={"remarks": "Talk to me first"})
    assert r.json()["status"] == "REJECTED"
    r = await client.post(f"/api/student/projects/{app['application_id']}/change-requests", headers=world.student(1), json={
        "request_type": "TEAM_DELETION", "reason": "We want to stop the project",
    })
    await client.post(f"/api/admin/change-requests/{r.json()['request_id']}/approve", headers=world.hod, json={})
    project = (await client.get(f"/api/admin/projects/{app['application_id']}", headers=world.hod)).json()
    assert project["status"] == "DISSOLVED"
    assert (await create_team(client, world, 2)).status_code == 201


# ---------------------------------------------------------------- audit & auth

async def test_every_step_is_audited(client, world):
    app = await approved_team(client, world, 1, [2])
    log = (await client.get("/api/admin/audit-log", headers=world.hod, params={"entity": "application", "entity_id": str(app["application_id"])})).json()
    actions = [item["action"] for item in log["items"]]
    assert {"application.create", "invite.accept", "application.guide_approve"} <= set(actions)


async def test_roles_are_separated(client, world):
    assert (await client.get("/api/guide/applications", headers=world.student(1))).status_code == 401
    assert (await client.get("/api/admin/projects", headers=world.guide(1))).status_code == 401
    assert (await client.get("/api/student/projects/registration", headers=world.guide(1))).status_code == 401


async def test_dev_login_disabled_in_tests(client, world):
    r = await client.post("/api/auth/dev-login", json={"email": "s1@vitstudent.ac.in"})
    assert r.status_code == 404
