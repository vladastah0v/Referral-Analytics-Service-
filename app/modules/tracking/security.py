import hashlib
import hmac
import secrets
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.modules.tracking.models import IntegrationCredential

service_key_header = APIKeyHeader(name="X-Integration-Key", auto_error=False)


async def create_credential(
    session: AsyncSession, source_system: str
) -> tuple[IntegrationCredential, str]:
    source_system = source_system.strip()
    if (
        not source_system
        or len(source_system) > 128
        or any(ord(c) < 32 or ord(c) == 127 for c in source_system)
    ):
        raise ValueError("Invalid source")
    credential_id, secret = uuid4(), secrets.token_urlsafe(32)
    credential = IntegrationCredential(
        id=credential_id,
        source_system=source_system,
        secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
        is_active=True,
    )
    session.add(credential)
    await session.commit()
    return credential, f"{credential_id}.{secret}"


async def authenticate_integration(
    session: Annotated[AsyncSession, Depends(get_session)],
    key: Annotated[str | None, Depends(service_key_header)],
) -> IntegrationCredential:
    invalid = HTTPException(status_code=401, detail="Invalid integration credentials")
    if key is None or len(key) > 256 or key.count(".") != 1:
        raise invalid
    credential_id, secret = key.split(".", 1)
    try:
        credential_uuid = UUID(credential_id)
        raw = secret.encode("ascii")
    except (ValueError, UnicodeEncodeError) as exc:
        raise invalid from exc
    credential = await session.get(IntegrationCredential, credential_uuid)
    digest = hashlib.sha256(raw).hexdigest()
    expected = credential.secret_hash if credential else "0" * 64
    if not hmac.compare_digest(digest, expected) or credential is None or not credential.is_active:
        raise invalid
    return credential


IntegrationPrincipal = Annotated[IntegrationCredential, Depends(authenticate_integration)]


async def disable_credential(session: AsyncSession, credential_id: UUID) -> bool:
    credential = await session.scalar(
        select(IntegrationCredential)
        .where(IntegrationCredential.id == credential_id)
        .with_for_update()
    )
    if credential is None:
        return False
    credential.is_active = False
    await session.commit()
    return True
