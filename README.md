# mail-bridge

Прокладка между мобильным API Яндекс.Почты и стандартными IMAP/SMTP.
Позволяет работать с ящиком из любой почтовой программы (Thunderbird
и т.п.) **без платной подписки** — сервер общается с теми же API, что и
официальное Android-приложение.

Реверс-инжиниринг выполнен по APK:

- `Yandex Mail 9.27.0` (`ru.yandex.mail`) — протокол **mobapi**

---

## 1. Что было найдено в APK

### Яндекс.Почта (mobapi)

Источник: `com/yandex/mail/network/*` (classes3.dex)

- Base URL: `https://mobapi.mail.yandex.net/api/mobile/` (+ `/v1/`, `/v2/`)
  (`ProductionMailApiEndpoints.java:87`)
- Авторизация: заголовок `Authorization: OAuth <token>`
  (`d71.java`: `"OAuth " + token`)
- User-Agent: `mail2-v123612_productionCommonStoreRelease`
- Retrofit-аннотации: `@sbb`=GET, `@kxh`=POST, `@q8a`=form field,
  `@guj`=query, `@e32`=JSON body, `@cfc`=header

Методы (`RetrofitMailApi.java`, `RetrofitMailApiV2.java`, `RetrofitComposeApi.java`):

| Метод | Endpoint | Назначение |
|---|---|---|
| GET | `v1/settings` | настройки/юзер |
| GET | `v1/xlist` | папки и метки |
| POST | `v1/messages` `{"requests":[{fid,first,last,md5,...}]}` | список писем (конверты) |
| POST | `v1/message_body` form `mids=1,2,3` | тела писем |
| POST | `v1/mark_read` / `mark_unread` form `mids=` | флаги прочтения |
| POST | `v1/move_to_folder` form `mids,fid,current_folder` | перемещение |
| POST | `v1/delete_items` form `mids,current_folder` | удаление |
| POST | `v2/generate_operation_id` | id операции для отправки |
| POST | `v1/send` JSON | **отправка письма** |
| POST | `v1/store` JSON | черновик |
| POST | `v1/upload` multipart | вложения |

Формат `send` (`MailSendRequest.java`):

```json
{
  "compose_check": "1",
  "subj": "тема",
  "to": "a@b.ru,c@d.ru",
  "cc": "", "bcc": "",
  "ttype": "html",
  "from_name": "", "from_mailbox": "you@yandex.ru",
  "send": "<html>…</html>",
  "references": "", "inreplyto": "", "draft_base": "",
  "reply": "", "forward": "", "template_base": "",
  "att_ids": [], "attaches_count": 0,
  "operation_id": "<из generate_operation_id>",
  "notify_on_send": false, "send_time": null, "send_type": null
}
```
Ответ — `SaveDraftResponse {captcha, stored}`; 

при ошибке может
прийти капча (`generate_captcha`/`check_captcha` — в прокладке не реализовано).


## 2. Откуда взять токены

### Яндекс — OAuth-токен

Приложение получает OAuth-токен через passport SDK
(`mobileproxy.passport.yandex.net`). Варианты:

1. **Из cookies браузера** (проще всего): из браузера возьмите куки
   `sessionid2` (и по желанию `Session_id`) и выполните:
   ```
   python scripts/yandex_token.py --sessionid2 "значение"
   ```
   Скрипт использует endpoint приложения
   `POST /1/bundle/oauth/token_by_sessionid` (passport SDK,
   `GetMasterTokenByCookieRequest`), заголовки `Ya-Client-Host` /
   `Ya-Client-Cookie` и client_id/secret из `MailApplication.java:295`.
   Ответ: `{"status":"ok","access_token":"y0_..."}`.
2. **Токен из приложения** (если есть root / `adb backup`):
   база `ru.yandex.mail` (accounts), поле auth-token.
3. **Свой OAuth-клиент** (oauth.yandex.ru/client/new) с почтовыми скоупами.

Токен вставляется в `config.json` (`"token": "y0_AgAAAA..."`).

---

## 3. Запуск

```
cd <путь к проекту>
copy config.example.json config.json   # заполнить токены
python -m bridge check                 # проверка токенов (папки, письма)
python -m bridge                       # поднять серверы
```

Токены можно не хранить в `config.json`, а подставлять из переменных
окружения: любое значение вида `${ИМЯ_ПЕРЕМЕННОЙ}` раскрывается при
загрузке конфига (отсутствующая переменная — ошибка запуска):

```json
"token": "${YANDEX_OAUTH_TOKEN}"
```

```
setx YANDEX_OAUTH_TOKEN "y0_AgAAAA..."    # постоянная переменная (новые окна)
$env:YANDEX_OAUTH_TOKEN = "y0_..."        # только текущая сессия PowerShell
```

По умолчанию (все на 127.0.0.1):

| Аккаунт | IMAP | SMTP | логин | пароль |
|---|---|---|---|---|
| Яндекс | 1143 | 1025 | `yandex` | `bridge` |

Без TLS (локально; при желании — терминируйте stunnel/nginx stream TLS).

### Настройка почтовой программы (Thunderbird)

- Сервер IMAP: `localhost`, порт 1143, SSL: **нет**, аутентификация: обычный пароль
- Сервер SMTP: `localhost`, порт 1025, SSL: **нет**, аутентификация: обычный пароль

## 4. Что реализовано

