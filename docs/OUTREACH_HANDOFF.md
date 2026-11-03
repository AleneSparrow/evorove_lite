# Outreach (цикл 2) — что сделано и что нужно поменять в чужих файлах

Агент 3. Зона: `app/outreach.py`, `app/guards.py`, `app/prompts/sales_*.md`,
`tests/test_outreach.py`, `tests/test_guards.py`. Ничего другого не менялось.

## Что готово

`app/guards.py` (137 строк) — `can_send()`, `footer()`, плюс
`unsubscribe_url()`, `unsubscribe_headers()` (RFC 8058), `suppress()`,
`recipient_zone()`, `US_STATE_ZONES`.

`app/outreach.py` (529 строк) — `send_first_letters()`, `poll_replies()`,
`record_payment()` с подписями договора, плюс политика ходов, валидатор,
SMTP/IMAP-транспорты и разбор писем.

`app/prompts/sales_analysis.md` — описание входящего письма (модель не выбирает
ход). `app/prompts/sales_reply.md` — формулировка уже выбранного хода.

Схема БД не менялась, новых зависимостей нет.

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

**(в)** Чтобы `person_id` вообще приходил, ссылка оплаты для конкретного
человека должна нести его в `custom`. Это касается не `checkout()` (он
продаёт подписку самой Evorove), а поля `businesses.payment_link`, которое
владелец заполняет руками. Пока владелец не добавит в ссылку
`?checkout[custom][person_id]=<id>`, Done по вебхуку не наступит — останется
ручная кнопка из пункта 2. Решение за владельцем; мой код работает в обоих
случаях.

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

## Решения, о которых нужно знать

**Курсор IMAP лежит в `log`, не в отдельной колонке.** Схема — общая правда, и
менять её ради одного числа я не стал. `poll_replies` пишет строку
`event='imap_cursor', detail='<uid>'` и читает последнюю. Первый опрос ящика
только запоминает позицию и не отвечает на старую почту. Минус: строки
копятся (одна на цикл с новой почтой, раз в 5 минут максимум). Если решите
добавить `businesses.imap_last_uid` отдельной миграцией — у меня меняются
только `read_cursor`/`write_cursor`.

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

`make test` — 96 passed (41 было, 55 моих). Сети нет: SMTP и IMAP
подменяются вызываемыми объектами, LLM — заглушкой; письма собираются
настоящим `email.message.EmailMessage` и разбираются тем же
`outreach.parse_email`, что и реальный IMAP.

Запускались из воркдерева `~/dev/evorove_lite-outreach` интерпретатором
`../evorove_lite/.venv/bin/python` — в самом воркдереве `.venv` нет.
