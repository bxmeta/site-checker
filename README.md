# Site Monitor

Система мониторинга доступности сайтов с уведомлениями в Telegram.

## Возможности

- **HTTP-проверки**: проверка кода ответа
- **SSL-проверки**: валидность сертификата, срок действия, соответствие домену
- **IDN-домены**: поддержка кириллических доменов (например, `сайт.рф`)
- **Ключевые слова**: поиск заданных слов в HTML-контенте
- **Retry-логика**: повторные попытки при неудаче
- **Уведомления в Telegram**: при падении и восстановлении сайтов
- **Админы получают всё**: администраторы автоматически получают уведомления по всем сайтам
- **Повторные уведомления**: напоминания с интервалом 15→30→60→120→240 мин
- **Mute per-user**: заглушить напоминания для себя до восстановления сайта
- **История инцидентов**: хранение всех падений в SQLite
- **Статистика uptime**: команда /stats для просмотра статистики
- **Telegram-бот**: управление мониторингом через inline-кнопки
- **Управление админами**: добавление/удаление администраторов через бота
- **Webhook / Polling**: два режима работы бота
- **Планировщик**: автоматическая проверка по расписанию
- **Логирование**: запись всех проверок в файл

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

### 1. Создайте Telegram-бота

1. Напишите [@BotFather](https://t.me/BotFather) в Telegram
2. Отправьте команду `/newbot`
3. Следуйте инструкциям и получите токен бота

### 2. Настройте конфигурацию

Отредактируйте файл `config.yaml`:

```yaml
telegram:
  bot_token: "YOUR_BOT_TOKEN"  # Токен от BotFather
  admin_ids: [123456789]       # Ваш Telegram ID (можно узнать через /myid)

scheduler:
  interval_minutes: 3          # Интервал проверок в минутах

default:
  retry_count: 3               # Количество повторных попыток
  retry_interval_minutes: 5    # Интервал между попытками
  timeout_seconds: 10          # Таймаут запроса

sites:
  - id: "site1"
    name: "Example"
    url: "https://example.com"
    support_level: "premium"   # none / standard / premium
    check_ssl: true
    check_http_code: true
    expected_code: 200
    keywords: ["Example", "Domain"]
    notify_users: [123456789]  # Кому отправлять уведомления
```

## Запуск

### Простой запуск (polling)

```bash
python main.py
```

### Запуск в фоне

```bash
nohup python main.py > bot.log 2>&1 &
```

### Режим Webhook (рекомендуется для продакшена)

Webhook работает быстрее polling и надёжнее для продакшена.

#### 1. Настройте config.yaml

```yaml
telegram:
  bot_token: "YOUR_BOT_TOKEN"
  admin_ids: [123456789]
  use_webhook: true
  webhook_url: "https://your-domain.com/webhook"
  webhook_path: "/webhook"
  webhook_host: "127.0.0.1"
  webhook_port: 8000
```

#### 2. Настройте Nginx

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        return 301 https://$server_name$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location /webhook {
        proxy_pass http://127.0.0.1:8000/webhook;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

#### 3. Перезапустите Nginx

```bash
nginx -t && systemctl reload nginx
```

### Автозапуск (systemd)

Создайте файл `/etc/systemd/system/site-monitor.service`:

```ini
[Unit]
Description=Site Monitor Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/path/to/site-checker
ExecStart=/usr/bin/python3 /path/to/site-checker/main.py
Restart=always
RestartSec=10
TimeoutStartSec=10
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
```

Активируйте сервис:

```bash
# Перезагрузить systemd
systemctl daemon-reload

# Включить автозапуск
systemctl enable site-monitor

# Запустить сервис
systemctl start site-monitor

# Проверить статус
systemctl status site-monitor

# Посмотреть логи
journalctl -u site-monitor -f
```

## Команды Telegram-бота

| Команда | Описание | Доступ |
|---------|----------|--------|
| `/start` | Регистрация пользователя | Все |
| `/myid` | Показать Telegram ID | Все |
| `/my_sites` | Список сайтов (для админов - все сайты) | Все |
| `/muted` | Список заглушенных сайтов | Все |
| `/status_all` | Статус всех сайтов | Админы |
| `/check_now` | Мгновенная проверка | Админы |
| `/sites` | Управление сайтами (интерактивное меню) | Админы |
| `/add_site` | Добавить новый сайт | Админы |
| `/stats` | Статистика uptime сайтов | Админы |
| `/admins` | Список администраторов | Админы |
| `/add_admin ID` | Добавить администратора | Админы |
| `/remove_admin ID` | Удалить администратора | Админы |

## Управление сайтами через бота (для админов)

После команды `/sites` откроется интерактивное меню с inline-кнопками:

### Просмотр и навигация
- Список всех сайтов с пагинацией
- Статус каждого сайта (🟢 UP / 🔴 DOWN)
- Подробная информация о сайте

### Редактирование сайта
- Изменение названия и URL
- Выбор уровня поддержки (none/standard/premium)
- Настройка ожидаемого HTTP кода
- Включение/отключение проверки SSL
- Включение/отключение проверки HTTP кода
- Редактирование ключевых слов

### Управление подписчиками
- Просмотр списка подписчиков сайта
- Добавление нового подписчика по Telegram ID
- Удаление подписчика

### Проверка и удаление
- Мгновенная проверка конкретного сайта
- Удаление сайта (с подтверждением)

### Добавление нового сайта
Команда `/add_site` или кнопка "➕ Добавить сайт" запускает пошаговый мастер:
1. Ввод URL (протокол https:// добавляется автоматически)
2. Выбор уровня поддержки (🟡 Стандарт / 🟢 Премиум)
3. Ввод ключевых слов (через запятую или кнопка "Пропустить")
4. Ввод Telegram ID подписчиков (через запятую или кнопка "Пропустить")

- ID и название сайта генерируются автоматически из домена
- HTTP код 200 и SSL-проверка включены по умолчанию
- Администраторы получают уведомления по всем сайтам автоматически

## Структура проекта

```
/site-checker/
├── monitor/
│   ├── __init__.py
│   ├── config_loader.py     # Загрузка конфигурации
│   ├── database.py          # SQLite база данных
│   ├── checker.py           # HTTP-проверки
│   ├── ssl_checker.py       # SSL-проверки (срок, домен, доверие цепочки, издатель)
│   ├── diagnostics.py       # Пошаговая диагностика при падении
│   ├── messages.py          # Общие тексты уведомлений (Telegram + Bitrix24)
│   ├── multi_notifier.py    # Фан-аут уведомлений TG + Б24, тихий режим
│   ├── b24.py               # Канал Bitrix24: бот, команды, уведомления
│   ├── changelog.py         # Журнал изменений конфигурации (changes.log)
│   ├── keyword_checker.py   # Поиск ключевых слов
│   ├── retry_logic.py       # Логика повторных попыток
│   ├── notifier.py          # Telegram-уведомления
│   ├── scheduler.py         # Планировщик проверок
│   ├── telegram_bot.py      # Telegram-бот
│   ├── logger.py            # Логирование
│   └── time_utils.py        # Работа с временем
├── main.py                  # Точка входа
├── config.yaml              # Конфигурация
├── monitor.db               # SQLite база данных (создаётся автоматически)
├── monitor.log              # Файл логов
├── requirements.txt         # Зависимости
└── README.md
```

## Типы ошибок

| Тип | Описание |
|-----|----------|
| `timeout` | Сервер не ответил за `timeout_seconds` (или TCP-соединение не устанавливается) |
| `dns` | Имя домена не разрешается |
| `conn_refused` | Порт закрыт, веб-сервер не запущен |
| `conn_reset` | Сервер сбрасывает соединение |
| `no_response` | Прочие сетевые ошибки |
| `wrong_code` | Неверный HTTP-код |
| `redirect_loop` | Слишком много редиректов |
| `bad_response` | Ответ оборван или некорректен |
| `keyword_missing` | Ключевое слово не найдено |
| `ssl_expired` | SSL-сертификат истёк |
| `ssl_not_yet_valid` | SSL-сертификат ещё не начал действовать |
| `ssl_mismatch` | SSL-сертификат выдан на другой домен |
| `ssl_untrusted` | Цепочка не доверенная: неполная цепочка, самоподписанный, неизвестный CA |
| `ssl_handshake` | TLS-рукопожатие не удалось |

## Диагностика при падении

Когда сайт переходит в DOWN, бот один раз выполняет пошаговую диагностику и добавляет её в уведомление:

```
Диагностика:
• DNS: ok (158.160.143.34)
• Ping: ok (23 мс)
• TCP 443: нет ответа за 5 с (пакеты отбрасываются: файрвол, блокировка по IP или сервер выключен)
```

Шаги: DNS, ICMP ping, TCP-порт, TLS (сертификат и доверие цепочки), HTTP-запрос.
Для ping от непривилегированного пользователя нужен sysctl `net.ipv4.ping_group_range = 0 2147483647`.

## SSL

- Для каждого HTTPS-сайта проверяются срок действия, соответствие домену и доверие цепочки (как в браузере, по системным корням).
- Если сертификат выдан не издателем с автопродлением (список `ssl.auto_renew_issuers`, по умолчанию Let's Encrypt и ZeroSSL), за `ssl.expiry_warn_days` дней до истечения приходит предупреждение. Повтор не чаще `ssl.remind_every_hours`, перевыпуск сертификата сбрасывает историю (таблица `ssl_warnings`).
- Для Let's Encrypt о сроке не предупреждаем: он продлевается автоматически. Если продление сломалось и сертификат истёк, сайт перейдёт в DOWN с типом `ssl_expired`.
- Корень Let's Encrypt "ISRG Root YR" (цепочка YR1) есть не во всех системных хранилищах: без него сайты с такой цепочкой дают `ssl_untrusted`. Добавление: `deploy/README` (см. ниже).

```yaml
ssl:
  expiry_warn_days: 3
  remind_every_hours: 24
  auto_renew_issuers:
  - Let's Encrypt
  - ZeroSSL

scheduler:
  interval_minutes: 5
  concurrency: 5    # сколько сайтов проверять параллельно (на 1 ядро: 5)
```

## Канал Bitrix24

Уведомления дублируются в общий чат коробочного Bitrix24 через чат-бота. OAuth-приложение не нужно:
все вызовы идут через входящий вебхук со scope `imbot` (мастер "Разработчикам - Добавить чат-бот").
Для `imbot.*` через вебхук обязателен параметр `CLIENT_ID`, он подставляется автоматически.

```yaml
bitrix24:
  enabled: true
  domain: https://portal.example.ru
  webhook_url: https://portal.example.ru/rest/1/xxxxxxxx/     # секрет, не коммитить
  client_id: site_monitor_bot
  handler_base: https://monitor-bot.example.ru:2053            # куда портал шлёт события бота
  bot_code: site_monitor
  bot_name: Мониторинг сайтов
  chat_title: 🌐 Мониторинг сайтов
  chat_dialog_id: ""        # chatNNN; пусто - чат создаётся при первом запуске
  admin_users: [42]         # ID пользователей портала, которым доступны /add и /del
  application_token: ""     # auth[application_token] из событий; пусто - запомнить из первого события
  allowed_ips: [1.2.3.4]    # IP портала: события бота принимаются только с них
```

- Состояние бота в `b24_auth.json` (каталог данных): `bot_id`, `chat_dialog_id`, `application_token`, id команд.
- При каждом старте обработчик событий бота и команд обновляется на `{handler_base}/b24/events`, поэтому переезд на другой сервер не требует перерегистрации.
- HTTP-маршруты службы: `POST /b24/events`, `GET /b24/health`, `POST /b24/install` (заглушка).
- Команды в чате: `/status`, `/check [id]`, `/mute id`, `/unmute id`, `/add url` (админ), `/del id` (админ), `/help`.
- Получатели: общий чат и личные диалоги из `notify_b24_users` сайта. Заглушки Bitrix24 хранятся в `user_mutes` с отрицательным `user_id`.
- События проверяются по `auth[application_token]`. При регистрации через вебхук портал токен не возвращает, поэтому если `bitrix24.application_token` не задан, он запоминается из первого принятого события (после переезда напишите боту любую команду) и дальше сверяется. Чтобы окно доверия не было открыто кому попало, в `allowed_ips` указывается IP портала: события с других адресов отбрасываются. Запомненный токен лежит в `b24_auth.json`, его стоит перенести в `config.yaml`.

## Тихий режим и работа без Telegram

- `quiet: true` в `config.yaml`: события мониторинга только пишутся в лог, в каналы ничего не уходит. Нужен для первого запуска на новом сервере, пока не убедились, что все сайты в UP.
- Пустой `telegram.bot_token`: Telegram-бот не запускается, aiogram не импортируется (экономит ~150 МБ памяти), работают проверки и канал Bitrix24.
- `telegram.proxy`: HTTP CONNECT прокси для api.telegram.org, пусто - напрямую. Для прокси aiogram требует `pip install aiohttp-socks`.

## Журнал изменений конфигурации

Все изменения списка сайтов и настроек через ботов пишутся в `changes.log` рядом с `config.yaml`
(время | автор | действие | объект | детали) и дублируются в `monitor.log` с префиксом "ИЗМЕНЕНИЕ КОНФИГА".
Команда `/changes` в Telegram (админ) показывает последние 20 записей.

Грабля: `save_config()` сериализует секции явно. Новое поле конфига нужно вписать и туда, иначе первое
сохранение через бота сотрёт ключ из `config.yaml`.

## Развёртывание (systemd, отдельный пользователь, venv)

Эталонная установка описана в `deploy/README.md`: код в `/opt/site-checker/app`, данные
(`config.yaml`, `monitor.db`, `monitor.log`, `b24_auth.json`, `changes.log`) в `/opt/site-checker`,
переменная `SITE_CHECKER_DATA` указывает на каталог данных. Шаблоны: `deploy/site-monitor.service`,
`deploy/nginx-monitor-bot.conf`, `deploy/90-site-monitor.sysctl.conf`.

## Формат уведомлений

### Падение сайта
```
🚨 Сайт не отвечает

Название: Example
URL: https://example.com
Поддержка: premium
Проблема: Таймаут: сервер не ответил за 10 с
Что это значит: Сервер принимает соединение или молчит дольше лимита. Смотрите диагностику ниже.
Код: нет ответа
Время ответа: 10002 мс

Диагностика:
• DNS: ok (1.2.3.4)
• Ping: ok (12 мс)
• TCP 443: ok (15 мс)
• TLS: ok (сертификат до 21.10.2026 10:23 UTC, 9 дн., издатель GlobalSign nv-sa)
• HTTP: соединение есть, но ответа нет 10 с (похоже, зависло приложение: PHP, база данных, очередь воркеров)

Время: 01.01.2025 12:00:00 (UTC+4)

[🔇 Заглушить] [🔄 Проверить]
```

### Напоминание
```
🔔 Напоминание #2

Сайт Example всё ещё недоступен
URL: https://example.com
Время простоя: 45 мин
Следующее напоминание через 60 мин

[🔇 Заглушить] [🔄 Проверить]
```

### Восстановление сайта
```
✅ Сайт восстановлен

Название: Example
URL: https://example.com
Статус поддержки: premium
Код: 200
Простой: 47 мин
Время: 01.01.2025 12:47:00 (UTC+4)
```

## Формат логов

```
[2025-01-01 12:00:00+0400] [site1] OK (200) 150ms
[2025-01-01 12:05:00+0400] [site1] ERROR Timeout
```

## Таймзона

Все времена в логах, проверках и уведомлениях указаны в таймзоне **UTC+4** (Europe/Samara, Ижевск).

## Лицензия

MIT
