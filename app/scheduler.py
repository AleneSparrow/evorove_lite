"""One in-process scheduler. SQLite runs is both history and the overlap guard."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import billing, guards, notify, outreach, search
from .db import connect, now, current_run_id

US_ZONES = ('America/New_York', 'America/Chicago', 'America/Denver', 'America/Phoenix',
            'America/Los_Angeles', 'America/Anchorage', 'Pacific/Honolulu')


def allowed_hours(business, current, timezone_name):
    local = current.astimezone(ZoneInfo(timezone_name)).strftime('%H:%M')
    start, end = business['quiet_start'] or '20:00', business['quiet_end'] or '08:00'
    quiet = start <= local < end if start < end else local >= start or local < end
    return start == end or not quiet


class Scheduler:
    def __init__(self, settings):
        self.settings = settings
        self.tasks = set()
        self.loop_task = None

    def claim(self, business_id, kind, since=None):
        with connect(self.settings.db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM runs WHERE business_id IS ? AND kind=? AND status='running'", (business_id, kind)).fetchone():
                return None
            if since and db.execute(
                "SELECT 1 FROM runs WHERE business_id IS ? AND kind=? AND started_at>=?"
                + (" AND status='ok' AND started_at<?" if kind == 'search' else ''),
                (business_id, kind, since, (datetime.fromisoformat(since).astimezone(
                    ZoneInfo(self.settings.get('TIMEZONE_OWNER', 'Asia/Ho_Chi_Minh')))
                    + timedelta(days=1)).astimezone(timezone.utc).isoformat())
                if kind == 'search' else (business_id, kind, since),
            ).fetchone():
                return None
            run_id = str(uuid4())
            db.execute("INSERT INTO runs(id,business_id,kind,started_at,status) VALUES (?,?,?,?,'running')", (run_id, business_id, kind, now()))
        return run_id

    def execute(self, run_id, business_id, kind):
        context_token = current_run_id.set(run_id)
        try:
            if kind == 'search':
                with connect(self.settings.db_path) as db:
                    business = db.execute('SELECT * FROM businesses WHERE id=?', (business_id,)).fetchone()
                    if not business or not billing.can_search(business, settings=self.settings):
                        db.execute("INSERT INTO log(business_id,event,detail,created_at) VALUES (?,'search_skipped',?,?)",
                                   (business_id, 'Lead search requires Pro with subscription access', now()))
                        db.execute("UPDATE runs SET status='skipped',finished_at=? WHERE id=?", (now(), run_id))
                        return
                # Same owner-local day as the first-letter cap (TIMEZONE_OWNER), not a UTC day.
                day = guards._owner_day_start(self.settings, datetime.now(timezone.utc))
                with connect(self.settings.db_path) as db:
                    used = db.execute("SELECT count(*) FROM people WHERE business_id=? AND source_url!='owner' AND created_at>=?",
                                      (business_id, day)).fetchone()[0]
                remaining = max(0, 20 - used)
                people = (search.run_search(business_id) if remaining == 20 else
                          search.run_search(business_id, limit=remaining) if remaining else [])
                found, sent = len(people), 0
            elif kind == 'send':
                found, sent = 0, outreach.send_first_letters(business_id)
            elif kind == 'poll':
                found, sent = outreach.poll_replies(business_id), 0
            else:
                if not notify.daily_summary(self.settings):
                    raise RuntimeError('Summary delivery failed')
                found, sent = 0, 0
            with connect(self.settings.db_path) as db:
                db.execute("UPDATE runs SET status='ok',found=?,sent=?,finished_at=? WHERE id=?", (found, sent, now(), run_id))
        except Exception as exc:
            error_type = type(exc).__name__
            with connect(self.settings.db_path) as db:
                db.execute("UPDATE runs SET status='failed',error=?,finished_at=? WHERE id=?", (error_type, now(), run_id))
            # Alerting is best effort and cannot take down the scheduler.
            try:
                notify.failure_alert(self.settings, business_id, kind, error_type)
            except Exception:
                pass
        finally:
            current_run_id.reset(context_token)

    def dispatch(self, business_id, kind, since=None):
        run_id = self.claim(business_id, kind, since)
        if not run_id:
            return False
        task = asyncio.create_task(asyncio.to_thread(self.execute, run_id, business_id, kind))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def tick(self, current=None):
        current = current or datetime.now(timezone.utc)
        zone = self.settings.get('TIMEZONE_OWNER', 'Asia/Ho_Chi_Minh')
        with connect(self.settings.db_path) as db:
            businesses = [dict(row) for row in db.execute('SELECT * FROM businesses')]
        for business in businesses:
            if not billing.entitled(business, current, settings=self.settings) or business['outreach_paused']:
                continue
            bid = business['id']
            day = guards._owner_day_start(self.settings, current)
            if current.hour >= 13 and billing.can_search(business, current, settings=self.settings):
                self.dispatch(bid, 'search', day)
            for kind, interval in (('send', 15), ('poll', 5)):
                # Scheduler opens a US-wide window; guards check each recipient immediately before SMTP.
                if kind == 'send' and not any(allowed_hours(business, current, recipient_zone) for recipient_zone in US_ZONES):
                    continue
                bucket = current.replace(minute=current.minute // interval * interval, second=0, microsecond=0)
                self.dispatch(bid, kind, bucket.isoformat())
        local = current.astimezone(ZoneInfo(zone))
        if local.hour >= 9 and self.settings.get('TELEGRAM_BOT_TOKEN') and self.settings.get('TELEGRAM_CHAT_ID'):
            self.dispatch(None, 'summary', local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat())

    async def loop(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                # A broken settings row or transient SQLite error does not stop future ticks.
                try:
                    await asyncio.to_thread(notify.failure_alert, self.settings, None, 'scheduler', type(exc).__name__)
                except Exception:
                    pass
            await asyncio.sleep(15)

    def start(self):
        with connect(self.settings.db_path) as db:
            db.execute("UPDATE runs SET status='failed',error='ProcessInterrupted',finished_at=? WHERE status='running'", (now(),))
        self.loop_task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.loop_task:
            self.loop_task.cancel()
            try:
                await self.loop_task
            except asyncio.CancelledError:
                pass
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
