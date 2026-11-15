from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    EmailStr,
    SecretStr,
    StringConstraints,
    field_validator,
)

from app.modules.users.models import UserRole


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: SecretStr
    full_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        if not 12 <= len(value.get_secret_value()) <= 128:
            raise ValueError("Password must contain between 12 and 128 characters")
        return value


class UserRegistration(UserCreate):
    visitor_id: UUID4 | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime
