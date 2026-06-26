-- heardhere.uk — D1 schema
-- Run via: wrangler d1 execute heardhere-db --file=schema.sql
-- ============================================================

-- Units table — one row per deployed unit
CREATE TABLE IF NOT EXISTS units (
  id                    TEXT PRIMARY KEY,   -- e.g. "nw-001"
  site_name             TEXT NOT NULL,       -- "Ghyll Head Farm Campsite"
  w3w                   TEXT,               -- "filled.count.soap"
  lat                   REAL,
  lon                   REAL,
  enabled_sensors       TEXT,               -- JSON array: ["birds","bats"]
  theme_colour          TEXT DEFAULT '#2d6a4f',
  version               TEXT,               -- last reported firmware version
  last_heartbeat        INTEGER,            -- unix timestamp
  battery_voltage       REAL,
  battery_percent       REAL,
  signal_dbm            INTEGER,
  disk_free_gb          REAL,
  cpu_percent           REAL,
  last_recording_birds  INTEGER,            -- unix timestamp (watchdog metric)
  last_recording_bats   INTEGER,            -- unix timestamp (watchdog metric)
  created_at            INTEGER DEFAULT (unixepoch())
);

-- Detections — birds and bats share this table, distinguished by `type`
CREATE TABLE IF NOT EXISTS detections (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_id       TEXT NOT NULL,
  type          TEXT NOT NULL CHECK(type IN ('bird', 'bat')),
  species       TEXT NOT NULL,
  scientific    TEXT,
  confidence    REAL,
  detected_at   TEXT,                       -- ISO 8601 string from Pi
  audio_r2_key  TEXT,                       -- R2 object key if clip uploaded
  created_at    INTEGER DEFAULT (unixepoch()),
  FOREIGN KEY (unit_id) REFERENCES units(id)
);

-- Indexes for the queries the API Worker will actually run
CREATE INDEX IF NOT EXISTS idx_detections_unit_type_time
  ON detections(unit_id, type, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_detections_unit_time
  ON detections(unit_id, detected_at DESC);

-- Summary stats — pre-aggregated per unit per day, updated by ingest Worker
-- Avoids expensive COUNT queries on the detections table at read time
CREATE TABLE IF NOT EXISTS daily_stats (
  unit_id       TEXT NOT NULL,
  stat_date     TEXT NOT NULL,              -- YYYY-MM-DD
  bird_count    INTEGER DEFAULT 0,
  bat_count     INTEGER DEFAULT 0,
  species_seen  TEXT,                       -- JSON array of unique species
  PRIMARY KEY (unit_id, stat_date),
  FOREIGN KEY (unit_id) REFERENCES units(id)
);

-- ============================================================
-- Example: seed a test unit (remove before production)
-- ============================================================
-- INSERT INTO units (id, site_name, w3w, lat, lon, enabled_sensors)
-- VALUES (
--   'nw-001',
--   'Test Unit — Threlkeld',
--   'lake.fell.stone',
--   54.6234,
--   -3.0521,
--   '["birds","bats"]'
-- );
