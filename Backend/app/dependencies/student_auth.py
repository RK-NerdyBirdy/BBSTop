from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.crud.student import get_student_by_email
from app.models.student import Student
from app.services.auth_service import auth_service

security = HTTPBearer()


async def get_current_student(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Extract and validate JWT token from Bearer auth."""
    token = credentials.credentials
    payload = auth_service.decode_jwt_token(token)
    
    # Ensure only student tokens can access student endpoints
    user_type = payload.get("user_type")
    if user_type != "student":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not a student token",
        )
    
    return payload


async def get_current_student_record(
    current_student: dict = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
) -> Student:
    """Like get_current_student, but returns the Student row (404 if the student was removed)."""
    student = await get_student_by_email(db, current_student.get("email", ""))
    if student is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )
    return student
