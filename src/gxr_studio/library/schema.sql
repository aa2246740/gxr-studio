CREATE TABLE IF NOT EXISTS shots(
  shot_id TEXT PRIMARY KEY, name TEXT NOT NULL, folder TEXT NOT NULL,
  date TEXT, date_source TEXT, captured_at TEXT,
  unit TEXT, lens TEXT, iso INTEGER, orientation INTEGER, width INTEGER, height INTEGER,
  has_raw INTEGER NOT NULL, has_jpg INTEGER NOT NULL, added_at TEXT NOT NULL,
  copyright TEXT, recipe_tag TEXT);
CREATE TABLE IF NOT EXISTS files(
  shot_id TEXT NOT NULL, kind TEXT NOT NULL, relpath TEXT NOT NULL,
  sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  PRIMARY KEY(shot_id, kind));
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS files_rel ON files(relpath);
CREATE TABLE IF NOT EXISTS seen(
  card_key TEXT NOT NULL, relpath TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  sha256 TEXT NOT NULL, PRIMARY KEY(card_key, relpath));
CREATE TABLE IF NOT EXISTS seen2(
  volume_uuid TEXT NOT NULL, relpath TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  sha256 TEXT NOT NULL, read_at TEXT NOT NULL, PRIMARY KEY(volume_uuid, relpath));
CREATE TABLE IF NOT EXISTS videos(
  relpath TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, added_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS videos_sha ON videos(sha256);
CREATE TABLE IF NOT EXISTS filecheck(
  relpath TEXT PRIMARY KEY, ino INTEGER NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL,
  sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS edits(
  shot_id TEXT PRIMARY KEY, sidecar_rev INTEGER NOT NULL, updated_at TEXT, crop_json TEXT);
CREATE INDEX IF NOT EXISTS shots_date ON shots(date, captured_at, name);
