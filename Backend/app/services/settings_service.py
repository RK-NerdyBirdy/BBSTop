from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.app_setting import AppSetting
from app.models.project import ProjectSubtype

# Rules the HOD can edit. Missing rows fall back to these defaults.
DEFAULTS: dict[str, object] = {
    # Team size per in-house category: {"CAPSTONE": {"min": 1, "max": 3}, ...}
    "team_size": {subtype: {"min": 1, "max": 3} for subtype in ProjectSubtype.ALL},
    # Moving into these categories after approval needs a proof upload (e.g. an internship offer)
    "category_change_proof": [ProjectSubtype.NON_CDC],
    "default_guide_cap": 4,
    "enabled_subtypes": list(ProjectSubtype.ALL),
    "cdc_enabled": True,
    "max_proof_size_mb": 5,
    # Batch swaps: partners must be within this CGPA range of each other
    "swap_cgpa_tolerance": get_settings().CGPA_TOL,
}


def _validate(key: str, value: object) -> object:
    if key == "team_size":
        if not isinstance(value, dict) or set(value) != set(ProjectSubtype.ALL):
            raise ValueError(f"team_size needs a min and max for each of {', '.join(ProjectSubtype.ALL)}")
        cleaned = {}
        for subtype in ProjectSubtype.ALL:
            limits = value[subtype]
            low, high = (limits.get("min"), limits.get("max")) if isinstance(limits, dict) else (None, None)
            if not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 10 for v in (low, high)):
                raise ValueError(f"{subtype}: team size limits must be whole numbers between 1 and 10")
            if low > high:
                raise ValueError(f"{subtype}: the minimum team size can't be more than the maximum")
            cleaned[subtype] = {"min": low, "max": high}
        value = cleaned
    elif key == "category_change_proof":
        if not isinstance(value, list) or not all(v in ProjectSubtype.ALL for v in value):
            raise ValueError(f"category_change_proof must be a list drawn from {', '.join(ProjectSubtype.ALL)}")
        value = [v for v in ProjectSubtype.ALL if v in value]
    elif key == "default_guide_cap":
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
            raise ValueError("default_guide_cap must be a whole number between 0 and 100")
    elif key == "enabled_subtypes":
        if not isinstance(value, list) or not all(v in ProjectSubtype.ALL for v in value):
            raise ValueError(f"enabled_subtypes must be a list drawn from {', '.join(ProjectSubtype.ALL)}")
        value = [v for v in ProjectSubtype.ALL if v in value]
    elif key == "cdc_enabled":
        if not isinstance(value, bool):
            raise ValueError("cdc_enabled must be true or false")
    elif key == "max_proof_size_mb":
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10:
            raise ValueError("max_proof_size_mb must be a whole number between 1 and 10")
    elif key == "swap_cgpa_tolerance":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10:
            raise ValueError("swap_cgpa_tolerance must be a number between 0 and 10")
        value = round(float(value), 2)
    else:
        raise ValueError(f"Unknown setting: {key}")
    return value


class SettingsService:
    @staticmethod
    async def get_all(db: AsyncSession) -> dict[str, object]:
        result = await db.execute(select(AppSetting))
        stored = {row.key: row.value for row in result.scalars().all()}
        return {key: stored.get(key, default) for key, default in DEFAULTS.items()}

    async def get(self, db: AsyncSession, key: str) -> object:
        row = await db.get(AppSetting, key)
        return row.value if row is not None else DEFAULTS[key]

    async def team_limits(self, db: AsyncSession, subtype: str | None) -> tuple[int, int]:
        """(min, max) team size for an in-house category."""
        limits = (await self.get(db, "team_size")).get(subtype or "", {"min": 1, "max": 3})
        return limits["min"], limits["max"]

    async def update(self, db: AsyncSession, changes: dict[str, object]) -> dict[str, dict]:
        """Validate and stage changes. Returns {key: {"from": old, "to": new}} for the audit log. Caller commits."""
        try:
            validated = {key: _validate(key, value) for key, value in changes.items()}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        diff: dict[str, dict] = {}
        for key, value in validated.items():
            row = await db.get(AppSetting, key)
            old = row.value if row is not None else DEFAULTS[key]
            if old == value:
                continue
            if row is None:
                db.add(AppSetting(key=key, value=value))
            else:
                row.value = value
            diff[key] = {"from": old, "to": value}
        return diff


settings_service = SettingsService()
