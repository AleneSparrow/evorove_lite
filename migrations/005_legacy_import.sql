CREATE TABLE legacy_business_map (
  old_business_id TEXT PRIMARY KEY,
  business_id TEXT NOT NULL REFERENCES businesses(id)
);
ALTER TABLE log ADD COLUMN legacy_note_id TEXT;
CREATE UNIQUE INDEX log_legacy_note ON log(legacy_note_id) WHERE legacy_note_id IS NOT NULL;
