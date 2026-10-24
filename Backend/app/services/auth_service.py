from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import jwt

from app.core.config import get_settings
from app.models.student import Student

settings = get_settings()


class AuthService:
    @staticmethod
    def _parse_domains(raw_domains: str) -> list[str]:
        domains: list[str] = []
        seen: set[str] = set()

        for domain in raw_domains.split(","):
            # FIX: Strip spaces and strip the '@' symbol if it exists in the .env file
            normalized = domain.strip().lower().lstrip("@")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            domains.append(normalized)

        return domains

    def get_allowed_domains(self) -> list[str]:
        return self._parse_domains(settings.STUDENT_ALLOWED_DOMAINS)

    def get_faculty_domains(self) -> list[str]:
        return self._parse_domains(settings.FACULTY_ALLOWED_DOMAINS)

    def role_for_email(self, email: str) -> str | None:
        """'student' / 'guide' based on the email domain, or None if the domain isn't allowed."""
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if domain in self.get_allowed_domains():
            return "student"
        if domain in self.get_faculty_domains():
            return "guide"
        return None

    def ensure_allowed_domain(self, email: str) -> None:
        allowed_domains = {
            domain.lower() for domain in self.get_allowed_domains() + self.get_faculty_domains()
        }
        try:
            domain = email.split("@", 1)[1].lower()
        except IndexError:
            domain = ""

        if not allowed_domains or domain not in allowed_domains:
            domain_hint = ", ".join(sorted(allowed_domains)) or "configured domains"
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Only {domain_hint} accounts are allowed",
            )

    def ensure_google_email_verified(self, google_user: dict) -> None:
        # FIX: Check both common Google keys. Default to True to allow G-Suite 
        # domain accounts that omit this explicit flag.
        is_verified = google_user.get("email_verified", google_user.get("verified_email", True))
        
        # Check against both boolean False and string "false"
        if is_verified is False or str(is_verified).lower() == "false":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only verified Google accounts are allowed",
            )

    async def update_student_pfp(
        self,
        db: AsyncSession,
        email: str,
        picture_url: str | None,
    ) -> None:
        if not picture_url:
            return

        result = await db.execute(select(Student).where(Student.email == email))
        student = result.scalar_one_or_none()
        if student and student.pfp_url != picture_url:
            student.pfp_url = picture_url
            await db.commit()

    def create_jwt_token(
        self,
        email: str,
        name: str,
        user_type: str = "student",
        pfp_url: str | None = None,
        user_id: int | None = None,
    ) -> str:
        """Create a JWT token for authentication (student or admin)."""
        now = datetime.now(timezone.utc)
        payload = {
            "sub": email,
            "email": email,
            "name": name,
            "user_type": user_type,
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + timedelta(minutes=settings.JWT_EXPIRATION_MINUTES),
        }
        if user_id:
            payload["user_id"] = user_id
        if pfp_url:
            payload["pfp"] = pfp_url
        
        return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm="HS256")

    def decode_jwt_token(self, token: str) -> dict:
        """Decode and validate JWT token."""
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET_KEY,
                algorithms=["HS256"],
            )
            return payload
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )
auth_service = AuthService()
