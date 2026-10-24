from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse  # <-- Added for React redirect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.oauth import get_oauth
from app.core.security import verify_password, get_password_hash
from app.crud.admin import get_admin_by_email
from app.crud.faculty import get_faculty_by_email
from app.crud.student import get_student_by_email
from app.models.admin import Admin
from app.schemas.admin import AdminLogin, AdminResponse, AdminCreate, AdminLoginResponse
from pydantic import BaseModel, EmailStr
from app.services.auth_service import auth_service

router = APIRouter()


def _resolve_google_redirect_uri(request: Request) -> str:
    settings = get_settings()
    configured = (settings.GOOGLE_REDIRECT_URI or "").strip()

    # Prefer an explicit non-localhost redirect URI when deployed.
    if configured and "localhost" not in configured and "127.0.0.1" not in configured:
        return configured

    return str(request.url_for("google_callback"))


@router.get("/google/login")
async def google_login(request: Request):
    oauth = get_oauth()
    redirect_uri = _resolve_google_redirect_uri(request)
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/google/callback", name="google_callback")
async def google_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    oauth = get_oauth()
    token = await oauth.google.authorize_access_token(request)
    user = token.get("userinfo")
    if user is None:
        user = await oauth.google.parse_id_token(request, token)

    email = (user or {}).get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only permitted student accounts are allowed",
        )

    # 1. Verify Domain and Google Status
    auth_service.ensure_google_email_verified(user or {})
    auth_service.ensure_allowed_domain(email)
    settings = get_settings()
    pfp_url = (user or {}).get("picture")

    # 1.2 Faculty-domain accounts sign in as guides, if HOD has added them to the faculty list
    if auth_service.role_for_email(email) == "guide":
        faculty = await get_faculty_by_email(db, email)
        if not faculty or not faculty.active:
            return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/callback?error=not_authorized")
        if pfp_url and faculty.pfp_url != pfp_url:
            faculty.pfp_url = pfp_url
            await db.commit()
        jwt_token = auth_service.create_jwt_token(
            faculty.email,
            faculty.faculty_name,
            user_type="guide",
            pfp_url=pfp_url,
            user_id=faculty.faculty_id,
        )
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/callback?token={jwt_token}&role=guide")

    # 1.5 Only allow students that already exist in the student table
    student = await get_student_by_email(db, email)
    if not student:
        error_redirect_url = f"{settings.FRONTEND_URL}/auth/callback?error=not_authorized"
        return RedirectResponse(url=error_redirect_url)

    # 2. Update Profile Picture in DB
    await auth_service.update_student_pfp(db, email, pfp_url)

    # 3. Generate JWT token for student
    student_name = student.student_name or (user or {}).get("name", email)
    jwt_token = auth_service.create_jwt_token(
        email,
        student_name,
        user_type="student",
        pfp_url=pfp_url,
    )

    # 4. Redirect to React Frontend
    frontend_redirect_url = f"{settings.FRONTEND_URL}/auth/callback?token={jwt_token}&role=student"
    return RedirectResponse(url=frontend_redirect_url)


@router.post("/admin/login", response_model=AdminLoginResponse)
async def admin_login(
    payload: AdminLogin,
    db: AsyncSession = Depends(get_db),
) -> AdminLoginResponse:
    admin = await get_admin_by_email(db, payload.admin_email)
    if not admin or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Generate JWT token for admin
    jwt_token = auth_service.create_jwt_token(
        email=admin.admin_email,
        name=admin.admin_name,
        user_type="admin",
        user_id=admin.admin_id,
    )
    
    return AdminLoginResponse(
        admin_name=admin.admin_name,
        admin_email=admin.admin_email,
        admin_id=admin.admin_id,
        access_token=jwt_token,
        password_initial=admin.password_initial_change,
    )


@router.post("/admin/logout")
async def admin_logout() -> dict:
    """Logout endpoint (stateless JWT - just discard token on client side)."""
    return {"ok": True, "message": "Please discard the token on the client side"}


class DevLoginRequest(BaseModel):
    email: EmailStr


@router.post("/dev-login")
async def dev_login(
    payload: DevLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Local development only: issue a student/guide token for a seeded account without Google.

    Returns 404 unless DEV_AUTH_ENABLED=true, which must never be set in production.
    """
    if not get_settings().DEV_AUTH_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    email = payload.email.lower()
    role = auth_service.role_for_email(email)
    if role == "guide":
        faculty = await get_faculty_by_email(db, email)
        if faculty:
            token = auth_service.create_jwt_token(
                faculty.email, faculty.faculty_name, user_type="guide", user_id=faculty.faculty_id
            )
            return {"token": token, "role": "guide"}
    elif role == "student":
        student = await get_student_by_email(db, email)
        if student:
            token = auth_service.create_jwt_token(student.email, student.student_name, user_type="student")
            return {"token": token, "role": "student"}

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No seeded student or guide with that email")
