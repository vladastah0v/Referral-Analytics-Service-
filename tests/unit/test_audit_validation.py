from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.main import app
from app.modules.leads.schemas import LeadCreate
from app.modules.referrals.schemas import ReferralCodeCreate


def test_out_of_range_timezone_adjustment_is_a_validation_error() -> None:
    latest = datetime.max.replace(tzinfo=timezone(-timedelta(hours=1)))
    with pytest.raises(ValidationError, match="supported UTC range"):
        ReferralCodeCreate(expires_at=latest)


def test_incompatible_text_fails_before_a_database_write() -> None:
    with pytest.raises(ValidationError, match="Text must not contain NUL"):
        LeadCreate(source="web\x00form")


def test_openapi_describes_event_discriminator_for_single_and_batch() -> None:
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["EventCreate"]["discriminator"]["propertyName"] == "event_type"
    batch = schemas["BatchRequest"]["properties"]["events"]["items"]
    assert batch["discriminator"]["propertyName"] == "event_type"
    assert "order_adjusted" in batch["discriminator"]["mapping"]
