from fastapi import HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models.document import Document
from app.models.student import Student
from app.services.audit_service import Actor, audit_service
from app.services.settings_service import settings_service

ALLOWED_TYPES = {
    "application/pdf": b"%PDF",
    "image/png": b"\x89PNG",
    "image/jpeg": b"\xff\xd8\xff",
}


class DocumentService:
    async def upload(self, db: AsyncSession, student: Student, file: UploadFile) -> Document:
        max_mb = await settings_service.get(db, "max_proof_size_mb")
        content = await file.read(max_mb * 1024 * 1024 + 1)
        if len(content) > max_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File must be at most {max_mb} MB")
        if not content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File is empty")
        # Trust the file's bytes, not the browser-supplied content type
        content_type = next((t for t, magic in ALLOWED_TYPES.items() if content.startswith(magic)), None)
        if content_type is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF, PNG or JPG files are allowed")

        filename = (file.filename or "proof").replace("/", "_").replace("\\", "_")[:255]
        document = Document(
            owner_register_number=student.register_number,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            content=content,
        )
        db.add(document)
        await db.flush()
        audit_service.log(db, Actor("student", student.register_number), "document.upload", "document", document.document_id, {
            "filename": filename, "size_bytes": len(content),
        })
        await db.commit()
        return document

    @staticmethod
    async def download(db: AsyncSession, document_id: int) -> Response:
        result = await db.execute(
            select(Document).options(undefer(Document.content)).where(Document.document_id == document_id)
        )
        document = result.scalar_one_or_none()
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        safe_name = document.filename.replace('"', "")
        return Response(
            content=document.content,
            media_type=document.content_type,
            headers={
                "Content-Disposition": f'inline; filename="{safe_name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @staticmethod
    async def owner_of(db: AsyncSession, document_id: int) -> str | None:
        result = await db.execute(select(Document.owner_register_number).where(Document.document_id == document_id))
        return result.scalar_one_or_none()


document_service = DocumentService()
