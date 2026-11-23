from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime


def validate_postgres_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Text must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Text must be valid UTF-8") from exc
    return value


def normalize_utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Datetime is outside the supported UTC range") from exc


PostgresText = Annotated[str, AfterValidator(validate_postgres_text)]
UTCDateTime = Annotated[AwareDatetime, AfterValidator(normalize_utc)]
