# Outreach (цикл 2) — что сделано и что нужно поменять в чужих файлах

Агент 3. Зона: `app/outreach.py`, `app/guards.py`, `app/prompts/sales_*.md`,
`tests/test_outreach.py`, `tests/test_guards.py`. Ничего другого не менялось.

## Что готово

`app/guards.py` (137 строк) — `can_send()`, `footer()`, плюс
`unsubscribe_url()`, `unsubscribe_headers()` (RFC 8058), `suppress()`,
`recipient_zone()`, `US_STATE_ZONES`.

`app/outreach.py` (565 строк) — `send_first_letters()`, `poll_replies()`,
`record_payment()` с подписями договора, плюс политика ходов, валидатор,
SMTP/IMAP-транспорты и разбор писем.

`app/prompts/sales_analysis.md` — описание входящего письма (модель не выбирает
ход). `app/prompts/sales_reply.md` — формулировка уже выбранного хода.

Миграция `003_outreach_imap_cursor.sql` добавляет
`businesses.imap_last_uid`/`imap_uidvalidity` (см. «Решения» ниже). Это
единственная правка схемы. Новых зависимостей нет.

Обновление по ходу работы агента glue: правки `billing.py` (Done по
`custom_data.person_id`) и кнопку «Record payment» в `main.py`/`person.html`
(пункты 1–2 ниже) делает агент в `feat/glue` — он учитывает этот файл, я их не
трогала. Оставляю инструкции как есть — они описывают контракт с моей
стороны (`record_payment(..., db=db)`, проверка владения `business_id`).

## Правки, которые нужны вне моей зоны

### 1. `app/billing.py` — Done по оплате с `custom_data.person_id`

Без этой правки `record_payment` вызывается только вручную.

**(а)** Рядом со строкой, читающей `business_id` из `custom_data`, прочитать и
`person_id`:

```python
business_id = (meta.get("custom_data") or {}).get("business_id")
person_id = (meta.get("custom_data") or {}).get("person_id")      # добавить
```

**(б)** В ветке `if name in INVOICES:` в `INSERT ... INTO payments` сейчас не
заполняется колонка `person_id`, хотя она есть в схеме. Добавить её и, когда
`person_id` задан, перевести карточку в Done **тем же соединением** (`db`),
внутри уже открытого `BEGIN IMMEDIATE`:

```python
db.execute("INSERT OR IGNORE INTO payments(id,business_id,person_id,source,event_name,test_mode,amount_cents,raw_event_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
           (str(uuid4()), business_id, person_id, "lemonsqueezy", name, int(test), amount, "invoice:" + str(data["id"]), now()))
if person_id:
    try:
        outreach.record_payment(business_id, person_id, "lemonsqueezy", db=db)
    except ValueError:
        pass   # person_id из чужого бизнеса или удалённая карточка: платёж записан, карточка не трогается
return
```

плюс `from . import outreach` в импортах.

Важно: `record_payment(..., db=db)` — это отдельный параметр именно для
вебхука. Вызов без `db` открыл бы второе соединение к тому же файлу SQLite и
ждал бы освобождения `BEGIN IMMEDIATE` до таймаута. Проверка `person_id`
привязана к `business_id`, так что чужой `person_id` в `custom_data`
злоумышленника ничего не даёт.

**(в)** Чтобы `person_id` вообще приходил, письмо с оффером должно слать
персональную ссылку, а не сырой `businesses.payment_link`. Это уже сделано с
моей стороны через `billing.payment_link_for(business, person_id)` — см.
пункт 5.

### 2. `app/main.py` + `app/templates/person.html` — кнопка «Record payment»

Ручной путь в Done, только для роли `owner`.

В `app/main.py` рядом с остальными `@app.post('/app/...')`:

```python
@app.post('/app/people/{person_id}/payment')
async def record_payment(request: Request, person_id: str):
    await form(request)
    s = request.app.state.settings
    user = auth.current_user(s, request)
    if user['role'] != 'owner':
        raise HTTPException(403, 'Owner only')
    await run_in_threadpool(outreach.record_payment, user['business_id'], person_id, 'manual')
    return render(request, 'person.html', person=board.person(s, user['business_id'], person_id))
```

плюс `outreach` в строку `from . import auth, billing, board`.

В `app/templates/person.html` — кнопка, видимая пока карточка не в Done:

```html
{% if person.stage != 'done' %}
<form method="post" action="/app/people/{{ person.id }}/payment"
      hx-post="/app/people/{{ person.id }}/payment" hx-target="closest article">
  <input type="hidden" name="csrf" value="{{ csrf }}">
  <button type="submit">Record payment</button>
</form>
{% endif %}
```

`record_payment` сам проверяет, что человек принадлежит этому бизнесу, и
бросает `ValueError` иначе, поэтому арендатор не может перевести чужую
карточку. Если хочется 404 вместо 500 — оберните вызов в `try/except
ValueError` и поднимите `HTTPException(404)`.

### 3. `app/llm.py` (агент 2) — замена заглушки одной строкой

В `app/outreach.py` есть функция `ask()` с подписью договора. При слиянии
удалить её целиком и поставить вместо неё:

```python
from .llm import ask
```

Больше ничего менять не нужно: вызовы уже идут через `ask(...)` с
`purpose='sales_analysis'` и `purpose='sales_reply'`, и обе функции
переживают исключение из `ask` (анализ деградирует до discovery, формулировка
— до безопасного текста хода).

### 4. `app/scheduler.py` — не обязательно, но лучше

