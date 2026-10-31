from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text, update, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.crud.student import get_student_by_email, get_student
from app.models.student import Student
from app.models.swap_request import SwapRequest
from app.crud.swap_request import (
    create_swap_request as create_swap_request_crud,
    get_assignment_by_id,
    get_assignment_by_id_for_update,
    get_all_student_assignments,
    get_assignments_by_register_number_for_update,
    get_active_swap_request_between,
    get_swap_request_for_update,
    has_accepted_request_for_assignments,
    list_swap_requests_for_assignments,
)
from app.crud.batch import get_batch, get_semester
from app.crud.batch import get_batch
from app.models.student_semester_assignment import StudentSemesterAssignment
from app.dependencies.student_auth import get_current_student
from app.schemas.swap_request import (
    AssignmentResponse,
    SemesterSwapAllowedResponse,
    StudentBatchInfoResponse,
    SwapCandidateResponse,
    SwapRequestCreate,
    SwapRequestResponseWithDetails,
)
from app.schemas.student import (
    PhoneNumberUpdate,
    PhoneStatusResponse,
    StudentDirectoryResponse,
    StudentSwapLockResponse,
    StudentSwapLockUpdate,
)
from app.services.settings_service import settings_service
from app.services.swap_service import swap_candidate_service
from app.models.semester import Semester
from app.models.batch import Batch


router = APIRouter()


async def _build_assignment_response(
    db: AsyncSession,
    assignment: StudentSemesterAssignment,
) -> AssignmentResponse:
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


@router.get("/me", response_model=dict)
async def get_me(
    current_student: dict = Depends(get_current_student),
) -> dict:
    return {
        "email": current_student.get("email"),
        "name": current_student.get("name"),
        "pfp": current_student.get("pfp"),
    }


