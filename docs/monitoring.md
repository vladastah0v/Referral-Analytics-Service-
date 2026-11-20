# Метрики HTTP и PostgreSQL

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
| `referral_db_query_duration_seconds` | Гистограмма выполнения SQL через движок API | `outcome` со значениями `success` или `error` |

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