Scheduler вызывает `outreach.send_first_letters(business_id)` без настроек, как
и требует договор, поэтому `outreach` при первом вызове сам делает
`settings.load()` и кеширует результат в модуле. Это второе чтение `.env` в
процессе. Если удобно, передайте настройки, которые у scheduler уже есть:

```python
found, sent = 0, outreach.send_first_letters(business_id, settings=self.settings)
...
found, sent = outreach.poll_replies(business_id, settings=self.settings), 0
```

Параметры именованные и необязательные, подпись договора не нарушается.

### 5. `app/billing.py` — `payment_link_for(business, person_id)`

`outreach.py` зовёт эту функцию при отправке оффера вместо сырого
`business['payment_link']`, чтобы в вебхук пришёл `custom_data.person_id` (см.
пункт 1в). До слияния у меня в `outreach.py` — тонкая заглушка с той же
подписью (как `ask()` для `llm.py`): она пытается `from .billing import
payment_link_for`, а если модуля ещё нет — просто отдаёт
`business['payment_link'].strip()`.

Контракт для billing-агента:

```python
def payment_link_for(business: dict, person_id: str) -> str:
    """Per-person checkout link carrying custom_data.person_id, so the webhook
    can close the right card. business['payment_link'] is the owner's base
    Lemon Squeezy URL; append the checkout custom-data query params to it."""
```

Например, добавить `?checkout[custom][person_id]=<person_id>` (и
`checkout[custom][business_id]=<business['id']>`, если вебхуку так проще) к
`business['payment_link']`. Когда функция появится в `app/billing.py`, в
`app/outreach.py` нужно удалить заглушку `payment_link_for` целиком и
поставить:

```python
from .billing import payment_link_for
```

Больше ничего в `outreach.py` менять не нужно — вызов уже
`payment_link_for(business, person['id'])` внутри `compose_reply`.

## Решения, о которых нужно знать

**Курсор IMAP теперь на самой строке business, не в `log`.** Миграция
`003_outreach_imap_cursor.sql` добавляет `businesses.imap_last_uid` и
`imap_uidvalidity`. Первый опрос ящика только запоминает позицию (не читает
`UNSEEN` — это было багом в первой версии: пустой `UNSEEN` на старом ящике
обнулял курсор и на следующем опросе читал всё подряд; теперь это `SEARCH
ALL`, только чтобы найти текущий максимальный UID). Если провайдер
пересобрал ящик и `UIDVALIDITY` изменился, старый диапазон UID ничего не
значит — `imap_fetch` тогда ищет `SINCE` за последние 7 дней вместо `UID
N:*`; `handle_reply` всё равно отвечает один раз на каждое письмо, потому что
дедуп идёт по `Message-ID`, а не по UID.

**Часовой пояс получателя — из `people.us_state`**, таблица в
`guards.US_STATE_ZONES`. Старый код выводил зону из ZIP-префиксов, но в этой
схеме ZIP не хранится. Разделённые штаты (KY, TN, KS, NE, обе Dakota, ID)
получают зону, в которую попадает большая часть их ZIP — то же решение, что в
`us_postal_timezones.py`. Неопознанный штат даёт Eastern, как и раньше.
Агенту поиска: заполняйте `us_state` двухбуквенным кодом или полным названием,
оба понимаются.

**`can_send` сама ищет человека по email**, чтобы не расширять подпись
договора. Поэтому порядок такой: сначала настройки бизнеса, потом права
получателя, потом репутация. Причины отказа: `no_email`,
`unknown_business`, `outreach_paused`, `mailbox_not_configured`,
`postal_address_required`, `base_url_required`, `suppressed`, `quiet_hours`,
`daily_cap`, `delivery_failures`.

**Три отказа сервера по одному адресу за 30 дней** (`log.event='send_failed'`)
блокируют адрес; жёсткий отказ получателя (`SMTPRecipientsRefused`) сразу
попадает в `suppressions` с причиной `bounce`. Сбой аутентификации или связи
пишется как `smtp_error` и **не** засчитывается адресу — иначе одна поломка
ящика заморозила бы всю базу.

**`move='offer'`** сохраняется в нижнем регистре, как ждёт `notify.py`.
Остальные ходы: `greet`, `ask_discovery`, `confirm_need`, `present_value`,
`diagnose_objection`, `answer_objection`, `nurture`, `end_contact`, `handoff`.

**Ход `handoff`** (юридический вопрос, жалоба, просьба позвать человека)
отправляется дословным серверным текстом, без обращения к модели, и оставляет
карточку в In progress — как `HANDOFF_TO_HUMAN` в старом валидаторе. Отдельного
экрана «ждёт владельца» в lite нет; если он понадобится, фильтруйте по
`people.last_move='handoff'`.

**Done наступает только из `record_payment`.** Ни отправка оффера, ни переход
по ссылке карточку не закрывают.

## Тесты

`make test` — 102 passed (41 было, 61 мой). Сети нет: SMTP и IMAP
подменяются вызываемыми объектами, LLM — заглушкой; письма собираются
настоящим `email.message.EmailMessage` и разбираются тем же
`outreach.parse_email`, что и реальный IMAP. Для самой `imap_fetch` (а не
только для инжектируемой замены `fetch=`) есть отдельная заглушка
`imaplib.IMAP4_SSL`, которая проверяет три ветки: первый опрос, обычное
продолжение по UID и перечитывание за 7 дней при смене `UIDVALIDITY`.

Запускались из воркдерева `~/dev/evorove_lite-outreach` интерпретатором
`../evorove_lite/.venv/bin/python` — в самом воркдереве `.venv` нет.
