# Архитектура Referral Analytics Service

FastAPI принимает запросы пользователей и интеграций. PostgreSQL хранит состояния сущностей, финансовые операции и очередь событий. Celery beat планирует фоновые задачи, а воркеры выполняют их через Redis. Redis также хранит результаты задач и кэш отчётов. API читает бизнес-данные из PostgreSQL с учётом области доступа пользователя.

```mermaid
flowchart LR
    Client["Клиенты и CRM"] --> API["FastAPI"]
    Integration["Интеграции заказов"] --> API
    API --> PG[("PostgreSQL")]
    API --> Cache[("Redis · кэш отчётов")]
    Beat["Celery beat"] --> Broker[("Redis · брокер и результаты")]
    Broker <--> Worker["Celery worker"]
    Worker --> PG
    Prometheus["Prometheus"] -->|scrape| API
    Prometheus -->|scrape| Worker
    Grafana["Grafana"] --> Prometheus
```

Запись события и outbox происходит в одной транзакции. Воркеры блокируют доступные записи через `SKIP LOCKED`, а обработчик финансовых событий сериализует изменения по заказу. Запись финансовой операции, перерасчёт вознаграждения и отметка об обработке фиксируются вместе. При ошибке временная задержка освобождает очередь для остальных событий. [Диаграмма обработки событий и расписание задач](workers.md)

## Основные связи данных

Диаграмма показывает бизнес-сущности и связи внешних ключей. Несколько ролей пользователя, например владелец кода и приглашённый, хранятся как ссылки на одну таблицу `users`. Технические таблицы кэша, снимков и ключей интеграций описаны ниже.

```mermaid
erDiagram
    USERS ||--o{ AUTH_SESSIONS : opens
    USERS ||--o{ REFERRAL_CODES : owns
    REFERRAL_CODES ||--o{ REFERRAL_CLICKS : receives
    REFERRAL_CODES ||--o{ REFERRAL_ATTRIBUTIONS : credits
    REFERRAL_CLICKS ||--o{ REFERRAL_ATTRIBUTIONS : supports
    USERS ||--o| REFERRAL_ATTRIBUTIONS : is_referred
    USERS ||--o{ LEADS : requests
    LEADS ||--o{ LEAD_STATUS_HISTORY : records
    REFERRAL_ATTRIBUTIONS o|--o{ LEADS : attributes
    USERS ||--o{ ORDERS : places
    REFERRAL_ATTRIBUTIONS o|--o{ ORDERS : attributes
    ORDERS ||--o{ ORDER_FINANCIAL_EVENTS : records
    ORDERS ||--o| REFERRAL_REWARDS : earns
    REFERRAL_PROGRAMS ||--o{ REFERRAL_REWARDS : defines
    REFERRAL_REWARDS ||--o{ REFERRAL_REWARD_HISTORY : tracks
    ORDER_FINANCIAL_EVENTS o|--o{ REFERRAL_REWARD_HISTORY : triggers
    EVENTS ||--|| EVENT_OUTBOX : queues
```

- `integration_credentials` хранит хеши ключей. `events` и `orders` связываются с источником по `source_system`, который определяется сервером по ключу интеграции.
- `analytics_cache_revisions` хранит ревизии данных. Запись в таблицы отчётов обновляет ревизии в той же транзакции. При чтении отчёта ревизия и права пользователя участвуют в ключе Redis.
- `analytics_snapshot_requests` хранит изменившиеся даты. `analytics_daily_snapshots` хранит готовые агрегаты, а `analytics_snapshot_failures` — даты, по которым пересчёт исчерпал попытки.

## Атрибуция регистрации

```mermaid
sequenceDiagram
    participant Visitor as Посетитель
    participant API as FastAPI
    participant DB as PostgreSQL
    Visitor->>API: POST /api/v1/referrals/track с visitor_id и request_id
    API->>DB: Проверить код и записать переход
    API-->>Visitor: 201 или 200 при идентичном повторе
    Visitor->>API: POST /api/v1/auth/register с тем же visitor_id
    API->>DB: Начать транзакцию и записать пользователя
    API->>DB: Синхронизировать операции по пользователю и посетителю
    API->>DB: Найти последний переход за 30 дней и заблокировать код
    alt Найден допустимый переход и есть свободный лимит
        API->>DB: Записать единственную атрибуцию
    else Подходящего перехода нет
        Note over API,DB: Пользователь регистрируется без атрибуции
    end
    API->>DB: Зафиксировать транзакцию
    API-->>Visitor: 201 Created
```

Код проверяется на активность, срок действия, самоприглашение и число сохранённых атрибуций. Если кандидат недопустим, обработчик ищет более ранний переход. UUID посетителя остаётся в домене отслеживания и служит только для связи переходов с регистрацией. [Правила атрибуции и реферальные коды](referrals.md)

Атрибуция заказа фиксируется при его создании. При первой оплате сервис выбирает действующую программу по времени операции и валюте. Последующие возвраты и корректировки пересчитывают сумму по сохранённым правилам. [Заказы](orders.md), [Вознаграждения](rewards.md), [Аналитика](analytics.md).
