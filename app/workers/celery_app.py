from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

celery_app = Celery(
    "referral_analytics",
    broker=get_settings().celery_broker_url,
    backend=get_settings().celery_result_backend,
    include=["app.workers.tasks.outbox", "app.workers.tasks.maintenance"],
)
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
