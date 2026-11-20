# Метрики сервиса

## Метрики HTTP и PostgreSQL

`GET /metrics` отдаёт текстовый формат Prometheus. В Docker Compose порт API привязан к `127.0.0.1:8000`. Если API опубликован через прокси, оставьте путь `/metrics` доступным только в сети мониторинга.

```bash
curl --fail http://127.0.0.1:8000/health/ready
curl --fail http://127.0.0.1:8000/metrics
```

| Метрика | Тип | Метки |
| --- | --- | --- |
| `referral_http_requests_total` | Счётчик завершённых HTTP-запросов | `method`, `route`, `status` |
| `referral_http_errors_total` | Счётчик ответов 4xx и 5xx | `method`, `route`, `status` |
| `referral_http_request_duration_seconds` | Гистограмма времени обработки HTTP-запроса | `method`, `route` |
| `referral_db_query_duration_seconds` | Гистограмма выполнения SQL через драйвер PostgreSQL | `outcome` со значениями `success` или `error` |

Метка `route` содержит зарегистрированный шаблон, например `/api/v1/events/{event_id}`. Для запроса без выбранного маршрута используется `unmatched`, для нестандартного HTTP-метода — `OTHER`. Параметры URL, тела запросов, заголовки, SQL и параметры SQL в метки не попадают. Пакетный ответ `207` учитывается по общему HTTP-статусу ответа.

Метрики HTTP появляются после первого запроса. Сбор `/metrics` исключён из HTTP-счётчиков и гистограммы. Метрики PostgreSQL появляются после выполнения первого SQL-запроса. Их время измеряется от начала выполнения через драйвер до его ответа или ошибки и включает задержку обмена с PostgreSQL. Ожидание свободного соединения измеряется внутри HTTP-времени. Ошибка подключения без SQL-операции попадает в HTTP-метрики, если API ответил ошибкой. По умолчанию Compose запускает один процесс Uvicorn, а `/metrics` показывает данные обслуживающего процесса.

Примеры запросов PromQL для p95 HTTP по маршрутам и средней длительности успешного SQL-запроса.

```promql
histogram_quantile(0.95,
  sum by (le, route) (rate(referral_http_request_duration_seconds_bucket[5m]))
)
```

```promql
sum(rate(referral_db_query_duration_seconds_sum{outcome="success"}[5m]))
/
sum(rate(referral_db_query_duration_seconds_count{outcome="success"}[5m]))
```

