from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.common.validation import PostgresText, UTCDateTime
from app.core.dependencies import SessionDependency, require_roles
from app.modules.analytics.cache import CacheClient, cached_report, report_snapshot
from app.modules.analytics.conversion import ConversionReport, conversion_report
from app.modules.analytics.funnel import FunnelReport, funnel_report
from app.modules.analytics.overview import OverviewReport, overview_report
from app.modules.analytics.referrals import ReferralReport, referral_report
from app.modules.analytics.repository import RevenueRepository
from app.modules.analytics.schemas import (
    Grouping,
    ReferrerRanking,
    ReferrerRankingPeriod,
    ReferrerRevenue,
    RevenueReport,
)
from app.modules.analytics.service import InvalidRevenueRangeError, RevenueService
from app.modules.users.models import User, UserRole

router = APIRouter(prefix="/analytics", tags=["analytics"], dependencies=[Depends(report_snapshot)])
Admin = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
Staff = Annotated[User, Depends(require_roles(UserRole.MANAGER, UserRole.ADMIN))]


@router.get("/revenue", response_model=RevenueReport)
@cached_report(RevenueReport)
async def revenue(
    user: Staff,
    cache: CacheClient,
    session: SessionDependency,
    response: Response,
    date_from: UTCDateTime | None = None,
    date_to: UTCDateTime | None = None,
    currency: Annotated[PostgresText | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    group_by: Grouping | None = None,
    campaign: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    source: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    referral_code: Annotated[PostgresText | None, Query(min_length=1, max_length=64)] = None,
) -> RevenueReport:
    try:
        report = await RevenueService(session).report(
            user,
            date_from,
            date_to,
            currency,
            group_by=group_by,
            campaign=campaign,
            source=source,
            referral_code=referral_code,
        )
    except InvalidRevenueRangeError as exc:
        raise HTTPException(
            status_code=422, detail="date_from must be earlier than date_to"
        ) from exc
    return report


@router.get("/referrals", response_model=ReferralReport)
@cached_report(ReferralReport)
async def referrals(
    user: Admin,
    cache: CacheClient,
    session: SessionDependency,
    response: Response,
    date_from: UTCDateTime | None = None,
    date_to: UTCDateTime | None = None,
    group_by: Grouping | None = None,
    campaign: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    source: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    referral_code: Annotated[PostgresText | None, Query(min_length=1, max_length=64)] = None,
) -> ReferralReport:
    if date_from is not None and date_to is not None and date_from >= date_to:
        raise HTTPException(status_code=422, detail="date_from must be earlier than date_to")
    return await referral_report(
        session, date_from, date_to, group_by, campaign, source, referral_code
    )


@router.get("/funnel", response_model=FunnelReport)
@cached_report(FunnelReport)
async def funnel(
    user: Staff,
    cache: CacheClient,
    session: SessionDependency,
    response: Response,
    date_from: UTCDateTime | None = None,
    date_to: UTCDateTime | None = None,
    group_by: Grouping | None = None,
    source: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    campaign: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    referral_code: Annotated[PostgresText | None, Query(min_length=1, max_length=64)] = None,
) -> FunnelReport:
    if date_from is not None and date_to is not None and date_from >= date_to:
        raise HTTPException(status_code=422, detail="date_from must be earlier than date_to")
    return await funnel_report(
        session, user, date_from, date_to, group_by, source, campaign, referral_code
    )


@router.get("/overview", response_model=OverviewReport)
@cached_report(OverviewReport)
async def overview(
    user: Admin,
    cache: CacheClient,
    session: SessionDependency,
    response: Response,
    date_from: UTCDateTime | None = None,
    date_to: UTCDateTime | None = None,
    group_by: Grouping | None = None,
    campaign: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    source: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    referral_code: Annotated[PostgresText | None, Query(min_length=1, max_length=64)] = None,
) -> OverviewReport:
    if date_from is not None and date_to is not None and date_from >= date_to:
        raise HTTPException(status_code=422, detail="date_from must be earlier than date_to")
    return await overview_report(
        session, user, date_from, date_to, group_by, campaign, source, referral_code
    )


@router.get("/top-referrers", response_model=ReferrerRanking)
@cached_report(ReferrerRanking)
async def top_referrers(
    user: Admin,
    cache: CacheClient,
    session: SessionDependency,
    response: Response,
    date_from: UTCDateTime | None = None,
    date_to: UTCDateTime | None = None,
    group_by: Grouping | None = None,
    currency: Annotated[PostgresText | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    campaign: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    source: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    referral_code: Annotated[PostgresText | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> ReferrerRanking:
    start = date_from.astimezone(UTC) if date_from else None
    end = date_to.astimezone(UTC) if date_to else None
    if start and end and start >= end:
        raise HTTPException(status_code=422, detail="date_from must be earlier than date_to")
    rows = await RevenueRepository(session).top_referrers(
        user, start, end, currency, group_by, campaign, source, referral_code, limit
    )
    items = []
    periods = {}
    for row in rows:
        item = ReferrerRevenue.model_validate(row)
        period = row.get("period_start")
        if period is None:
            items.append(item)
        else:
            periods.setdefault(period.astimezone(UTC), []).append(item)
    return ReferrerRanking(
        date_from=start,
        date_to=end,
        group_by=group_by,
        limit=limit,
        items=items,
        periods=[
            ReferrerRankingPeriod(period_start=period, items=entries)
            for period, entries in periods.items()
        ],
    )


@router.get("/conversion", response_model=ConversionReport)
@cached_report(ConversionReport)
async def conversion(
    user: Admin,
    cache: CacheClient,
    session: SessionDependency,
    response: Response,
    date_from: UTCDateTime,
    date_to: UTCDateTime,
    previous_from: UTCDateTime | None = None,
    previous_to: UTCDateTime | None = None,
    group_by: Grouping | None = None,
    campaign: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    source: Annotated[PostgresText | None, Query(min_length=1, max_length=128)] = None,
    referral_code: Annotated[PostgresText | None, Query(min_length=1, max_length=64)] = None,
) -> ConversionReport:
    start, end = date_from.astimezone(UTC), date_to.astimezone(UTC)
    if start >= end:
        raise HTTPException(422, "date_from must be earlier than date_to")
    if (previous_from is None) != (previous_to is None):
        raise HTTPException(422, "Provide both previous period boundaries")
    if previous_from is None:
        try:
            previous_from, previous_to = start - (end - start), start
        except OverflowError as exc:
            raise HTTPException(422, "Previous period outside supported dates") from exc
    if not previous_from < previous_to <= start:
        raise HTTPException(422, "Previous period must end before or at date_from")
    return await conversion_report(
        session,
        user,
        start,
        end,
        previous_from,
        previous_to,
        group_by,
        campaign,
        source,
        referral_code,
    )
