from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SemesterCreate(BaseModel):
    semester_name: str = Field(..., max_length=100)
    swap_allowed: bool = Field(default=False)


class SemesterResponse(BaseModel):
    semester_id: int
    semester_name: str
    swap_allowed: bool
    registration_open: bool = False
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
