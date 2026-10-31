import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash, verify_password
from app.core.database import get_db
from app.crud.student_bulk import bulk_create_students_with_assignments
from app.crud.batch import create_semester, list_semesters, get_semester, create_batch, list_batches
from app.crud.swap_request import (
    execute_batch_swap,
    get_assignment_by_id,
    get_swap_request_for_update,
    list_accepted_swap_requests,
)
from app.dependencies.admin_auth import get_current_admin
from app.models.admin import Admin
from app.schemas.student import StudentCreate, StudentResponse
from app.schemas.batch import SemesterCreate, SemesterResponse, BatchCreate, BatchResponse
from app.schemas.admin import AdminChangePassword, AdminChangePasswordResponse
from app.schemas.swap_request import AssignmentResponse, SemesterSwapAllowedResponse, SwapRequestResponseWithDetails
from app.services.csv_service import csv_service, CSVParsingError
from app.services.swap_service import swap_candidate_service

router = APIRouter(dependencies=[Depends(get_current_admin)])


async def _build_assignment_response(
    db: AsyncSession,
    assignment: "StudentSemesterAssignment",
) -> AssignmentResponse:
    from app.crud.student import get_student
    from app.crud.batch import get_batch

    student = await get_student(db, assignment.register_number)
    batch = await get_batch(db, assignment.batch_id)
    return AssignmentResponse(
        assignment_id=assignment.assignment_id,
        register_number=assignment.register_number,
        student_name=student.student_name if student else "Unknown",
        semester_id=assignment.semester_id,
        batch_id=assignment.batch_id,
        batch_name=batch.batch_name if batch else "Unknown",
        cgpa=float(assignment.cgpa),
        active=assignment.active,
        created_at=assignment.created_at,
    )


@router.post("/students/upload-csv", status_code=status.HTTP_202_ACCEPTED)
async def upload_students_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be a CSV file",
        )

    try:
        content = await file.read()
        records = await csv_service.parse_csv(content)
    except CSVParsingError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to parse CSV: {str(e)}",
        )

    if not records:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV contains no valid records",
        )

    created, failed = await bulk_create_students_with_assignments(db, records)

    response = {
        "total": len(records),
        "created": len(created),
        "failed": len(failed),
        "created_records": [
            {
                "register_number": item["student"].register_number,
                "student_name": item["student"].student_name,
                "email": item["student"].email,
                "semester_name": item["semester"].semester_name,
                "batch_name": item["batch"].batch_name,
                "cgpa": float(item["assignment"].cgpa),
            }
            for item in created
        ],
    }

    if failed:
        response["errors"] = [
            {
                "register_number": f["payload"].register_number,
                "email": f["payload"].email,
                "semester_name": f["payload"].semester_name,
                "batch_name": f["payload"].batch_name,
                "error": f["error"],
            }
            for f in failed
        ]

    return response


