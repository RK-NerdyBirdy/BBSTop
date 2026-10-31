from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AssignmentResponse(BaseModel):
    assignment_id: int
    register_number: str
    student_name: str
    semester_id: int
    batch_id: int
    batch_name: str
    cgpa: float
    active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StudentBatchInfoResponse(BaseModel):
    assignment_id: int
    register_number: str
    student_name: str
    semester_id: int
    batch_id: int
    batch_name: str
    cgpa: float
    active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SemesterSwapAllowedResponse(BaseModel):
    semester_id: int
    semester_name: str
    swap_allowed: bool
    swaps_used: int = 0
    swap_limit: int = 1

    model_config = ConfigDict(from_attributes=True)


class SwapCandidateResponse(BaseModel):
    """Student details with their assignment info for swap eligibility."""
    assignment_id: int
    register_number: str
    student_name: str
    email: str
    phone_number: str | None
    semester_id: int
    batch_id: int
    batch_name: str
    cgpa: float
    active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SwapRequestBase(BaseModel):
    requester_assignment_id: int
    target_assignment_id: int


class SwapRequestCreate(SwapRequestBase):
    pass


class SwapRequestResponse(SwapRequestBase):
    request_id: int
    status: str
    approved_by: int | None = None
    approved_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SwapRequestResponseWithDetails(SwapRequestResponse):
    requester_assignment: AssignmentResponse | None = None
    target_assignment: AssignmentResponse | None = None
