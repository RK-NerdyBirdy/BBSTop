from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SemesterCreate(BaseModel):
    semester_name: str = Field(..., max_length=100)
    swap_allowed: bool = Field(default=False)
    cohort_year: int | None = Field(default=None, ge=2000, le=2100)


class SemesterResponse(BaseModel):
    semester_id: int
    semester_name: str
    swap_allowed: bool
    registration_open: bool = False
    cohort_year: int | None = None
    archived: bool = False
    assignment_locked: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BatchCreate(BaseModel):
    batch_name: str = Field(..., max_length=50)
    semester_id: int


class BatchResponse(BaseModel):
    batch_id: int
    batch_name: str
    semester_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
