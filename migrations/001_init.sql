CREATE TABLE businesses (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
  plan TEXT, subscription_status TEXT NOT NULL DEFAULT 'incomplete', ls_customer_id TEXT, ls_subscription_id TEXT,
  offer_text TEXT, payment_link TEXT, postal_address TEXT, sender_name TEXT,
  smtp_host TEXT, smtp_port INTEGER, smtp_user TEXT, smtp_password_enc TEXT,
  imap_host TEXT, imap_port INTEGER, imap_user TEXT, imap_password_enc TEXT,
  daily_cap INTEGER NOT NULL DEFAULT 20, quiet_start TEXT DEFAULT '20:00', quiet_end TEXT DEFAULT '08:00',
  outreach_paused INTEGER NOT NULL DEFAULT 1, brief TEXT);
CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  business_id TEXT REFERENCES businesses(id), role TEXT NOT NULL DEFAULT 'owner', created_at TEXT NOT NULL);
CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
CREATE TABLE password_resets (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER DEFAULT 0);
CREATE TABLE people (id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id),
  name TEXT, role TEXT, email TEXT NOT NULL, company TEXT, website TEXT, us_state TEXT, segment TEXT,
  pain TEXT NOT NULL, evidence_quote TEXT NOT NULL, source_url TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('cold','in_progress','offer_made','done','lost')) DEFAULT 'cold',
  last_move TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (business_id, email));
CREATE TABLE messages (id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  direction TEXT NOT NULL CHECK (direction IN ('out','in')), subject TEXT, body TEXT NOT NULL,
  smtp_message_id TEXT, in_reply_to TEXT, move TEXT, created_at TEXT NOT NULL);
CREATE TABLE suppressions (business_id TEXT NOT NULL, email TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (business_id, email));
CREATE TABLE payments (id TEXT PRIMARY KEY, business_id TEXT, person_id TEXT, source TEXT NOT NULL,
  event_name TEXT, test_mode INTEGER NOT NULL DEFAULT 0, amount_cents INTEGER, raw_event_id TEXT UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE runs (id TEXT PRIMARY KEY, business_id TEXT, kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT, found INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, error TEXT);
CREATE TABLE log (id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT, person_id TEXT, event TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
