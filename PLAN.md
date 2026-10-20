# Shop: личный Home-канал

Root согласовал новую волну без публикации. База — `ba1dc322627908e36a51573d0b3cd3e205afda0e`, Railway project incredible-passion / 408186cc-3c2f-4dd3-9313-6e7f8997fe6a, service TG-BOT / ab9ad0ae-9130-41e0-a592-f31149462491, production d6a8142c-de6a-43be-a5a9-8159a8987f52, deployment 9e20f678-7a44-46f2-9dc9-628cf10094de.

## Контракт

1. Новый `/tma/api/auth/home_token` — только существующий internal bearer + числовой проверенный TG. users.user_id должен существовать; иначе unlinked403, без создания native/ghost аккаунта. JWT `home_entitlement:true`, audience `rb_home_shop`; не подписывать Home токен fallback-secret.
2. Каждый Home API запрос проверяет strict Home shop entitlement, без кеша/редиректов, missing configuration/error/unmanaged deny. Признак берётся только из проверенного JWT, не тела/query. Home JWT имеет приоритет над посторонним injected Telegram initData. Старые native/guest/generic JWT остаются legacy каналом.
3. Только explicit OWNER_TG_ID может быть Home admin exception; отсутствует => все Home токены own scope. ADMIN_IDS не расширяет Home employee; старый owner Portal generic flow неизменён.
4. New channel: orders, purchased products, payment authorization, chat — только exact orders.user_id=verifiedTG. Никакого phone fallback. Claim/ghost transfer недоступен Home. SQL scope до LIMIT. Existing customer_account helper принимает optional strict_identity=False для совместимости.
5. Создание заказа: server-side keyword strict_identity=True идёт HTTP→process_tma_order→create_order_from_tma→_create_order_from_tma. Пропустить _absorb_phone_stub только здесь. Любые caller-body strict_identity/home_entitlement не управляют policy; legacy default сохраняет действующий бизнес flow.
6. Browser Home bootstrap принимает fragment token, затем подтверждает principal сервером. До подтверждения не читает cart/profile/favorites legacy origin storage. Namespace только verified principal, без миграции legacy/global favorites в нового пользователя. Epoch после JSON decode, смена principal/401/403/network очищает private state/DOM; profile/cart/favorites тоже очищаются до нового входа. Settings темы могут остаться общими.
7. Private API no-store. SW должен обходить API и не выдавать кеш предыдущего пользователя; Home startup удаляет/отключает старый Shop worker до private load. Default public catalog/legacy shopping остаются.

## Ограниченные production файлы

- tma_static_server.py: new issuer, central Home auth/check guard, exact ownership/admin helpers, no-store/principal identity.
- customer_account.py: optional strict_identity на resolver/purchased helper.
- tma_handler.py: trusted keyword propagation, Home no-absorb.
- shop_home_access.py: strict synchronous internal checker with no redirect, failclosed.
- tma_static_v2/index.html: actual v2 UI identity/storage/request guards; tma_static_v2/sw.js privacy bypass/version.

База этих файлов сверяется SHA с actual /app перед изменением, manifest beforeSHA неизменяем.

## RED → GREEN

Synthetic A/B с одинаковым телефоном, own/foreign/negative ghost orders; Home list/purchased/pay exact identity, chat/admin role claim denied, native legacy phone union сохраняется. Issuer existing/unlinked, strict revoke/unknown/missing config, no fallback signing, invalid audience/bool/id. Order creation mock phone-stub merge: Home skip vs legacy preserve. Client verified namespaces, no origin-global merge, principal switch, delayed JSON after deny, private SW bypass. Реальные заказы/платежи/DM/account/grants не создаются.

## Публикация

Запрещена в текущем задании. Runtime HOME_ACCESS_URL и HOME_ACCESS_INTERNAL_TOKEN отсутствуют; root настроит до открытия Home. OWNER_TG_ID также отсутствует — default own scope. Нужен независимый review, exact source backup, clean commit/deploy artifact excluding any env/native DB/private files. Root владеет Home broker интеграцией.

## Выполнение и evidence

- RED: первые 7 HTTP тестов и order-create phone collision все8 воспроизвели нарушения; /tmp/shop-red2.log. Frontend первые6 RED→GREEN; дополнительные storage reload/event race RED→GREEN.
- GREEN:19 Home Python tests;14 JS tests;12 существующих customer_account tests. Только synthetic SQLite/HTTP и AST-выбранный order-create handler, никакого импорта bot.py/startup/liveDB и реальных платежей/сообщений.
- Исправлен необходимый интеграционный дефект исходной user/me: `_ensure_pricing_schema(con)` вызывал zero-arg helper и падал TypeError. Вызов исправлен на `_ensure_pricing_schema()`, legacy/Home HTTP200 + verified principal header покрыты тестом.
- Home UI context использует не секретный `?home=1` marker после fragment bootstrap. Отзыв/удаление токена и reload не переключают эту вкладку в legacy origin-global storage. Storage event сравнивается с observed token, а не уже изменившимся localStorage. Новый Home principal загружается только после очистки и серверной проверки.
- Fresh clone имеет7 dirty image aliases: все связаны с NFC/NFD filename collisions на macOS (в Git255 collision groups). Изображения не редактировались и исключены из source commit/artifact; reset/restore этих алиасов не делался. Deployment — только явные6 source файлов поверх actual latest runtime bundle, никаких catalog/photos/nativeDB/env из старой копии.
- Source freeze:6 production afterSHA в SOURCE_BASELINE; до независимого review не коммитить и не публиковать. Root отдельно перепроверяет deployment и SHA из-за параллельных обновлений stock/catalog.

Независимый Astra final PASS/GO:19Python +14JS +12legacy, все6 afterSHA exact. Root разрешил точный source commit и готовит config/deploy отдельно после повторной сверки latest runtime.