@router.get("/students", response_model=list[StudentResponse])
async def list_students(
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> list[StudentResponse]:
    from app.crud.student import list_students as crud_list_students
    students = await crud_list_students(db)
    return students


@router.post("/students", response_model=StudentResponse, status_code=status.HTTP_201_CREATED)
async def create_student_endpoint(
    payload: StudentCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> StudentResponse:
    from app.crud.student import create_student as crud_create_student
    student = await crud_create_student(db, payload)
    return student


@router.get("/students/{register_number}", response_model=StudentResponse)
async def get_student(
    register_number: str,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> StudentResponse:
    from app.crud.student import get_student as crud_get_student
    student = await crud_get_student(db, register_number)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )
    return student


@router.put("/students/{register_number}", response_model=StudentResponse)
async def update_student(
    register_number: str,
    payload: StudentCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> StudentResponse:
    from app.crud.student import get_student as crud_get_student
    student = await crud_get_student(db, register_number)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    student.student_name = payload.student_name
    student.email = payload.email
    student.phone_number = payload.phone_number
    student.pfp_url = payload.pfp_url
    await db.commit()
    await db.refresh(student)
    return student


@router.delete("/students/{register_number}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_student(
    register_number: str,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> None:
    from app.crud.student import get_student as crud_get_student
    student = await crud_get_student(db, register_number)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    await db.delete(student)
    await db.commit()


@router.get("/analytics/cgpa-above")
async def cgpa_above_threshold(
    min_cgpa: float = 9.0,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    from app.services.analytics_service import analytics_service
    return await analytics_service.count_students_above_cgpa(db, min_cgpa)


@router.get("/analytics/batch-cgpa-distribution")
async def batch_cgpa_distribution(
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    from app.services.analytics_service import analytics_service
    return await analytics_service.batch_cgpa_distribution(db)


@router.get("/analytics/batch-stats-by-semester/{semester_id}")
async def batch_stats_by_semester(
    semester_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    from app.services.analytics_service import analytics_service
    return await analytics_service.batch_stats_by_semester(db, semester_id)


@router.get("/semesters/{semester_id}/swap-allowed", response_model=SemesterSwapAllowedResponse)
async def get_swap_allowed(
    semester_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> SemesterSwapAllowedResponse:
    semester = await get_semester(db, semester_id)
    if not semester:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Semester not found",
        )

    return SemesterSwapAllowedResponse(
        semester_id=semester.semester_id,
        semester_name=semester.semester_name,
        swap_allowed=semester.swap_allowed,
    )


@router.get("/swap-requests/accepted", response_model=list[SwapRequestResponseWithDetails])
async def list_accepted_swap_requests_endpoint(
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> list[SwapRequestResponseWithDetails]:
    accepted_requests = await list_accepted_swap_requests(db)
    response: list[SwapRequestResponseWithDetails] = []
    for request in accepted_requests:
        requester_assignment = await get_assignment_by_id(db, request.requester_assignment_id)
        target_assignment = await get_assignment_by_id(db, request.target_assignment_id)
        requester_response = (
            await _build_assignment_response(db, requester_assignment)
            if requester_assignment
            else None
        )
        target_response = (
            await _build_assignment_response(db, target_assignment)
            if target_assignment
            else None
        )
        response.append(
            SwapRequestResponseWithDetails(
                request_id=request.request_id,
                requester_assignment_id=request.requester_assignment_id,
                target_assignment_id=request.target_assignment_id,
                status=request.status,
                approved_by=request.approved_by,
                approved_at=request.approved_at,
                created_at=request.created_at,
                requester_assignment=requester_response,
                target_assignment=target_response,
            )
        )
    return response


@router.patch("/change-password", response_model=AdminChangePasswordResponse)
async def change_admin_password(
    payload: AdminChangePassword,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> AdminChangePasswordResponse:
    if not verify_password(payload.current_password, current_admin.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid current password",
        )

    if verify_password(payload.new_password, current_admin.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from current password",
        )

    current_admin.password_hash = get_password_hash(payload.new_password)
    current_admin.password_initial_change = True
    await db.commit()

    return AdminChangePasswordResponse(message="Password updated successfully")


@router.patch("/swap-requests/{request_id}/approve", response_model=SwapRequestResponseWithDetails)
async def approve_swap_request(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> SwapRequestResponseWithDetails:
    try:
        swap_request = await get_swap_request_for_update(db, request_id)
        if not swap_request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Swap request not found",
            )

        if swap_request.status != "ACCEPTED":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Swap request must be accepted by the student before approval",
            )

        # CGPAs or batches may have changed since the students agreed
        requester = await get_assignment_by_id(db, swap_request.requester_assignment_id)
        target = await get_assignment_by_id(db, swap_request.target_assignment_id)
        semester = await get_semester(db, requester.semester_id) if requester else None
        if semester is None or semester.archived:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This semester is archived")
        # The limit may have been lowered since the students agreed
        if requester and target:
            reached = await swap_candidate_service.limit_reached(db, requester, target)
            if reached:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{', '.join(a.register_number for a in reached)} has used all their swaps for this semester. Reject the request, or raise the swap limit in Rules.",
                )
        if not requester or not target or not await swap_candidate_service.is_eligible(db, requester, target):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="These students are no longer eligible to swap (CGPA or batch changed). Reject the request.",
            )

        # Execute the batch swap
        swap_result = await execute_batch_swap(
            db,
            swap_request.requester_assignment_id,
            swap_request.target_assignment_id,
            commit=False,
            lock=True,
        )

        if not swap_result:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to execute batch swap",
            )

        requester_assignment, target_assignment = swap_result

        swap_request.status = "APPROVED"
        swap_request.approved_by = current_admin.admin_id
        swap_request.approved_at = datetime.now(timezone.utc)
        await db.flush()
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    requester_response = await _build_assignment_response(db, requester_assignment)
    target_response = await _build_assignment_response(db, target_assignment)
    return SwapRequestResponseWithDetails(
        request_id=swap_request.request_id,
        requester_assignment_id=swap_request.requester_assignment_id,
        target_assignment_id=swap_request.target_assignment_id,
        status=swap_request.status,
        approved_by=swap_request.approved_by,
        approved_at=swap_request.approved_at,
        created_at=swap_request.created_at,
        requester_assignment=requester_response,
        target_assignment=target_response,
    )


@router.patch("/swap-requests/{request_id}/reject", response_model=SwapRequestResponseWithDetails)
async def reject_swap_request(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> SwapRequestResponseWithDetails:
    try:
        swap_request = await get_swap_request_for_update(db, request_id)
        if not swap_request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Swap request not found",
            )

        if swap_request.status in {"APPROVED", "CANCELLED"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Swap request cannot be rejected in its current state",
            )

        swap_request.status = "REJECTED"
        swap_request.approved_by = current_admin.admin_id
        swap_request.approved_at = datetime.now(timezone.utc)
        await db.flush()
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    requester_assignment = await get_assignment_by_id(
        db, swap_request.requester_assignment_id
    )
    target_assignment = await get_assignment_by_id(
        db, swap_request.target_assignment_id
    )

    requester_response = await _build_assignment_response(db, requester_assignment)
    target_response = await _build_assignment_response(db, target_assignment)
    return SwapRequestResponseWithDetails(
        request_id=swap_request.request_id,
        requester_assignment_id=swap_request.requester_assignment_id,
        target_assignment_id=swap_request.target_assignment_id,
        status=swap_request.status,
        approved_by=swap_request.approved_by,
        approved_at=swap_request.approved_at,
        created_at=swap_request.created_at,
        requester_assignment=requester_response,
        target_assignment=target_response,
    )


@router.get("/batches/{batch_id}/export")
async def export_batch(
    batch_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    from app.services.batch_service import batch_service
    result = await batch_service.get_batch_export(db, batch_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Batch not found",
        )
    return result


@router.get("/batches/{batch_id}/export-csv")
async def export_batch_csv(
    batch_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> StreamingResponse:
    from app.services.batch_service import batch_service
    result = await batch_service.get_batch_export(db, batch_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Batch not found",
        )

    output = io.StringIO()
    fieldnames = [
        "register_number",
        "student_name",
        "email",
        "phone_number",
        "cgpa",
        "batch_name",
        "semester_name",
        "active",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for student in result.get("students", []):
        writer.writerow({
            "register_number": student.get("register_number"),
            "student_name": student.get("student_name"),
            "email": student.get("email"),
            "phone_number": student.get("phone_number"),
            "cgpa": student.get("cgpa"),
            "batch_name": student.get("batch_name"),
            "semester_name": student.get("semester_name"),
            "active": student.get("active"),
        })

    output.seek(0)
    filename = f"batch_{batch_id}.csv"
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return StreamingResponse(output, media_type="text/csv", headers=headers)


@router.patch("/assignments/{assignment_id}")
async def update_assignment_batch(
    assignment_id: int,
    new_batch_id: int = Body(..., embed=True),
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    """Move one student to another batch of the same semester; cancels their open swap requests."""
    from app.services.semester_service import semester_service
    from app.services.project_service import admin_actor

    return await semester_service.move_student(db, admin_actor(current_admin.admin_id), assignment_id, new_batch_id)


@router.patch("/semesters/{semester_id}/swap-allowed")
async def set_swap_allowed(
    semester_id: int,
    swap_allowed: bool = Body(..., embed=True),
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    from app.services.batch_service import batch_service
    result = await batch_service.update_swap_allowed(db, semester_id, swap_allowed)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Semester not found",
        )
    return result


# ============================================
# SEMESTER AND BATCH MANAGEMENT ENDPOINTS
# ============================================


@router.post("/semesters", response_model=SemesterResponse, status_code=status.HTTP_201_CREATED)
async def create_semester_endpoint(
    payload: SemesterCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> SemesterResponse:
    """Create a new semester. A year group can only have one current (non-archived) semester."""
    from app.services.semester_service import semester_service

    await semester_service.require_cohort_free(db, payload.cohort_year)
    semester = await create_semester(
        db,
        semester_name=payload.semester_name,
        swap_allowed=payload.swap_allowed,
        cohort_year=payload.cohort_year,
    )
    return semester


@router.get("/semesters", response_model=list[SemesterResponse])
async def list_semesters_endpoint(
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> list[SemesterResponse]:
    """Get all semesters."""
    semesters = await list_semesters(db)
    return semesters


@router.get("/semesters/{semester_id}", response_model=SemesterResponse)
async def get_semester_endpoint(
    semester_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> SemesterResponse:
    """Get semester by ID."""
    semester = await get_semester(db, semester_id)
    if not semester:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Semester not found",
        )
    return semester


@router.post("/batches", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
async def create_batch_endpoint(
    payload: BatchCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> BatchResponse:
    """Create a new batch under a semester."""
    # Verify semester exists
    semester = await get_semester(db, payload.semester_id)
    if not semester:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Semester {payload.semester_id} not found",
        )
    
    batch = await create_batch(
        db,
        batch_name=payload.batch_name,
        semester_id=payload.semester_id,
    )
    return batch


@router.get("/batches", response_model=list[BatchResponse])
async def list_batches_endpoint(
    semester_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> list[BatchResponse]:
    """Get all batches, optionally filtered by semester."""
    batches = await list_batches(db, semester_id)
    return batches
