from celery import Celery

from app.core.config import get_settings

celery_app = Celery("referral_analytics", broker=get_settings().celery_broker_url)
celery_app.conf.update(timezone="UTC", enable_utc=True, broker_connection_retry_on_startup=True)
