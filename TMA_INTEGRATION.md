# Интеграция Telegram Mini App с ботом — чек-лист

## 🎯 Что получится

После шагов ниже:
- В боте появится команда `/shop`
- В клавиатуре — кнопка «🛍️ Магазин (приложение)»
- Слева от поля ввода — Menu-кнопка с запуском магазина
- Заказы из Mini App создаются в той же БД (`orders`/`order_items`)
- Админ получает уведомление как обычно через `notify_new_order`
- Если задан `PAYMENTS_PROVIDER_TOKEN` — после заказа клиенту приходит счёт и он платит прямо в Telegram
- Бот раздаёт TMA-статику с того же порта что и healthcheck (через `tma_static_server.py`)

---

## Шаг 1 — заменить `run_server` в `bot.py`

В самом верху `bot.py` сейчас стоит:
```python
from aiohttp import web
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    ...

def run_server():
    ...

threading.Thread(target=run_server, daemon=True).start()
```

Заменить на:
```python
import os
import threading
from tma_static_server import run_server
threading.Thread(target=run_server, daemon=True).start()
```

(Старый `Handler` и `run_server` можно удалить — `tma_static_server.py` их заменит. Healthcheck `/` остаётся «OK».)

---

## Шаг 2 — подключить `tma_handler.py`

Найди строку:
```python
register_admin_handlers(dp, bot)
```

После неё добавь:
```python
from tma_handler import register_tma_handlers
register_tma_handlers(dp, bot, get_db, notify_new_order)
```

Всё. Команда `/shop`, обработчик `web_app_data`, MenuButton, обработка платежей — подключены.

---

## Шаг 3 — переменные окружения в Railway

В сервисе `TG-BOT` (проект `incredible-passion`) → Variables, добавить:

| Имя | Значение | Обязательно? |
|-----|----------|--------------|
| `TMA_URL` | `https://tg-bot-production-XXXX.up.railway.app/tma/` | да |
| `ADMIN_ID` | твой telegram id | если нужны уведомления |
| `PAYMENTS_PROVIDER_TOKEN` | токен от @BotFather → Payments | для онлайн-оплаты |
| `PAYMENTS_CURRENCY` | `RUB` | можно опустить |

`TMA_URL` берётся из публичного домена твоего сервиса. Если нет — Railway → сервис → **Settings → Networking → Generate Domain**, получаешь URL вроде `tg-bot-production-1234.up.railway.app`.

---

## Шаг 4 — настроить @BotFather

1. Открой `@BotFather`
2. `/mybots` → выбрать бот
3. **Bot Settings → Configure Mini App** → ввести `TMA_URL` (тот же из шага 3)
4. **Bot Settings → Payments** → выбрать провайдера (ЮKassa / Сбер / Stripe), получить `PAYMENTS_PROVIDER_TOKEN`, положить в env

(Если не настраивать Payments — просто пропусти, заказы пойдут с пометкой «оплата при получении / счёт менеджером».)

---

## Что появилось внутри TMA

### 📋 Контактная форма перед оплатой

В корзине нажатие «Оформить» открывает экран с табами:

- **👤 Физ. лицо** — имя, телефон, адрес, комментарий
- **🏢 Юр. лицо** — компания, ИНН, юр.адрес, адрес доставки, контакт, телефон, email

Валидация: обязательные поля подсвечиваются красным.

### 💳 Способы оплаты

3 опции:
- **Картой онлайн** — после `confirmOrder()` бот шлёт `Invoice` через Telegram Payments
- **При получении** — заказ просто принят, статус `new`
- **Счёт на юр. лицо** — заказ принят, менеджер отправляет счёт на email

Способ передаётся в `payload.payment_method`. Бот в `tma_handler.py` решает что делать.

### 🎨 Лого

В шапке — настоящий лого Roastberry (4 цветных зерна + надпись), извлечён из бренд-PDF. Файл `tma_static/assets/logo_header.png`.

---

## Структура файлов

```
BOT_TG/
├─ bot.py                    # твой основной бот (нужно правки в шаге 1 и 2)
├─ admin.py                  # админка — без изменений
├─ database.py               # БД — без изменений
├─ tma_handler.py            # 🆕 хендлеры TMA + платежи
├─ tma_static_server.py      # 🆕 раздача статики на порту healthcheck
├─ tma_static/               # 🆕 файлы Mini App
│  ├─ index.html
│  ├─ products.json
│  ├─ photos/                # реальные фото (30 шт, ~4 МБ)
│  └─ assets/                # лого
└─ TMA_INTEGRATION.md        # 🆕 эта инструкция
```

---

## Тестирование

### Локально (на сервере, без Telegram)

```bash
cd /root/projects/ai-agents-rb/BOT_TG
python3 tma_static_server.py
# теперь TMA доступен на http://сервер:10000/tma/
```

### Через Telegram Desktop

1. Запустить бот (с интегрированным `tma_handler`)
2. Отправить `/shop` в чат бота
3. Нажать на «🛍️ Магазин (приложение)»
4. Открывается твой каталог в WebView Telegram
5. Положить пару товаров в корзину → «Оформить» → заполнить форму
6. Подтвердить → бот пришлёт «✅ Заказ №X оформлен» + (если включены платежи) Invoice

### С телефона

Mobile Telegram **требует HTTPS**. Railway автоматически даёт HTTPS по сгенерированному домену.

---

## Что НЕ сделано (на будущее)

- 🔄 **Живые остатки** — TMA читает товары из статичного `products.json`. Чтобы остатки были актуальные — добавить endpoint `/tma/api/products` в `tma_static_server.py`, который тянет данные из БД.
- 👤 **Подтянуть профиль из БД** — если клиент уже регистрировался через бот, можно не спрашивать имя/телефон заново. По `user_id` из `tg.initDataUnsafe.user.id` искать в `users`.
- 📦 **Статусы заказа** — экран «📦 Заказы» сейчас заглушка. Добавить тянуть из БД через API.
- 📸 **Фото товаров** — сейчас рандомные снимки из бренд-бука как фоны. Когда будут реальные фото каждого SKU — положить их в `tma_static/photos/products/<sku>.jpg` и обновить ссылки в `products.json`.
- 🌍 **Свой домен** — вместо `*.up.railway.app` подключить `shop.roastberry.ru` в Railway.

---

## TL;DR

1. Заменить `run_server` в bot.py на импорт из `tma_static_server`
2. Добавить 2 строки регистрации `register_tma_handlers` после `register_admin_handlers`
3. Сгенерировать домен в Railway, положить `TMA_URL` в env
4. В @BotFather → Configure Mini App → ввести URL
5. Опционально: подключить Payments провайдера, скинуть токен в env

После этого `git push` → Railway автоматически передеплоит, `/shop` в боте откроет твой Mini App с настоящим каталогом, заказы пойдут в БД, оплата через Telegram.
