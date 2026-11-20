from logging.config import dictConfig

from celery import Celery, signals
from celery.schedules import crontab

from app.core.config import get_settings
from app.core.logging import logging_config
from app.workers.metrics import instrument_worker


@signals.setup_logging.connect
def configure_celery_logging(loglevel=None, logfile=None, **kwargs):
    dictConfig(logging_config("celery", loglevel or get_settings().log_level, logfile))


celery_app = Celery(
    "referral_analytics",
    broker=get_settings().celery_broker_url,
    backend=get_settings().celery_result_backend,
    include=["app.workers.tasks.outbox", "app.workers.tasks.maintenance"],
)
instrument_worker(celery_app)
celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=172800,
    worker_send_task_events=True,
    task_track_started=True,
    beat_schedule={
        "drain-event-outbox": {"task": "tracking.drain_outbox", "schedule": 5.0},
        "refresh-changed-days": {"task": "analytics.recalculate_pending", "schedule": 60.0},
        "snapshot-yesterday": {
            "task": "analytics.daily_snapshot",
            "schedule": crontab(hour=0, minute=5),
        },
        "purge-expired-sessions": {"task": "maintenance.cleanup_sessions", "schedule": 300.0},
    },
)
