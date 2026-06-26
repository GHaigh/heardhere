#!/usr/bin/env python3
"""
NatureWarden uploader — heardhere.uk
Reads local detection data, sends unsent records to the ingest Worker,
updates the upload cursor on success. Safe to run repeatedly — idempotent.
"""

import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import requests

log = logging.getLogger("uploader")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [uploader] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR      = Path("/home/pi")
CONFIG_PATH   = BASE_DIR / "sitedata" / "unit.json"
CURSOR_PATH   = BASE_DIR / "sitedata" / "upload_cursor.json"
BAT_DB_PATH   = BASE_DIR / "storages" / "metadata.db"
BIRD_JSON     = BASE_DIR / "BirdNET-Pi" / "scripts" / "birds.json"

# ---------------------------------------------------------------------------
# Config + cursor
# ---------------------------------------------------------------------------
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

def load_cursor() -> dict:
    if CURSOR_PATH.exists():
        with open(CURSOR_PATH) as f:
            return json.load(f)
    return {"birds_last_id": 0, "bats_last_id": 0, "last_heartbeat": 0}

def save_cursor(cursor: dict):
    with open(CURSOR_PATH, "w") as f:
        json.dump(cursor, f, indent=2)

# ---------------------------------------------------------------------------
# Harvest new bird detections from BirdNET-Pi's birds.json
# ---------------------------------------------------------------------------
def harvest_birds(cursor: dict) -> list[dict]:
    """
    BirdNET-Pi appends to birds.json. We track last uploaded index.
    Returns only new entries since last upload.
    """
    if not BIRD_JSON.exists():
        log.debug("birds.json not found — no bird data yet")
        return []

    try:
        with open(BIRD_JSON) as f:
            all_detections = json.load(f)
    except json.JSONDecodeError:
        log.warning("birds.json malformed — skipping bird upload this cycle")
        return []

    last_id = cursor.get("birds_last_id", 0)
    new_detections = []

    for i, d in enumerate(all_detections):
        if i <= last_id:
            continue
        new_detections.append({
            "type": "bird",
            "species": d.get("Com_Name", "Unknown"),
            "scientific": d.get("Sci_Name", ""),
            "confidence": float(d.get("Confidence", 0)),
            "detected_at": d.get("Date", "") + "T" + d.get("Time", ""),
            "source_index": i,
        })

    return new_detections

