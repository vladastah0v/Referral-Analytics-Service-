import json
import logging
from datetime import UTC, datetime

CONTEXT_FIELDS = (
    "method",
    "route",
    "status",
    "duration_ms",
    "task",
    "task_id",
    "attempt",
    "outcome",
    "error_type",
)


class JSONFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            payload["error_type"] = record.exc_info[0].__name__
        for key in CONTEXT_FIELDS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        return json.dumps(payload, ensure_ascii=False)


def logging_config(service: str, level: str | int, logfile: str | None = None) -> dict:
    output = (
        {"class": "logging.FileHandler", "filename": logfile}
        if logfile
        else {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        }
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": JSONFormatter, "service": service}},
        "handlers": {
            "json": output | {"formatter": "json"},
            "discard": {"class": "logging.NullHandler"},
        },
        "root": {"handlers": ["json"], "level": level},
        "loggers": {
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.access": {"handlers": ["discard"], "propagate": False},
            "celery.app.trace": {"handlers": ["discard"], "propagate": False},
        },
    }
