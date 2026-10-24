from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.crud.faculty import get_faculty
from app.models.faculty import Faculty
from app.services.auth_service import auth_service

security = HTTPBearer()


async def get_current_guide(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> Faculty:
    payload = auth_service.decode_jwt_token(credentials.credentials)
    if payload.get("user_type") != "guide":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not a guide token",
        )

    user_id = payload.get("user_id")
    faculty = await get_faculty(db, user_id) if user_id else None
    if faculty is None or not faculty.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guide not found or inactive",
        )
    return faculty
