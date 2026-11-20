import logging
from time import perf_counter

from prometheus_client import Counter, Histogram
from sqlalchemy import event
from sqlalchemy.engine import Engine, ExceptionContext, ExecutionContext
from starlette.types import ASGIApp, Message, Receive, Scope, Send

http_logger = logging.getLogger("app.http")

HTTP_REQUESTS = Counter(
    "referral_http_requests_total",
    "Completed HTTP requests.",
    ("method", "route", "status"),
)
HTTP_ERRORS = Counter(
    "referral_http_errors_total",
    "HTTP requests resulting in a 4xx or 5xx response.",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "referral_http_request_duration_seconds",
    "Time spent handling HTTP requests.",
    ("method", "route"),
)
DB_DURATION = Histogram(
    "referral_db_query_duration_seconds",
    "Time spent executing SQL through the PostgreSQL driver.",
    ("outcome",),
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)


class HTTPMetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = perf_counter()
        status = None

        async def record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record_status)
        except Exception:
            # ServerErrorMiddleware sends the 500 outside this middleware.
            if status is None:
                status = 500
            raise
        finally:
            # FastAPI stores include_router prefixes on the effective route context.
            effective_route = scope.get("fastapi", {}).get("effective_route_context")
            route = getattr(effective_route or scope.get("route"), "path_format", None)
            route = route or "unmatched"
            if status is not None and route != "/metrics":
                method = scope["method"]
                if method not in HTTP_METHODS:
                    method = "OTHER"
                elapsed = perf_counter() - started
                HTTP_DURATION.labels(method, route).observe(elapsed)
                http_logger.info(
                    "HTTP request completed",
                    extra={
                        "method": method,
                        "route": route,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 3),
                    },
                )
                HTTP_REQUESTS.labels(method, route, str(status)).inc()
                if 400 <= status < 600:
                    HTTP_ERRORS.labels(method, route, str(status)).inc()


def record_query(context: ExecutionContext | None, outcome: str) -> None:
    # Connection and pool errors may have no statement execution context.
    started = getattr(context, "_referral_query_started", None)
    if started is not None:
        del context._referral_query_started
        DB_DURATION.labels(outcome).observe(perf_counter() - started)


def instrument_database(engine: Engine) -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        context._referral_query_started = perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        record_query(context, "success")

    @event.listens_for(engine, "handle_error")
    def handle_error(context: ExceptionContext):
        record_query(context.execution_context, "error")
