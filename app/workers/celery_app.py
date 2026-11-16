from celery import Celery

from app.core.config import get_settings

celery_app = Celery(
    "referral_analytics",
    broker=get_settings().celery_broker_url,
    include=["app.workers.tasks.outbox"],
)
celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    worker_prefetch_multiplier=1,
    beat_schedule={"drain-event-outbox": {"task": "tracking.drain_outbox", "schedule": 5.0}},
)