### IMAP (Yandex)

- LOGIN / LOGOUT / CAPABILITY / NAMESPACE / ID / ENABLE
- LIST / LSUB / SUBSCRIBE / UNSUBSCRIBE
- SELECT / EXAMINE (UIDNEXT, UIDVALIDITY, FLAGS)
- STATUS (MESSAGES, UNSEEN, RECENT, UIDNEXT)
- FETCH (FLAGS, UID, RFC822.SIZE, ENVELOPE, BODYSTRUCTURE, BODY[],
  RFC822, RFC822.HEADER, RFC822.TEXT, BODY[HEADER], BODY[TEXT],
  BODY[HEADER.FIELDS], BODY[n] вложения)
- STORE +FLAGS / -FLAGS (\Seen, $Forwarded, …), UID STORE
- COPY / UID COPY — реализован как серверный move_to_folder
  (удаление в TB: письмо уезжает в Корзину, сессии синхронизируются
  отложенными EXPUNGE)
- APPEND (+ APPENDUID) — черновики в Drafts; APPEND в Sent — no-op
  (сервер сам кладёт копию), нужен Thunderbird'у для завершения отправки
- SEARCH (ALL, SUBJECT, FROM, TO, BODY, SEEN, UNSEEN, UID, NOT, OR,
  CHARSET UTF-8 с литералами — кириллица работает)
- CHECK / NOOP / CLOSE / EXPUNGE
- IDLE (RFC 2177 + отложенные EXISTS; EXPUNGE-очередь флешится после DONE)

### SMTP

- EHLO / HELO (мультистрочный)
- AUTH PLAIN / AUTH LOGIN (опционально для localhost)
- MAIL FROM / RCPT TO / DATA / RSET / NOOP / QUIT
- 8BITMIME / SMTPUTF8 / SIZE
- 250 уходит **после** успешного вызова API (сообщение уже в ящике к
  моменту, когда TB начинает копию в Sent)

### Вложения

- **Чтение**: скачиваются через mobapi `v1/attach` (signed CDN URL) и
  встраиваются в MIME (base64). Thunderbird видит реальные файлы,
  размеры, имена — может сохранять/открывать.
- **Отправка**: работает. Протокол снят фридой с живого приложения:
  `POST https://mail.yandex.ru/api/mobile/v1/upload?client=aphone&app_state=foreground&uuid=<hex>`
  — multipart из двух частей: `filename` (текст) и `attachment` (байты).
  Ответ: `{"status":1, "id":"YWVzX3NpZDp7…"}` — blob подставляется в
  `att_ids` при `v1/send`. Если загрузить вложение не удалось, отправка
  падает с 550 — письма без запрошенных вложений не уходят молча.

### Совместимость с Thunderbird (важные детали)

- **Отправка без зависания прогрессбара**: TB ≥102 после отправки ищет в
  папке Sent копию с совпадающим Message-ID и без неё висит вечно. Мост
  запоминает оригинальные заголовки (Message-ID, To, Cc, Date,
  In-Reply-To, References) из SMTP DATA и подставляет их в серверную
  копию при отдаче по IMAP.
- **250 SMTP отправляется после записи в Sent**, иначе TB успевает
  проверить папку до появления копии.
- **APPEND "Sent"** отвечает фиктивным APPENDUID — TB завершает FCC.
- **COPY/UID COPY** синхронизирует удаление между всеми IMAP-сессиями.

### Прочее

- Кэш папок 2 мин; мета-кэш по папкам; disk-кэш тел писем (Sent).
- Капча Яндекса при подозрительной активности не обрабатывается.
- Заголовки ENVELOPE упрощены (часть NIL) — на отображение в клиенте
  не влияет.

## 5. Структура

```
bridge/
  http.py         — HTTP-клиент (stdlib + SPKI-пиннинг)
  yandex_api.py   — клиент mobapi (Яндекс)
  rfc822.py       — JSON↔MIME конвертеры + parse_outgoing
  backends.py     — общий интерфейс + Yandex-бэкенд
  imap_server.py  — мини-IMAP4rev1 (asyncio)
  smtp_server.py  — мини-SMTP (asyncio)
  main.py         — запуск/проверка
scripts/          — утилиты (yandex_token.py, probe-скрипты, frida-хуки)
tests/            — юнит-тесты (unittest)
AGENTS.md         — заметки для агентов/разработчиков
```

## 5.1 Как снимался протокол вложений

Статический реверс APK не даёт всего: R8 шифрует строки эндпоинтов,
часть методов строится динамически. Финальный протокол загрузки
вложений снят динамически:

1. Android-эмулятор (SDK emulator + образ google_apis, драйвер AEHD,
   VT-x/SVM включён в BIOS) + APK Яндекс.Почты.
2. `frida` + хук на экспорты `SSL_write`/`SSL_read` из `libssl.so`
   (`scripts/hook_native.js`, `scripts/attach_hook.py`) — весь TLS-трафик
   приложения пишется в plaintext до шифрования, сертификат-pinning не
   мешает.
3. В приложении выполняется целевое действие (прикрепить файл, отправить)
   — дамп содержит точные URL, заголовки и формат multipart.

## 6. Права

Код предоставлен для личного использования собственных аккаунтов.
Протоколы и секрет подписи извлечены из официальных распространяемых
приложений; ключи авторизации вы используете свои.
