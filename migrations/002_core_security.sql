CREATE TABLE rate_limits (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, window_start INTEGER NOT NULL);
CREATE TABLE billing_events (event_id TEXT PRIMARY KEY, business_id TEXT, event_at TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE INDEX billing_events_business ON billing_events(business_id, event_at);
