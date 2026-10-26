from fastapi import APIRouter

from app.api import admin, admin_project, admin_semester, auth, guide, student, student_dashboard, student_project

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_project.router, prefix="/admin", tags=["admin-projects"])
api_router.include_router(admin_semester.router, prefix="/admin", tags=["admin-semesters"])
api_router.include_router(student.router, prefix="/student", tags=["student"])
api_router.include_router(student_dashboard.router, prefix="/student", tags=["student"])
api_router.include_router(student_project.router, prefix="/student/projects", tags=["student-projects"])
api_router.include_router(guide.router, prefix="/guide", tags=["guide"])