@router.get("/me/swap-lock", response_model=StudentSwapLockResponse)
async def get_swap_lock_status(
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> StudentSwapLockResponse:
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    return StudentSwapLockResponse(swap_lock=student.swap_lock)


@router.patch("/me/swap-lock", response_model=StudentSwapLockResponse)
async def update_swap_lock_status(
    payload: StudentSwapLockUpdate,
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> StudentSwapLockResponse:
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    student.swap_lock = payload.swap_lock
    await db.commit()
    await db.refresh(student)

    return StudentSwapLockResponse(swap_lock=student.swap_lock)


@router.get("/me/batch", response_model=list[StudentBatchInfoResponse])
async def get_my_batch(
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> list[StudentBatchInfoResponse]:
    """Get all batch assignments for the current student across all semesters."""
    email = current_student.get("email")
    
    # Find student by email
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )
    
    # Get all assignments
    assignments = await get_all_student_assignments(db, student.register_number)
    response: list[StudentBatchInfoResponse] = []
    for assignment in assignments:
        batch = await get_batch(db, assignment.batch_id)
        response.append(
            StudentBatchInfoResponse(
                assignment_id=assignment.assignment_id,
                register_number=assignment.register_number,
                student_name=student.student_name,
                semester_id=assignment.semester_id,
                batch_id=assignment.batch_id,
                batch_name=batch.batch_name if batch else "Unknown",
                cgpa=float(assignment.cgpa),
                active=assignment.active,
                created_at=assignment.created_at,
            )
        )
    return response


@router.get("/swap-allowed", response_model=list[SemesterSwapAllowedResponse])
async def get_my_swap_allowed_status(
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> list[SemesterSwapAllowedResponse]:
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    assignments = await get_all_student_assignments(db, student.register_number)
    semester_ids = sorted({assignment.semester_id for assignment in assignments})

    used = await swap_candidate_service.swaps_used(db, [a.assignment_id for a in assignments])
    used_by_semester = {a.semester_id: used.get(a.assignment_id, 0) for a in assignments}
    swap_limit = await settings_service.get(db, "swap_limit")

    result: list[SemesterSwapAllowedResponse] = []
    for semester_id in semester_ids:
        semester = await get_semester(db, semester_id)
        if semester:
            result.append(
                SemesterSwapAllowedResponse(
                    semester_id=semester.semester_id,
                    semester_name=semester.semester_name,
                    swap_allowed=semester.swap_allowed,
                    swaps_used=used_by_semester.get(semester_id, 0),
                    swap_limit=swap_limit,
                )
            )

    return result


@router.get("/swap-eligible", response_model=dict)
async def get_my_eligible_swaps(
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> dict:
    """Get all eligible swap candidates for the current student across all their semesters.
    
    Shows students grouped by semester that match CGPA tolerance (±CGPA_TOL).
    """
    email = current_student.get("email")
    
    # Find student by email
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )
    
    # Get all assignments for this student
    all_assignments = await get_all_student_assignments(db, student.register_number)
    
    if not all_assignments:
        return {
            "register_number": student.register_number,
            "student_name": student.student_name,
            "eligible_by_semester": {},
        }
    
    # For each semester, find eligible swap candidates
    eligible_by_semester = {}
    for assignment in all_assignments:
        candidates = await swap_candidate_service.get_swap_candidates(
            db,
            assignment.assignment_id,
        )
        
        if candidates:
            candidate_list = []
            for c in candidates:
                candidate_student = await get_student(db, c.register_number)
                batch = await get_batch(db, c.batch_id)
                if candidate_student and candidate_student.swap_lock:
                    continue

                candidate_list.append({
                    "assignment_id": c.assignment_id,
                    "register_number": c.register_number,
                    "student_name": candidate_student.student_name if candidate_student else "Unknown",
                    "email": candidate_student.email if candidate_student else "",
                    "phone_number": candidate_student.phone_number if candidate_student else None,
                    "batch_id": c.batch_id,
                    "batch_name": batch.batch_name if batch else "Unknown",
                    "cgpa": float(c.cgpa),
                })
            eligible_by_semester[assignment.semester_id] = candidate_list
    
    return {
        "register_number": student.register_number,
        "student_name": student.student_name,
        "eligible_by_semester": eligible_by_semester,
    }


@router.get("/swap-requests/incoming", response_model=list[SwapRequestResponseWithDetails])
async def list_incoming_swap_requests(
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> list[SwapRequestResponseWithDetails]:
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    assignments = await get_all_student_assignments(db, student.register_number)
    assignment_ids = [assignment.assignment_id for assignment in assignments]
    requests = await list_swap_requests_for_assignments(db, assignment_ids, incoming=True)
    response: list[SwapRequestResponseWithDetails] = []
    for request in requests:
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


@router.get("/swap-requests/outgoing", response_model=list[SwapRequestResponseWithDetails])
async def list_outgoing_swap_requests(
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> list[SwapRequestResponseWithDetails]:
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    assignments = await get_all_student_assignments(db, student.register_number)
    assignment_ids = [assignment.assignment_id for assignment in assignments]
    requests = await list_swap_requests_for_assignments(db, assignment_ids, incoming=False)
    response: list[SwapRequestResponseWithDetails] = []
    for request in requests:
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


@router.post("/swap-requests", response_model=SwapRequestResponseWithDetails, status_code=status.HTTP_201_CREATED)
async def create_swap_request_endpoint(
    payload: SwapRequestCreate,
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> SwapRequestResponseWithDetails:
    email = current_student.get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid student token",
        )

    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    # 1. Deterministic locking of assignments to serialize with the accept flow
    a_id = payload.requester_assignment_id
    b_id = payload.target_assignment_id
    first_id, second_id = (a_id, b_id) if a_id <= b_id else (b_id, a_id)

    # These locks prevent race conditions if an 'accept' is happening simultaneously
    await get_assignment_by_id_for_update(db, first_id)
    await get_assignment_by_id_for_update(db, second_id)

    # Fetch locked assignments
    requester = await get_assignment_by_id(db, a_id)
    target = await get_assignment_by_id(db, b_id)

    if not requester or not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or both assignments not found",
        )

    if requester.register_number != student.register_number:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requester assignment does not belong to current student",
        )

    if requester.semester_id != target.semester_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Both assignments must be in the same semester",
        )

    semester = await get_semester(db, requester.semester_id)
    if not semester or not semester.swap_allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Swaps are not allowed for this semester",
        )

    if requester.batch_id == target.batch_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Swap candidates must be in different batches",
        )

    reached = await swap_candidate_service.limit_reached(db, requester, target)
    if reached:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You've used all your swaps for this semester" if reached[0] is requester else "This student has used all their swaps for this semester",
        )

    eligible_candidates = await swap_candidate_service.get_swap_candidates(
        db,
        requester.assignment_id,
    )
    eligible_ids = {candidate.assignment_id for candidate in eligible_candidates}
    if target.assignment_id not in eligible_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target assignment is not eligible for swap",
        )

    # 2. Strict One-Active Policy Check across ALL requests for BOTH students
    active_check_stmt = select(SwapRequest.request_id).where(
        or_(
            SwapRequest.requester_assignment_id.in_([a_id, b_id]),
            SwapRequest.target_assignment_id.in_([a_id, b_id])
        ),
        SwapRequest.status.in_(["PENDING", "ACCEPTED"])
    )
    active_check_result = await db.execute(active_check_stmt)
    if active_check_result.first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            # Frontend-friendly error
            detail="Cannot send request: You or the target student already have an active swap request.",
        )

    try:
        swap_request = await create_swap_request_crud(db, payload)
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            # Frontend-friendly error
            detail="This swap request has already been created or processed.",
        ) from error

    requester_response = await _build_assignment_response(db, requester)
    target_response = await _build_assignment_response(db, target)
    return SwapRequestResponseWithDetails(
        request_id=swap_request.request_id,
        requester_assignment_id=swap_request.requester_assignment_id,
        target_assignment_id=swap_request.target_assignment_id,
        status=swap_request.status,
        created_at=swap_request.created_at,
        requester_assignment=requester_response,
        target_assignment=target_response,
    )




