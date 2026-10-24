from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


# ---------- Faculty ----------

class FacultyBase(BaseModel):
    employee_id: str = Field(..., min_length=1, max_length=20)
    faculty_name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    max_projects: int | None = Field(default=None, ge=0, le=100)
    active: bool = True


class FacultyCreate(FacultyBase):
    pass


class FacultyUpdate(BaseModel):
    faculty_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: EmailStr | None = None
    max_projects: int | None = Field(default=None, ge=0, le=100)
    clear_max_projects: bool = False  # set true to fall back to the default cap
    active: bool | None = None


class FacultyResponse(FacultyBase):
    faculty_id: int
    pfp_url: str | None = None
    created_at: datetime


class FacultyCapacity(BaseModel):
    """What a student sees when picking a guide."""

    faculty_id: int
    employee_id: str
    faculty_name: str
    email: str
    cap: int
    approved_count: int
    slots_left: int


# ---------- Settings ----------

class SettingsResponse(BaseModel):
    max_team_size: int
    default_guide_cap: int
    enabled_subtypes: list[str]
    cdc_enabled: bool
    max_proof_size_mb: int
    swap_cgpa_tolerance: float


class SettingsUpdate(BaseModel):
    max_team_size: int | None = None
    default_guide_cap: int | None = None
    enabled_subtypes: list[str] | None = None
    cdc_enabled: bool | None = None
    max_proof_size_mb: int | None = None
    swap_cgpa_tolerance: float | None = None


class RegistrationOpenUpdate(BaseModel):
    registration_open: bool


# ---------- Applications ----------

class InhouseCreate(BaseModel):
    semester_id: int
    project_subtype: str
    title: str = Field(..., min_length=3, max_length=255)
    abstract: str = Field(..., min_length=20, max_length=5000)
    faculty_id: int
    teammates: list[str] = Field(default_factory=list, max_length=9)  # register numbers


class CdcCreate(BaseModel):
    semester_id: int
    document_id: int


class InhouseUpdate(BaseModel):
    project_subtype: str | None = None
    title: str | None = Field(default=None, min_length=3, max_length=255)
    abstract: str | None = Field(default=None, min_length=20, max_length=5000)
    faculty_id: int | None = None


class InviteCreate(BaseModel):
    register_number: str = Field(..., min_length=1, max_length=20)


class CdcResubmit(BaseModel):
    document_id: int


class Remarks(BaseModel):
    remarks: str | None = Field(default=None, max_length=2000)


class RequiredRemarks(BaseModel):
    remarks: str = Field(..., min_length=3, max_length=2000)


class MemberResponse(BaseModel):
    member_id: int
    register_number: str
    student_name: str
    email: str
    batch_name: str | None
    role: str
    status: str
    responded_at: datetime | None


class GuideSummary(BaseModel):
    faculty_id: int
    employee_id: str
    faculty_name: str
    email: str


class DocumentSummary(BaseModel):
    document_id: int
    filename: str
    content_type: str
    size_bytes: int
    created_at: datetime


class ApplicationResponse(BaseModel):
    application_id: int
    semester_id: int
    semester_name: str
    leader_register_number: str
    project_type: str
    project_subtype: str | None
    title: str | None
    abstract: str | None
    status: str
    guide: GuideSummary | None
    proof_document: DocumentSummary | None
    guide_remarks: str | None
    hod_remarks: str | None
    members: list[MemberResponse]
    submitted_at: datetime | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


class InviteResponse(BaseModel):
    member_id: int
    status: str
    application: ApplicationResponse


class StudentLookupResponse(BaseModel):
    register_number: str
    student_name: str
    batch_name: str
    available: bool  # false if already in a team / CDC for this semester


class RegistrationSemester(BaseModel):
    semester_id: int
    semester_name: str
    registration_open: bool
    batch_name: str
    application: ApplicationResponse | None
    pending_invites: int


class GuideDashboard(BaseModel):
    semester_id: int
    semester_name: str
    cap: int
    approved_count: int
    pending_count: int


# ---------- Change requests ----------

class ChangeRequestCreate(BaseModel):
    request_type: str
    reason: str = Field(..., min_length=10, max_length=3000)
    add: list[str] = Field(default_factory=list, max_length=9)        # TEAM_CHANGE
    remove: list[str] = Field(default_factory=list, max_length=9)     # TEAM_CHANGE
    to_faculty_id: int | None = None                                   # GUIDE_CHANGE
    title: str | None = Field(default=None, min_length=3, max_length=255)       # TITLE_ABSTRACT
    abstract: str | None = Field(default=None, min_length=20, max_length=5000)  # TITLE_ABSTRACT
    document_id: int | None = None                                     # CDC_CONVERSION


class ChangeRequestResponse(BaseModel):
    request_id: int
    application_id: int
    application_title: str | None
    requested_by: str
    requested_by_name: str
    request_type: str
    payload: dict
    reason: str
    document: DocumentSummary | None
    status: str
    hod_remarks: str | None
    decided_at: datetime | None
    created_at: datetime


class AuditLogResponse(BaseModel):
    log_id: int
    actor_type: str
    actor_id: str
    action: str
    entity: str
    entity_id: str
    details: dict | None
    created_at: datetime
