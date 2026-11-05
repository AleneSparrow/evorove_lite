ALTER TABLE businesses ADD COLUMN history_imported_at TEXT;
ALTER TABLE businesses ADD COLUMN mailbox_first_sent_at TEXT;
ALTER TABLE businesses ADD COLUMN mailbox_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN imported_at TEXT;
ALTER TABLE messages ADD COLUMN reply_processed INTEGER NOT NULL DEFAULT 0;
-- Also protect installations imported before this migration existed.
UPDATE messages SET imported_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now'), reply_processed=1
WHERE id LIKE 'legacy:%';
-- The old importer did not record its time. Preserve any native pending queue:
-- the conservative boundary must not be later than its earliest native row.
UPDATE businesses SET history_imported_at=COALESCE(
  (SELECT MIN(m.created_at) FROM messages m JOIN people p ON p.id=m.person_id
   WHERE p.business_id=businesses.id AND m.imported_at IS NULL),
  strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
WHERE id IN (SELECT business_id FROM legacy_business_map);
CREATE TABLE mailbox_sends (
  id TEXT PRIMARY KEY,
  business_id TEXT NOT NULL REFERENCES businesses(id),
  generation INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX mailbox_sends_day ON mailbox_sends(business_id,generation,created_at);
