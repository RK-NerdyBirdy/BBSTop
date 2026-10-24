"""Semester lifecycle: roll-over, archiving, batch assignment, CGPA updates and manual moves.

Lifecycle of a semester:
  setup (assignment_locked = false)  -> HOD may re-run a fresh batch assignment freely
  live  (swaps or registration opened at least once) -> only minimal-move rebalances
  archived -> hidden from students, read-only for everyone
"""

import hashlib
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.batch import Batch
from app.models.semester import Semester
from app.models.student import Student
from app.models.student_semester_assignment import StudentSemesterAssignment
from app.models.swap_request import SwapRequest
from app.services import batch_balancer
from app.services.audit_service import Actor, audit_service

ACTIVE_SWAP_STATUSES = ("PENDING", "ACCEPTED")


def _error(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


@dataclass
class SemesterStudent:
    assignment_id: int
    register_number: str
    student_name: str
    email: str
    batch_id: int
    batch_name: str
    cgpa: float


class SemesterService:
    # ------------------------------------------------------------------ reads

    @staticmethod
    async def lock(db: AsyncSession, semester_id: int) -> Semester:
        result = await db.execute(select(Semester).where(Semester.semester_id == semester_id).with_for_update())
        semester = result.scalar_one_or_none()
        if semester is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Semester not found")
        return semester

    @staticmethod
    def require_not_archived(semester: Semester) -> None:
        if semester.archived:
            raise _error(status.HTTP_400_BAD_REQUEST, "This semester is archived and read-only")

    @staticmethod
    async def batches(db: AsyncSession, semester_id: int) -> list[Batch]:
        result = await db.execute(select(Batch).where(Batch.semester_id == semester_id).order_by(Batch.batch_name))
        return list(result.scalars().all())

    @staticmethod
    async def students(db: AsyncSession, semester_id: int) -> list[SemesterStudent]:
        rows = await db.execute(
            select(
                StudentSemesterAssignment.assignment_id,
                StudentSemesterAssignment.register_number,
                Student.student_name,
                Student.email,
                StudentSemesterAssignment.batch_id,
                Batch.batch_name,
                StudentSemesterAssignment.cgpa,
            )
            .join(Student, Student.register_number == StudentSemesterAssignment.register_number)
            .join(Batch, Batch.batch_id == StudentSemesterAssignment.batch_id)
            .where(
                StudentSemesterAssignment.semester_id == semester_id,
                StudentSemesterAssignment.active.is_(True),
            )
            .order_by(StudentSemesterAssignment.register_number)
        )
        return [
            SemesterStudent(r.assignment_id, r.register_number, r.student_name, r.email, r.batch_id, r.batch_name, float(r.cgpa))
            for r in rows.all()
        ]

    @staticmethod
    async def active_swap_count(db: AsyncSession, semester_id: int) -> int:
        assignment_ids = select(StudentSemesterAssignment.assignment_id).where(
            StudentSemesterAssignment.semester_id == semester_id
        )
        return (await db.execute(
            select(func.count()).select_from(SwapRequest).where(
                SwapRequest.status.in_(ACTIVE_SWAP_STATUSES),
                or_(
                    SwapRequest.requester_assignment_id.in_(assignment_ids),
                    SwapRequest.target_assignment_id.in_(assignment_ids),
                ),
            )
        )).scalar_one()

    @staticmethod
    async def cancel_swaps(db: AsyncSession, assignment_ids: list[int]) -> list[int]:
        """Cancel open swap requests touching these assignments. Returns the cancelled request ids."""
        if not assignment_ids:
            return []
        result = await db.execute(
            select(SwapRequest)
            .where(
                SwapRequest.status.in_(ACTIVE_SWAP_STATUSES),
                or_(
                    SwapRequest.requester_assignment_id.in_(assignment_ids),
                    SwapRequest.target_assignment_id.in_(assignment_ids),
                ),
            )
            .with_for_update()
        )
        cancelled = []
        for request in result.scalars().all():
            request.status = "CANCELLED"
            cancelled.append(request.request_id)
        return cancelled

    @staticmethod
    async def current_semester_for_cohort(db: AsyncSession, cohort_year: int, exclude_id: int | None = None) -> Semester | None:
        stmt = select(Semester).where(Semester.cohort_year == cohort_year, Semester.archived.is_(False))
        if exclude_id is not None:
            stmt = stmt.where(Semester.semester_id != exclude_id)
        return (await db.execute(stmt.limit(1))).scalar_one_or_none()

    async def require_cohort_free(self, db: AsyncSession, cohort_year: int | None, exclude_id: int | None = None) -> None:
        if cohort_year is None:
            return
        other = await self.current_semester_for_cohort(db, cohort_year, exclude_id)
        if other is not None:
            raise _error(
                status.HTTP_409_CONFLICT,
                f"{other.semester_name} is still the current semester for the {cohort_year} year group. "
                "Archive it first, or use roll-over.",
            )

    @staticmethod
    async def _name_taken(db: AsyncSession, name: str) -> bool:
        result = await db.execute(select(Semester.semester_id).where(func.lower(Semester.semester_name) == name.lower()))
        return result.first() is not None

    # ------------------------------------------------------------------ archive

    async def archive(self, db: AsyncSession, actor: Actor, semester: Semester) -> list[int]:
        """Archive (caller holds the lock): close swaps/registration and cancel open swap requests."""
        ids = [s.assignment_id for s in await self.students(db, semester.semester_id)]
        cancelled = await self.cancel_swaps(db, ids)
        semester.archived = True
        semester.swap_allowed = False
        semester.registration_open = False
        audit_service.log(db, actor, "semester.archive", "semester", semester.semester_id, {"cancelled_swap_requests": cancelled})
        return cancelled

    async def set_archived(self, db: AsyncSession, actor: Actor, semester_id: int, archived: bool) -> Semester:
        semester = await self.lock(db, semester_id)
        if semester.archived == archived:
            return semester
        if archived:
            await self.archive(db, actor, semester)
        else:
            await self.require_cohort_free(db, semester.cohort_year, exclude_id=semester_id)
            semester.archived = False
            audit_service.log(db, actor, "semester.unarchive", "semester", semester_id, {})
        await db.commit()
        return semester

    # ------------------------------------------------------------------ roll-over

    async def rollover(
        self,
        db: AsyncSession,
        actor: Actor,
        source_id: int,
        name: str,
        cohort_year: int | None,
        batch_names: list[str],
        exclude: list[str],
    ) -> tuple[Semester, dict]:
        source = await self.lock(db, source_id)
        self.require_not_archived(source)
        name = name.strip()
        if await self._name_taken(db, name):
            raise _error(status.HTTP_409_CONFLICT, "A semester with this name already exists")
        batch_names = [b.strip() for b in batch_names if b.strip()]
        if not batch_names or len({b.lower() for b in batch_names}) != len(batch_names):
            raise _error(status.HTTP_400_BAD_REQUEST, "Give at least one batch, with unique names")
        cohort_year = cohort_year if cohort_year is not None else source.cohort_year
        if cohort_year != source.cohort_year:
            await self.require_cohort_free(db, cohort_year)

        exclude_set = {r.strip().upper() for r in exclude}
        students = [s for s in await self.students(db, source_id) if s.register_number not in exclude_set]
        if not students:
            raise _error(status.HTTP_400_BAD_REQUEST, "No students to copy")

        semester = Semester(semester_name=name, cohort_year=cohort_year, swap_allowed=False, registration_open=False)
        db.add(semester)
        await db.flush()
        batches = [Batch(batch_name=b, semester_id=semester.semester_id) for b in batch_names]
        db.add_all(batches)
        await db.flush()

        seed = secrets.randbelow(2**31)
        batch_ids = [b.batch_id for b in batches]
        mapping = batch_balancer.fresh_assignment(
            [batch_balancer.StudentCgpa(s.register_number, s.cgpa) for s in students], batch_ids, seed
        )
        db.add_all([
            StudentSemesterAssignment(
                register_number=s.register_number,
                semester_id=semester.semester_id,
                batch_id=mapping[s.register_number],
                cgpa=s.cgpa,
                active=True,
            )
            for s in students
        ])
        cancelled = await self.archive(db, actor, source)
        await db.flush()

        cgpas = {s.register_number: s.cgpa for s in students}
        stats = self._stats_payload(mapping, cgpas, batches)
        audit_service.log(db, actor, "semester.rollover", "semester", semester.semester_id, {
            "from_semester_id": source_id,
            "students": len(students),
            "excluded": sorted(exclude_set),
            "batches": batch_names,
            "seed": seed,
            "cancelled_swap_requests": cancelled,
        })
        await db.commit()
        return semester, {"students": len(students), "batches": stats, "seed": seed}

    # ------------------------------------------------------------------ batch assignment

    @staticmethod
    def _stats_payload(mapping: dict[str, int], cgpas: dict[str, float], batches: list[Batch]) -> list[dict]:
        names = {b.batch_id: b.batch_name for b in batches}
        return [
            {**s.__dict__, "batch_name": names[s.batch_id]}
            for s in batch_balancer.batch_stats(mapping, cgpas, [b.batch_id for b in batches])
        ]

    @staticmethod
    def _fingerprint(students: list[SemesterStudent], batch_ids: list[int]) -> str:
        """Identifies the exact data a preview was computed from, so apply can refuse stale previews."""
        raw = "|".join(f"{s.register_number}:{s.cgpa}:{s.batch_id}" for s in sorted(students, key=lambda s: s.register_number))
        raw += "#" + ",".join(map(str, sorted(batch_ids)))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def _guard_bulk_change(self, db: AsyncSession, semester: Semester, mode: str) -> None:
        self.require_not_archived(semester)
        if mode == "fresh" and semester.assignment_locked:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "This semester has already been opened to students, so only a minimal-move rebalance is allowed",
            )
        if semester.swap_allowed:
            raise _error(status.HTTP_400_BAD_REQUEST, "Close swaps for this semester first")
        active = await self.active_swap_count(db, semester.semester_id)
        if active:
            raise _error(
                status.HTTP_409_CONFLICT,
                f"{active} swap request(s) in this semester are still pending or accepted. Resolve them first.",
            )

    async def compute_assignment(self, db: AsyncSession, semester_id: int, mode: str, seed: int | None) -> dict:
        if mode not in ("fresh", "min_moves"):
            raise _error(status.HTTP_400_BAD_REQUEST, "mode must be 'fresh' or 'min_moves'")
        semester = await db.get(Semester, semester_id)
        if semester is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Semester not found")
        await self._guard_bulk_change(db, semester, mode)
        batches = await self.batches(db, semester_id)
        students = await self.students(db, semester_id)
        if not batches or not students:
            raise _error(status.HTTP_400_BAD_REQUEST, "The semester needs batches and students first")

        seed = seed if seed is not None else secrets.randbelow(2**31)
        batch_ids = [b.batch_id for b in batches]
        current = {s.register_number: s.batch_id for s in students}
        cgpas = {s.register_number: s.cgpa for s in students}
        if mode == "fresh":
            proposed = batch_balancer.fresh_assignment(
                [batch_balancer.StudentCgpa(s.register_number, s.cgpa) for s in students], batch_ids, seed
            )
        else:
            proposed = batch_balancer.rebalance_min_moves(current, cgpas, batch_ids, seed)
        moved = batch_balancer.moved(current, proposed)
        names = {b.batch_id: b.batch_name for b in batches}
        by_reg = {s.register_number: s for s in students}
        return {
            "mode": mode,
            "seed": seed,
            "fingerprint": self._fingerprint(students, batch_ids),
            "before": self._stats_payload(current, cgpas, batches),
            "after": self._stats_payload(proposed, cgpas, batches),
            "gap_before": batch_balancer.average_gap(current, cgpas, batch_ids),
            "gap_after": batch_balancer.average_gap(proposed, cgpas, batch_ids),
            "moves": [
                {
                    "register_number": reg,
                    "student_name": by_reg[reg].student_name,
                    "cgpa": cgpas[reg],
                    "from_batch": names[current[reg]],
                    "to_batch": names[proposed[reg]],
                }
                for reg in moved
            ],
            "_proposed": proposed,
            "_current": current,
        }

    async def apply_assignment(self, db: AsyncSession, actor: Actor, semester_id: int, mode: str, seed: int, fingerprint: str) -> dict:
        semester = await self.lock(db, semester_id)
        result = await self.compute_assignment(db, semester_id, mode, seed)
        if result["fingerprint"] != fingerprint:
            raise _error(status.HTTP_409_CONFLICT, "Students or CGPAs changed since the preview. Preview again.")
        proposed, current = result.pop("_proposed"), result.pop("_current")
        await self._write_batches(db, semester_id, proposed, current)
        audit_service.log(db, actor, "semester.batch_assign", "semester", semester.semester_id, {
            "mode": mode,
            "seed": seed,
            "gap_before": result["gap_before"],
            "gap_after": result["gap_after"],
            "moves": {reg: [current[reg], proposed[reg]] for reg in batch_balancer.moved(current, proposed)},
        })
        await db.commit()
        return result

    @staticmethod
    async def _write_batches(db: AsyncSession, semester_id: int, proposed: dict[str, int], current: dict[str, int]) -> None:
        changed = [reg for reg in proposed if proposed[reg] != current.get(reg)]
        if not changed:
            return
        result = await db.execute(
            select(StudentSemesterAssignment)
            .where(
                StudentSemesterAssignment.semester_id == semester_id,
                StudentSemesterAssignment.register_number.in_(changed),
            )
            .with_for_update()
        )
        for assignment in result.scalars().all():
            assignment.batch_id = proposed[assignment.register_number]

    # ------------------------------------------------------------------ manual move

    async def move_student(self, db: AsyncSession, actor: Actor, assignment_id: int, batch_id: int) -> dict:
        result = await db.execute(
            select(StudentSemesterAssignment).where(StudentSemesterAssignment.assignment_id == assignment_id).with_for_update()
        )
        assignment = result.scalar_one_or_none()
        if assignment is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Student is not in this semester")
        semester = await db.get(Semester, assignment.semester_id)
        self.require_not_archived(semester)
        batch = await db.get(Batch, batch_id)
        if batch is None or batch.semester_id != assignment.semester_id:
            raise _error(status.HTTP_400_BAD_REQUEST, "That batch isn't in this semester")
        if batch_id == assignment.batch_id:
            return {"cancelled_swap_requests": []}

        cancelled = await self.cancel_swaps(db, [assignment_id])
        old = assignment.batch_id
        assignment.batch_id = batch_id
        audit_service.log(db, actor, "semester.student_move", "semester", assignment.semester_id, {
            "register_number": assignment.register_number,
            "from_batch_id": old,
            "to_batch_id": batch_id,
            "cancelled_swap_requests": cancelled,
        })
        await db.commit()
        return {"cancelled_swap_requests": cancelled}

    # ------------------------------------------------------------------ CGPA update

    async def preview_cgpa(self, db: AsyncSession, semester_id: int, rows: list[tuple[int, str, str]]) -> dict:
        semester = await db.get(Semester, semester_id)
        if semester is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Semester not found")
        self.require_not_archived(semester)
        students = {s.register_number: s for s in await self.students(db, semester_id)}

        problems: list[dict] = []
        seen: dict[str, int] = {}
        updates: dict[str, float] = {}
        for row_number, reg, raw in rows:
            if reg in seen:
                problems.append({"row": row_number, "register_number": reg, "error": f"Duplicate of row {seen[reg]}"})
                continue
            seen[reg] = row_number
            if reg not in students:
                problems.append({"row": row_number, "register_number": reg, "error": "Not a student of this semester"})
                continue
            try:
                value = Decimal(raw).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                problems.append({"row": row_number, "register_number": reg, "error": f"'{raw}' is not a number"})
                continue
            if not Decimal(0) <= value <= Decimal(10):
                problems.append({"row": row_number, "register_number": reg, "error": f"{value} is outside 0-10"})
                continue
            updates[reg] = float(value)

        changes = sorted(
            (
                {
                    "register_number": reg,
                    "student_name": students[reg].student_name,
                    "old": students[reg].cgpa,
                    "new": new,
                }
                for reg, new in updates.items()
                if abs(new - students[reg].cgpa) > 1e-9
            ),
            key=lambda c: -abs(c["new"] - c["old"]),
        )
        missing = sorted(set(students) - set(seen))
        return {
            "semester_id": semester_id,
            "rows": len(rows),
            "changed": changes,
            "unchanged": len(updates) - len(changes),
            "problems": problems,
            "missing": [{"register_number": r, "student_name": students[r].student_name} for r in missing],
            "active_swap_requests": await self.active_swap_count(db, semester_id),
        }

    async def apply_cgpa(self, db: AsyncSession, actor: Actor, semester_id: int, rows: list[tuple[int, str, str]]) -> dict:
        semester = await self.lock(db, semester_id)
        preview = await self.preview_cgpa(db, semester_id, rows)
        if preview["problems"]:
            raise _error(status.HTTP_400_BAD_REQUEST, f"Fix the {len(preview['problems'])} problem row(s) in the file first")
        new_values = {c["register_number"]: c["new"] for c in preview["changed"]}
        if new_values:
            result = await db.execute(
                select(StudentSemesterAssignment)
                .where(
                    StudentSemesterAssignment.semester_id == semester_id,
                    StudentSemesterAssignment.register_number.in_(new_values),
                )
                .with_for_update()
            )
            for assignment in result.scalars().all():
                assignment.cgpa = new_values[assignment.register_number]
            audit_service.log(db, actor, "semester.cgpa_update", "semester", semester.semester_id, {
                "changes": {c["register_number"]: [c["old"], c["new"]] for c in preview["changed"]},
            })
        await db.commit()
        return preview

    # ------------------------------------------------------------------ undo

    async def undo_last(self, db: AsyncSession, actor: Actor, semester_id: int, kind: str) -> dict:
        """Undo the most recent CGPA update or batch assignment for a semester.

        Only rows still holding the value that change wrote are restored, so later edits win.
        """
        action = {"cgpa": "semester.cgpa_update", "batches": "semester.batch_assign"}.get(kind)
        if action is None:
            raise _error(status.HTTP_400_BAD_REQUEST, "kind must be 'cgpa' or 'batches'")
        semester = await self.lock(db, semester_id)
        if kind == "batches":
            await self._guard_bulk_change(db, semester, "min_moves")
        else:
            self.require_not_archived(semester)

        undone_ids = select(AuditLog.details["undo_of"].as_integer()).where(
            AuditLog.action == f"{action}_undo", AuditLog.entity == "semester", AuditLog.entity_id == str(semester_id)
        )
        entry = (await db.execute(
            select(AuditLog)
            .where(
                AuditLog.action == action,
                AuditLog.entity == "semester",
                AuditLog.entity_id == str(semester_id),
                AuditLog.log_id.not_in(undone_ids),
            )
            .order_by(AuditLog.log_id.desc())
            .limit(1)
        )).scalar_one_or_none()
        if entry is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Nothing to undo")

        pairs: dict[str, list] = (entry.details or {}).get("changes" if kind == "cgpa" else "moves", {})
        result = await db.execute(
            select(StudentSemesterAssignment)
            .where(
                StudentSemesterAssignment.semester_id == semester_id,
                StudentSemesterAssignment.register_number.in_(list(pairs)),
            )
            .with_for_update()
        )
        restored, skipped = 0, 0
        for assignment in result.scalars().all():
            old, new = pairs[assignment.register_number]
            current = float(assignment.cgpa) if kind == "cgpa" else assignment.batch_id
            if current != new:
                skipped += 1
                continue
            if kind == "cgpa":
                assignment.cgpa = old
            else:
                assignment.batch_id = old
            restored += 1
        audit_service.log(db, actor, f"{action}_undo", "semester", semester_id, {
            "undo_of": entry.log_id, "restored": restored, "skipped": skipped,
        })
        await db.commit()
        return {"restored": restored, "skipped": skipped}


semester_service = SemesterService()