@router.patch("/swap-requests/{request_id}/accept", response_model=SwapRequestResponseWithDetails)
async def accept_swap_request(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> SwapRequestResponseWithDetails:
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    try:
        swap_request = await get_swap_request_for_update(db, request_id)
        if not swap_request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Swap request not found",
            )

        if swap_request.status != "PENDING":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Swap request is not pending",
            )
            
        # Lock both assignment rows in deterministic order by assignment_id
        a_id = swap_request.requester_assignment_id
        b_id = swap_request.target_assignment_id
        first_id, second_id = (a_id, b_id) if a_id <= b_id else (b_id, a_id)

        first_assignment = await get_assignment_by_id_for_update(db, first_id)
        second_assignment = await get_assignment_by_id_for_update(db, second_id)

        if swap_request.target_assignment_id == first_assignment.assignment_id:
            target_assignment = first_assignment
            requester_assignment = second_assignment
        else:
            target_assignment = second_assignment
            requester_assignment = first_assignment

        if not target_assignment or target_assignment.register_number != student.register_number:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not authorized to accept this request",
            )

        semester = await get_semester(db, target_assignment.semester_id)
        if not semester or not semester.swap_allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Swaps are not allowed for this semester",
            )

        # Either student may have used up their swaps since the request was sent
        reached = await swap_candidate_service.limit_reached(db, target_assignment, requester_assignment)
        if reached:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You've used all your swaps for this semester" if reached[0] is target_assignment else "The other student has used all their swaps for this semester",
            )

        # CGPAs or batches may have changed since the request was sent
        if not await swap_candidate_service.is_eligible(db, requester_assignment, target_assignment):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You two are no longer eligible to swap (CGPA or batch changed). Ask them to cancel the request.",
            )

        # Lock both students
        reg_nums = sorted({requester_assignment.register_number, target_assignment.register_number})
        if reg_nums:
            for rn in reg_nums:
                await db.execute(
                    select(Student).where(Student.register_number == rn).with_for_update()
                )

        assignment_ids = set()
        result = await db.execute(
            select(StudentSemesterAssignment.assignment_id).where(
                StudentSemesterAssignment.register_number.in_(reg_nums)
            )
        )
        assignment_ids.update([row[0] for row in result.all()])
        assignment_ids_list = list(assignment_ids)

        has_active = await has_accepted_request_for_assignments(
            db,
            assignment_ids_list,
            exclude_request_id=request_id,
        )
        if has_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                # Frontend-friendly error
                detail="Cannot accept: One of the students involved has already accepted a different swap request.",
            )

        swap_request.status = "ACCEPTED"
        await db.flush()

        # Safely fetch and lock all OTHER peripheral requests for BOTH students
        if assignment_ids_list:
            pending_requests_stmt = (
                select(SwapRequest)
                .where(
                    SwapRequest.request_id != request_id,
                    or_(
                        SwapRequest.requester_assignment_id.in_(assignment_ids_list),
                        SwapRequest.target_assignment_id.in_(assignment_ids_list)
                    ),
                    SwapRequest.status == "PENDING"
                )
                .with_for_update()
            )
            pending_requests_result = await db.execute(pending_requests_stmt)
            pending_requests = pending_requests_result.scalars().all()

            # Cancel outgoing, Reject incoming
            for req in pending_requests:
                if req.requester_assignment_id in assignment_ids_list:
                    req.status = "CANCELLED"
                else:
                    req.status = "REJECTED"

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
        created_at=swap_request.created_at,
        requester_assignment=requester_response,
        target_assignment=target_response,
    )


