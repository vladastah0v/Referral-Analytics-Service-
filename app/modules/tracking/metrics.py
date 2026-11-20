from prometheus_client import Counter

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