# ---------------------------------------------------------------------------
# Harvest new bat detections from acoupi's SQLite DB
# ---------------------------------------------------------------------------
def harvest_bats(cursor: dict) -> list[dict]:
    """
    acoupi writes to metadata.db. We use the rowid as the cursor.
    """
    if not BAT_DB_PATH.exists():
        log.debug("metadata.db not found — no bat data yet")
        return []

    last_id = cursor.get("bats_last_id", 0)
    new_detections = []

    try:
        conn = sqlite3.connect(str(BAT_DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Query new records since last uploaded rowid
        # Column names based on acoupi/BatDetect2 schema — verify on your device
        cur.execute("""
            SELECT
                rowid,
                species_name,
                detection_confidence,
                datetime(created_at) as detected_at,
                recording_path
            FROM detection_predictions
            WHERE rowid > ?
            ORDER BY rowid ASC
            LIMIT 500
        """, (last_id,))

        for row in cur.fetchall():
            new_detections.append({
                "type": "bat",
                "species": row["species_name"] or "Unknown",
                "confidence": float(row["detection_confidence"] or 0),
                "detected_at": row["detected_at"],
                "source_rowid": row["rowid"],
                "recording_path": row["recording_path"],
            })

        conn.close()
    except sqlite3.Error as e:
        log.warning(f"Bat DB query failed: {e}")

    return new_detections

# ---------------------------------------------------------------------------
# System health metrics for heartbeat
# ---------------------------------------------------------------------------
def get_health_metrics() -> dict:
    metrics = {}
    try:
        import psutil
        metrics["cpu_percent"] = psutil.cpu_percent(interval=1)
        metrics["memory_percent"] = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/")
        metrics["disk_used_gb"] = round(disk.used / 1e9, 2)
        metrics["disk_free_gb"] = round(disk.free / 1e9, 2)
        metrics["uptime_seconds"] = int(time.time() - psutil.boot_time())
    except Exception as e:
        log.debug(f"psutil metrics failed: {e}")

    # Battery / UPS HAT via I2C (Waveshare UPS HAT)
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        # Waveshare UPS HAT (B) address 0x36 — MAX17040 fuel gauge
        voltage_raw = bus.read_word_data(0x36, 0x02)
        voltage = ((voltage_raw & 0xFF) << 8 | (voltage_raw >> 8)) * 1.25 / 1000 / 16
        soc_raw = bus.read_word_data(0x36, 0x04)
        soc = ((soc_raw & 0xFF) << 8 | (soc_raw >> 8)) / 256
        metrics["battery_voltage"] = round(voltage, 2)
        metrics["battery_percent"] = round(soc, 1)
    except Exception:
        pass  # UPS HAT may not be fitted on dev/test unit

    # Signal strength from 4G modem via AT command
    try:
        import serial
        with serial.Serial("/dev/ttyUSB2", 115200, timeout=2) as ser:
            ser.write(b"AT+CSQ\r\n")
            time.sleep(0.5)
            response = ser.read(100).decode(errors="ignore")
            if "+CSQ:" in response:
                csq_val = int(response.split("+CSQ:")[1].split(",")[0].strip())
                # Convert CSQ to approximate dBm: dBm = (CSQ * 2) - 113
                if csq_val != 99:
                    metrics["signal_dbm"] = (csq_val * 2) - 113
    except Exception:
        pass  # Modem not available in dev

    return metrics

# ---------------------------------------------------------------------------
# Upload payload to ingest Worker
# ---------------------------------------------------------------------------
def upload(config: dict, payload: dict) -> bool:
    """POST payload to ingest Worker. Returns True on success."""
    try:
        resp = requests.post(
            f"{config['ingest_url']}/api/ingest",
            json=payload,
            headers={
                "X-API-Key": config["api_key"],
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        if resp.status_code == 200:
            return True
        else:
            log.error(f"Ingest returned {resp.status_code}: {resp.text[:200]}")
            return False
    except requests.RequestException as e:
        log.error(f"Upload request failed: {e}")
        return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    config = load_config()
    cursor = load_cursor()

    log.info(f"Upload cycle starting — unit {config['unit_id']}")

    # --- Harvest new detections ---
    sensors = config.get("enabled_sensors", [])
    new_birds = harvest_birds(cursor) if "birds" in sensors else []
    new_bats = harvest_bats(cursor) if "bats" in sensors else []
    health = get_health_metrics()

    total_new = len(new_birds) + len(new_bats)
    log.info(f"New detections: {len(new_birds)} birds, {len(new_bats)} bats")

    # --- Build payload ---
    payload = {
        "unit_id": config["unit_id"],
        "timestamp": int(time.time()),
        "version": config.get("version", "unknown"),
        "detections": new_birds + new_bats,
        "health": health,
    }

    # --- Upload ---
    if total_new > 0 or (time.time() - cursor.get("last_heartbeat", 0)) > 900:
        # Upload if there's new data, or if heartbeat hasn't been sent in 15 min
        success = upload(config, payload)
        if success:
            # Update cursor only on confirmed success
            if new_birds:
                # Find the highest source_index in the uploaded batch
                cursor["birds_last_id"] = max(d["source_index"] for d in new_birds)
            if new_bats:
                cursor["bats_last_id"] = max(d["source_rowid"] for d in new_bats)
            cursor["last_heartbeat"] = int(time.time())
            save_cursor(cursor)
            log.info(f"Upload successful — cursor updated")
        else:
            log.warning("Upload failed — cursor not updated, will retry next cycle")
    else:
        log.debug("No new data and heartbeat is recent — skipping upload")


if __name__ == "__main__":
    main()
