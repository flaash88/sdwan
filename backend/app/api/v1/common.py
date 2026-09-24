from __future__ import annotations

import uuid
from typing import Any, TypeVar

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


async def get_or_404(db: AsyncSession, model: type[T], obj_id: uuid.UUID | str, what: str | None = None) -> T:
    obj = await db.get(model, obj_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what or model.__name__} nicht gefunden")
    return obj


def apply_update(obj: Any, data: dict[str, Any]) -> dict[str, Any]:
    changed = {}
    for k, v in data.items():
        if getattr(obj, k) != v:
            setattr(obj, k, v)
            changed[k] = v if not isinstance(v, uuid.UUID) else str(v)
    return changed
