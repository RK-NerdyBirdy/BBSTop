from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str
    CGPA_TOL: float
    STUDENT_ALLOWED_DOMAINS: str = "vitstudent.ac.in"
    FACULTY_ALLOWED_DOMAINS: str = "vit.ac.in"
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str | None = None
    SESSION_SECRET_KEY: str = "change-me"
    JWT_SECRET_KEY: str = "your-secret-key-change-this"
    JWT_EXPIRATION_MINUTES: int = 120
    FRONTEND_URL: str="http://localhost:3000"
    # Comma-separated list of allowed frontend origins
    CORS_ORIGINS: str = "http://localhost:3000,https://bbstop.vercel.app,https://batchxchange.vercel.app"
    # Local development only: enables /api/auth/dev-login. Never set this on Render.
    DEV_AUTH_ENABLED: bool = False
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
