-- Frozen v0.4.6 schema: used only to verify ORM compatibility with existing files.
PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS generations(
                id TEXT PRIMARY KEY, created REAL NOT NULL, export_id TEXT);
            CREATE TABLE IF NOT EXISTS images(
                generation TEXT NOT NULL, id INTEGER NOT NULL, text TEXT NOT NULL,
                rating TEXT NOT NULL, image_url TEXT NOT NULL, image_key TEXT NOT NULL,
                hub_tags TEXT NOT NULL, raw TEXT NOT NULL, PRIMARY KEY(generation,id));
            CREATE TABLE IF NOT EXISTS overrides(
                image_id INTEGER PRIMARY KEY, manual_tags TEXT NOT NULL DEFAULT '[]',
                excluded_tags TEXT NOT NULL DEFAULT '[]', description TEXT,
                enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS annotations(
                image_id INTEGER PRIMARY KEY, image_key TEXT NOT NULL, source_text TEXT NOT NULL,
                provider_id TEXT NOT NULL, prompt_version INTEGER NOT NULL,
                description TEXT NOT NULL, tags TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS notifications(
                id INTEGER PRIMARY KEY AUTOINCREMENT, image_id INTEGER NOT NULL,
                event TEXT NOT NULL, received REAL NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT);
            CREATE INDEX IF NOT EXISTS notifications_pending
                ON notifications(processed,next_attempt,image_id);
            CREATE TABLE IF NOT EXISTS webhook_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, received REAL NOT NULL,
                receipt_id INTEGER, event TEXT, image_id TEXT, signature_valid INTEGER NOT NULL,
                http_status INTEGER NOT NULL, body TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS tag_jobs(
                image_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT, lease TEXT, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sent_memes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL UNIQUE,
                session TEXT NOT NULL, conversation TEXT NOT NULL, sent_at REAL NOT NULL,
                image_id TEXT NOT NULL, details TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sent_memes_scope
                ON sent_memes(session,conversation,id DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                generation UNINDEXED, image_id UNINDEXED, text, manual_tags, tags, description,
                tokenize='unicode61');
            PRAGMA user_version=2;
