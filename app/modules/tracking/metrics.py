from prometheus_client import Counter, Gauge

EVENTS_PROCESSED = Counter(
    "referral_events_processed_total",
    "Outbox events committed to a terminal state.",
    ("event_type", "outcome"),
)
DUPLICATE_EVENTS = Counter(
    "referral_duplicate_events_total",
    "Idempotent duplicate deliveries detected.",
    ("stage",),
)
EVENT_INGESTION_FAILURES = Counter(
    "referral_event_ingestion_failures_total",
    "Failed event submissions, including individual items in batch responses.",
    ("mode", "status"),
)

OUTBOX_PENDING = Gauge(
    "referral_outbox_pending",
    "Pending outbox rows, including scheduled retries.",
    multiprocess_mode="livemostrecent",
)
OUTBOX_OLDEST = Gauge(
    "referral_outbox_oldest_pending_seconds",
    "Age in seconds of the oldest pending outbox row.",
    multiprocess_mode="livemostrecent",
)
