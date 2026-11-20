import os
from time import perf_counter

from celery import signals, states
from prometheus_client import CollectorRegistry, Counter, Histogram, multiprocess, start_http_server

TASK_DURATION = Histogram(
    "referral_celery_task_duration_seconds",
    "Execution time of each Celery task attempt.",
    ("task", "outcome"),
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
TASK_FAILURES = Counter(
    "referral_celery_task_failures_total",
    "Celery task failures, excluding attempts scheduled for retry.",
    ("task",),
)

OUTCOMES = {
    states.SUCCESS: "success",
    states.FAILURE: "failure",
    states.RETRY: "retry",
}


def instrument_worker(app):
    # Celery can reconstruct the application object in a spawned pool process.
    app_name = app.main

    @signals.task_prerun.connect(weak=False)
    def start_task(sender, **kwargs):
        if sender.app.main == app_name:
            sender.request.referral_metrics_started = perf_counter()

    @signals.task_postrun.connect(weak=False)
    def finish_task(sender, state=None, **kwargs):
        if sender.app.main == app_name:
            started = getattr(sender.request, "referral_metrics_started", None)
            if started is not None:
                TASK_DURATION.labels(sender.name, OUTCOMES.get(state, "other")).observe(
                    perf_counter() - started
                )

    @signals.task_failure.connect(weak=False)
    def fail_task(sender, **kwargs):
        if sender.app.main == app_name:
            TASK_FAILURES.labels(sender.name).inc()

    @signals.worker_ready.connect(weak=False)
    def serve_metrics(sender, **kwargs):
        port = os.getenv("CELERY_METRICS_PORT")
        if sender.app.main == app_name and port:
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
            start_http_server(
                int(port),
                addr=os.getenv("CELERY_METRICS_HOST", "127.0.0.1"),
                registry=registry,
            )
