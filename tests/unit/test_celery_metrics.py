from prometheus_client import REGISTRY

from app.workers.celery_app import celery_app


def test_celery_attempts_distinguish_retry_from_failure():
    name = "tests.metrics.retry_and_fail"

    @celery_app.task(name=name, bind=True)
    def retry_and_fail(self):
        if self.request.retries == 0:
            raise self.retry(countdown=0)
        raise ValueError("synthetic failure")

    result = retry_and_fail.apply(throw=False)
    assert result.failed()
    for outcome in ("retry", "failure"):
        assert (
            REGISTRY.get_sample_value(
                "referral_celery_task_duration_seconds_count", {"task": name, "outcome": outcome}
            )
            == 1
        )
    assert REGISTRY.get_sample_value("referral_celery_task_failures_total", {"task": name}) == 1
