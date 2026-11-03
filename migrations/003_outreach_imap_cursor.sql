-- Outreach (cycle 2): the IMAP read position lives on the business row, not in
-- `log`. imap_uidvalidity lets poll_replies notice the mailbox was recreated
-- (UIDs reused by the provider) and re-read recent mail by date instead of by
-- a now-meaningless UID range.
ALTER TABLE businesses ADD COLUMN imap_last_uid INTEGER;
ALTER TABLE businesses ADD COLUMN imap_uidvalidity INTEGER;
