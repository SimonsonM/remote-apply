# Rollback

For the one-command install, run `./install.sh --uninstall` to remove the generated `./ra` launcher, the Remote Apply desktop entry (if owned by this checkout), and the marked nightly cron line. This keeps the database, profile, answers, resume facts, and backups. Re-run `./install.sh` to restore the launcher. To reverse the schema migration, stop the app and restore a matching `backups/applications.<UTC-timestamp>.sqlite3` database backup. The new `profile_facts` table is additive; on a disposable copy, it can also be removed with `DROP TABLE profile_facts;`.

Stop the application and copy the matching `backups/applications.<UTC-timestamp>.sqlite3` over the database file. CLI answers, including sensitive answers, are stored as plain text in the local database, so protect the restored file with mode `0600`.

To remove only the new attempt state while preserving the current `jobs` table, run these statements against the database:

```sql
DROP TABLE IF EXISTS field_results;
DROP TABLE IF EXISTS blockers;
DROP TABLE IF EXISTS answer_store;
DROP TABLE IF EXISTS standing_authorizations;
DROP TABLE IF EXISTS evidence;
DROP TABLE IF EXISTS responses;
DROP TABLE IF EXISTS followups;
DROP TABLE IF EXISTS attempts;
```

The eight tables are additive. `answer_store` also gains `category` and `status`; `field_results` gains `review_status`. SQLite cannot drop those columns on older versions, so restoring the pre-migration backup is the way to remove them. A backup is created immediately before each migration that changes the schema. To roll back only TASK 07, drop `responses` and `followups` and remove locally generated `followups/` drafts and `reports/status.html` after reviewing them.

## Income experiment rollback

Stop the application and restore the `backups/applications.<UTC-timestamp>.sqlite3` file created before the income schema migration. On a disposable database copy, the additive tables can instead be removed in this order: `income_decisions`, `income_events`, `income_assets`, `income_claims`, `income_sources`, then `income_experiments`. After reviewing any data you want to keep, remove the generated local `income/` directory to remove inventories, saved source text, and drafts. No publication, message, payment, or platform action is undone because this milestone does not perform those actions.