Подробнее об агрегировании гистограмм. [Документация клиента Prometheus](https://prometheus.github.io/client_python/instrumenting/histogram/)

## Метрики Celery и событий

В Docker Compose воркер отдаёт `/metrics` по внутреннему адресу `celery-worker:9101`. Проверить его можно внутри контейнера.

```bash
docker compose exec celery-worker python -c 'from urllib.request import urlopen; print(urlopen("http://127.0.0.1:9101/metrics").read().decode())'
```

Для запуска вне Compose используйте новый каталог на каждый запуск воркера. Укажите его до импорта Python-клиента Prometheus. Адрес прослушивания по умолчанию — `127.0.0.1`. Переменная `CELERY_METRICS_HOST` задаёт другой адрес.

```bash
export PROMETHEUS_MULTIPROC_DIR="$(mktemp -d)"
export CELERY_METRICS_PORT=9101
uv run celery -A app.workers.celery_app:celery_app worker --concurrency=2 --loglevel=INFO
```

Внутри одного контейнера экспортер объединяет счётчики и гистограммы дочерних процессов. После остановки локального воркера его временный каталог можно удалить. Для нескольких контейнеров каждый экземпляр воркера должен иметь свой каталог метрик и отдельный target в Prometheus. [Документация multiprocess-режима Prometheus](https://prometheus.github.io/client_python/multiprocess/)

| Метрика | Где искать | Что измеряет |
| --- | --- | --- |
| `referral_celery_task_duration_seconds` | Воркер | Длительность исполнения каждой попытки по имени задачи и исходу `success`, `failure`, `retry` или `other` |
| `referral_celery_task_failures_total` | Воркер | Количество окончательных сбоев по имени задачи |
| `referral_events_processed_total` | Воркер | Записи outbox, зафиксированные с исходом `processed` или `rejected`, по типу события |
| `referral_duplicate_events_total{stage="ingestion"}` | API | Идентичные повторы на входе интеграционного API по идентификатору и содержимому события |
| `referral_duplicate_events_total{stage="financial_operation"}` | Воркер | Совпавшие финансовые операции, пришедшие с другим идентификатором доставки |
| `referral_db_query_duration_seconds` | API и воркер | Длительность SQL по результату выполнения |

Время Celery отсчитывается от начала исполнения попытки. Повторная попытка получает исход `retry`. Ошибка отдельного события отображается в метрике outbox, даже если задача разбора очереди успешно завершилась. Счётчики outbox обновляются после фиксации транзакции. Пока запись ждёт повторной попытки, она остаётся в очереди. В метках используются только имена зарегистрированных задач, допустимые типы событий и фиксированные исходы.

Количество окончательных ошибок Celery за пять минут по задачам.

```promql
sum by (task) (increase(referral_celery_task_failures_total[5m]))
```

Скорость обработки событий по типам и исходам.

```promql
sum by (event_type, outcome) (rate(referral_events_processed_total[5m]))
```

## Prometheus и Grafana в Docker Compose

Для нового стенда сначала запустите PostgreSQL и Redis и примените миграции. Затем включите профиль `monitoring`.

```bash
docker compose up -d postgres redis
docker compose run --rm --build api alembic upgrade head
docker compose --profile monitoring up -d --build
```

Prometheus доступен на `http://127.0.0.1:9090`, Grafana — на `http://127.0.0.1:3000`. При первом запуске Grafana создаёт администратора из `GRAFANA_ADMIN_USER` и `GRAFANA_ADMIN_PASSWORD`. Значения из `.env.example` предназначены для локального стенда. Задайте свой пароль в `.env` перед первым запуском. Для ранее созданного администратора пароль меняется средствами Grafana.

Prometheus опрашивает API и воркеры каждые 15 секунд. DNS-обнаружение регистрирует каждый экземпляр сервиса, адрес которого возвращает Docker DNS. Оба задания должны показывать `UP` в разделе **Status → Target health**.

Панель **Referral Analytics Service** и источник **Prometheus** загружаются при запуске Grafana. Панель показывает состояние целей, трафик и ошибки HTTP, p95 для HTTP, SQL и Celery, окончательные сбои задач, результаты outbox и повторные доставки. Ряды появляются после выполнения соответствующих операций и следующего сбора метрик. Графики на основе `rate()` и `increase()` требуют нескольких измерений.

Данные хранятся в томах Compose `prometheus_data` и `grafana_data`. Профиль останавливается отдельно от API и воркеров.

```bash
docker compose --profile monitoring stop grafana prometheus
```

Настройки сбора и загрузки конфигурации. [Prometheus](https://prometheus.io/docs/prometheus/latest/configuration/configuration/), [Grafana](https://grafana.com/docs/grafana/latest/administration/provisioning/).

## Структурированные логи

API, Celery worker и Celery beat в Compose пишут по одному JSON-объекту на строку. В `.env` уровень задаёт `LOG_LEVEL`, значение по умолчанию — `INFO`.

```bash
docker compose logs --no-log-prefix --no-color api celery-worker celery-beat
```

Общие поля — `timestamp` в UTC, `level`, `service`, `logger` и `message`. Запись HTTP содержит `method`, шаблон `route`, `status` и `duration_ms`. Запись Celery о завершении попытки содержит `task`, `task_id`, номер `attempt`, `outcome` и `duration_ms`. Ошибка фоновой операции содержит тип исходного исключения. Структурированные записи о запросах и задачах используют только эти поля. Параметры URL, тела запросов, токены, аргументы задач и текст SQL в них не передаются.

Для локального запуска API с тем же форматом используйте `uv run python -m app.server --port 8000`. Celery настраивает формат через сигнал `setup_logging`. Флаг `--quiet` перед `-A` отключает стартовый баннер. [Сигналы Celery](https://docs.celeryq.dev/en/stable/userguide/signals.html#setup-logging)