@router.patch("/swap-requests/{request_id}/reject", response_model=SwapRequestResponseWithDetails)
async def reject_swap_request(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> SwapRequestResponseWithDetails:
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    try:
        swap_request = await get_swap_request_for_update(db, request_id)
        if not swap_request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Swap request not found",
            )

        if swap_request.status != "PENDING":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Swap request is not pending",
            )

        target_assignment = await get_assignment_by_id_for_update(
            db, swap_request.target_assignment_id
        )
        if not target_assignment or target_assignment.register_number != student.register_number:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not authorized to reject this request",
            )

        requester_assignment = await get_assignment_by_id_for_update(
            db, swap_request.requester_assignment_id
        )
        if not requester_assignment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Requester assignment not found",
            )

        swap_request.status = "REJECTED"
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


@router.patch("/swap-requests/{request_id}/cancel", response_model=SwapRequestResponseWithDetails)
async def cancel_swap_request(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> SwapRequestResponseWithDetails:
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    try:
        swap_request = await get_swap_request_for_update(db, request_id)
        if not swap_request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Swap request not found",
            )

        if swap_request.status == "APPROVED":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Approved swap requests cannot be cancelled",
            )

        requester_assignment = await get_assignment_by_id_for_update(
            db, swap_request.requester_assignment_id
        )
        if not requester_assignment or requester_assignment.register_number != student.register_number:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not authorized to cancel this request",
            )

        target_assignment = await get_assignment_by_id_for_update(
            db, swap_request.target_assignment_id
        )
        if not target_assignment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target assignment not found",
            )

        swap_request.status = "CANCELLED"
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


@router.get("/me/phone-status", response_model=PhoneStatusResponse)
async def get_phone_status(
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> PhoneStatusResponse:
    """Check if the current student has a phone number registered."""
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )
        
    return PhoneStatusResponse(
        has_phone_number=bool(student.phone_number and student.phone_number.strip())
    )


@router.patch("/me/phone", response_model=dict)
async def update_phone_number(
    payload: PhoneNumberUpdate,
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> dict:
    """Update the current student's phone number."""
    email = current_student.get("email")
    student = await get_student_by_email(db, email)
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )
        
    student.phone_number = payload.phone_number
    await db.commit()
    
    return {"status": "success", "message": "Phone number updated successfully"}


@router.get("/directory", response_model=list[StudentDirectoryResponse])
async def get_student_directory(
    semester_id: int,
    db: AsyncSession = Depends(get_db),
    current_student: dict = Depends(get_current_student),
) -> list[StudentDirectoryResponse]:
    """View batch info, name, reg no, and pfp for all active students in a semester."""
    query = (
        select(
            Student.register_number,
            Student.student_name,
            Student.pfp_url,
            Semester.semester_name,
            Batch.batch_name
        )
        .select_from(Student)
        .join(StudentSemesterAssignment, Student.register_number == StudentSemesterAssignment.register_number)
        .join(Semester, StudentSemesterAssignment.semester_id == Semester.semester_id)
        .join(Batch, StudentSemesterAssignment.batch_id == Batch.batch_id)
        .where(
            StudentSemesterAssignment.active.is_(True),
            StudentSemesterAssignment.semester_id == semester_id,
            Semester.archived.is_(False),
        )
    )
    result = await db.execute(query)
    rows = result.all()
    
    return [
        StudentDirectoryResponse(
            register_number=row.register_number,
            student_name=row.student_name,
            pfp_url=row.pfp_url,
            semester_name=row.semester_name,
            batch_name=row.batch_name
        ) for row in rows
    ]