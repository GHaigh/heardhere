#!/usr/bin/env python3
"""
NatureWarden — heardhere.uk
Scheduler, watchdog, and service controller for a heardhere nature station.

Responsibilities:
  - Calculate dawn/dusk for the unit's location
  - Start/stop BirdNET-Pi and acoupi on schedule
  - Monitor that each service is actually producing recordings (not just running)
  - Trigger the uploader on a regular cycle
  - Check for OTA updates daily
  - Report heartbeat to the ingest API

This is the single process that runs at boot. It owns everything else.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import schedule

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR     = Path("/home/pi")
CONFIG_PATH  = BASE_DIR / "sitedata" / "unit.json"
CURSOR_PATH  = BASE_DIR / "sitedata" / "upload_cursor.json"
LOG_PATH     = BASE_DIR / "logs" / "naturewarden.log"
ACOUPI_PID   = BASE_DIR / ".acoupi" / "run" / "default.pid"

# ---------------------------------------------------------------------------
# Logging — to file (systemd also captures stdout)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("naturewarden")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# Dawn / dusk calculation
# ---------------------------------------------------------------------------
def get_sun_times(lat: float, lon: float) -> tuple[datetime, datetime]:
    """Return (dawn, dusk) as aware datetimes for today."""
    try:
        from astral import LocationInfo
        from astral.sun import sun
        import zoneinfo

        location = LocationInfo(latitude=lat, longitude=lon)
        s = sun(location.observer, date=date.today())
        dawn = s["dawn"]
        dusk = s["dusk"]
        return dawn, dusk
    except Exception as e:
        log.warning(f"Sun time calculation failed ({e}) — using fallback 05:00/21:00")
        today = datetime.now()
        dawn = today.replace(hour=5, minute=0, second=0, microsecond=0)
        dusk = today.replace(hour=21, minute=0, second=0, microsecond=0)
        return dawn, dusk

# ---------------------------------------------------------------------------
# Service control helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command, log it, return result."""
    log.debug(f"run: {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, check=check)

def service_active(name: str) -> bool:
    result = run(["systemctl", "is-active", name])
    return result.stdout.strip() == "active"

def start_service(name: str):
    if service_active(name):
        log.info(f"{name} already active — skipping start")
        return
    log.info(f"Starting {name}")
    result = run(["systemctl", "start", name])
    if result.returncode != 0:
        log.error(f"Failed to start {name}: {result.stderr.strip()}")
    else:
        log.info(f"{name} started")

def stop_service(name: str):
    if not service_active(name):
        log.info(f"{name} not active — skipping stop")
        return
    log.info(f"Stopping {name}")
    result = run(["systemctl", "stop", name])
    if result.returncode != 0:
        log.error(f"Failed to stop {name}: {result.stderr.strip()}")
    else:
        log.info(f"{name} stopped")

# ---------------------------------------------------------------------------
# BirdNET-Pi control
# ---------------------------------------------------------------------------

BIRDNET_SERVICES = ["birdnet_analysis", "birdnet_recording"]

def start_birds():
    log.info("=== BIRDS SESSION START ===")
    for svc in BIRDNET_SERVICES:
        start_service(svc)

def stop_birds():
    log.info("=== BIRDS SESSION STOP ===")
    for svc in BIRDNET_SERVICES:
        stop_service(svc)

def birds_producing_output() -> bool:
    """
    Check that BirdNET-Pi has written a new detection file in the last 90 minutes.
    Returns True if healthy, False if stalled.
    """
    detection_dirs = [
        BASE_DIR / "BirdNET-Pi" / "scripts" / "spectrogram",
        Path("/tmp/birdnet"),
    ]
    cutoff = time.time() - (90 * 60)  # 90 minutes ago
    for d in detection_dirs:
        if d.exists():
            for f in d.iterdir():
                if f.stat().st_mtime > cutoff:
                    return True
    # Also check BirdNET-Pi's own log for recent activity
    log_path = BASE_DIR / "BirdNET-Pi" / "scripts" / "birdnet_log.txt"
    if log_path.exists() and log_path.stat().st_mtime > cutoff:
        return True
    return False

# ---------------------------------------------------------------------------
# acoupi / bat control
# ---------------------------------------------------------------------------

def clear_acoupi_pid():
    """Remove stale PID file before starting acoupi."""
    if ACOUPI_PID.exists():
        log.warning(f"Removing stale acoupi PID: {ACOUPI_PID}")
        ACOUPI_PID.unlink()

def start_bats():
    log.info("=== BATS SESSION START ===")
    clear_acoupi_pid()
    # acoupi is started as a subprocess (not systemd) so we can manage it directly
    # This avoids the celery worker stall that caused the 33-hour outage
    global _acoupi_proc
    try:
        _acoupi_proc = subprocess.Popen(
            ["/home/pi/venv/bin/acoupi", "start", "--program", "batdetect2"],
            stdout=open(BASE_DIR / "logs" / "acoupi.log", "a"),
            stderr=subprocess.STDOUT,
        )
        log.info(f"acoupi started (PID {_acoupi_proc.pid})")
    except Exception as e:
        log.error(f"Failed to start acoupi: {e}")
        _acoupi_proc = None

def stop_bats():
    log.info("=== BATS SESSION STOP ===")
    global _acoupi_proc
    if _acoupi_proc and _acoupi_proc.poll() is None:
        log.info(f"Terminating acoupi (PID {_acoupi_proc.pid})")
        _acoupi_proc.terminate()
        try:
            _acoupi_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("acoupi did not terminate — killing")
            _acoupi_proc.kill()
        _acoupi_proc = None
    clear_acoupi_pid()
    log.info("Bats session stopped")

def bats_producing_output() -> bool:
    """
    Check that acoupi has written a new recording in the last 30 minutes.
    This is the fix for the 33-hour silent outage — we check output, not process state.
    """
    bat_dir = BASE_DIR / "storages" / "recordings" / "bats"
    if not bat_dir.exists():
        return False
    cutoff = time.time() - (30 * 60)  # 30 minutes
    for f in bat_dir.rglob("*.wav"):
        if f.stat().st_mtime > cutoff:
            return True
    return False

# Global handle for acoupi subprocess
_acoupi_proc = None

# ---------------------------------------------------------------------------
# Schedule builder — recalculates daily based on sunrise/sunset
# ---------------------------------------------------------------------------

class DailySchedule:
    """
    Recalculates dawn/dusk each day and schedules sessions accordingly.

    Bird window:  dawn - 30min  →  dawn + 4 hours  (covers dawn chorus)
    Bat window:   dusk - 30min  →  dusk + 3 hours  (peak bat activity)

    Both windows have a 30-minute "warmup" buffer before the event,
    accounting for species that start before official dawn/dusk.
    """

    def __init__(self, config: dict):
        self.config = config
        self.lat = config["lat"]
        self.lon = config["lon"]
        self.sensors = config.get("enabled_sensors", ["birds", "bats"])
        self._today = None

    def build_for_today(self):
        today = date.today()
        if self._today == today:
            return  # Already built for today

        self._today = today
        dawn, dusk = get_sun_times(self.lat, self.lon)

        bird_start = dawn - timedelta(minutes=30)
        bird_stop  = dawn + timedelta(hours=4)
        bat_start  = dusk - timedelta(minutes=30)
        bat_stop   = dusk + timedelta(hours=3)

        log.info(f"Today's schedule: dawn={dawn:%H:%M} dusk={dusk:%H:%M}")
        log.info(f"  Birds: {bird_start:%H:%M} → {bird_stop:%H:%M}")
        log.info(f"  Bats:  {bat_start:%H:%M} → {bat_stop:%H:%M}")

        # Clear existing jobs and rebuild
        schedule.clear("detection")

        if "birds" in self.sensors:
            schedule.every().day.at(bird_start.strftime("%H:%M")).do(start_birds).tag("detection")
            schedule.every().day.at(bird_stop.strftime("%H:%M")).do(stop_birds).tag("detection")

        if "bats" in self.sensors:
            schedule.every().day.at(bat_start.strftime("%H:%M")).do(start_bats).tag("detection")
            schedule.every().day.at(bat_stop.strftime("%H:%M")).do(stop_bats).tag("detection")

        # Rebuild schedule at midnight each day
        schedule.every().day.at("00:01").do(self.build_for_today).tag("detection")

# ---------------------------------------------------------------------------
# Watchdog — checks services are producing output, not just running
# ---------------------------------------------------------------------------

def watchdog_check(config: dict):
    """
    Run every 15 minutes during active detection windows.
    Checks that running services are actually producing output.
    Restarts if stalled. This is the real fix for the acoupi outage.
    """
    now = datetime.now()
    dawn, dusk = get_sun_times(config["lat"], config["lon"])

    in_bird_window = (
        (dawn - timedelta(minutes=30)) <= now.replace(tzinfo=None) <=
        (dawn + timedelta(hours=4))
        if dawn.tzinfo is None else
        (dawn - timedelta(minutes=30)).replace(tzinfo=None) <= now <=
        (dawn + timedelta(hours=4)).replace(tzinfo=None)
    )
    in_bat_window = False  # Simplified — extend with proper tz handling

    # Bird watchdog
    if "birds" in config.get("enabled_sensors", []):
        if service_active("birdnet_analysis"):
            if not birds_producing_output():
                log.warning("WATCHDOG: BirdNET-Pi running but not producing output — restarting")
                stop_birds()
                time.sleep(5)
                start_birds()

    # Bat watchdog — check subprocess is alive and producing
    if "bats" in config.get("enabled_sensors", []):
        global _acoupi_proc
        if _acoupi_proc is not None:
            if _acoupi_proc.poll() is not None:
                log.warning(f"WATCHDOG: acoupi process died (exit {_acoupi_proc.returncode}) — restarting")
                stop_bats()
                time.sleep(5)
                start_bats()
            elif not bats_producing_output():
                log.warning("WATCHDOG: acoupi running but no recent recordings — restarting")
                stop_bats()
                time.sleep(5)
                start_bats()

# ---------------------------------------------------------------------------
# Uploader trigger
# ---------------------------------------------------------------------------

def trigger_upload():
    """Call the uploader script. Runs in a subprocess so a failure can't crash naturewarden."""
    log.info("Triggering upload batch")
    result = run(["/home/pi/venv/bin/python3", "/home/pi/naturewarden/uploader.py"])
    if result.returncode != 0:
        log.error(f"Upload failed: {result.stderr.strip()}")
    else:
        log.info("Upload batch complete")

# ---------------------------------------------------------------------------
# OTA update check
# ---------------------------------------------------------------------------

def check_for_updates(config: dict):
    """Check the update Worker for a newer version. Apply if found."""
    import requests
    try:
        current_version = config.get("version", "unknown")
        resp = requests.get(
            f"{config['ingest_url'].replace('ingest', 'update')}/api/update-check",
            params={"unit_id": config["unit_id"], "version": current_version},
            headers={"X-API-Key": config["api_key"]},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("update_available"):
                log.info(f"Update available: {data['version']} — applying")
                result = run(["/home/pi/naturewarden/updater.sh", data["script_url"]])
                if result.returncode == 0:
                    log.info("Update applied — restarting")
                    sys.exit(0)  # systemd Restart=always brings us back on new code
                else:
                    log.error(f"Update failed: {result.stderr}")
            else:
                log.debug("No update available")
        else:
            log.warning(f"Update check returned {resp.status_code}")
    except Exception as e:
        log.warning(f"Update check failed (non-fatal): {e}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("  NatureWarden starting — heardhere.uk")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    config = load_config()
    log.info(f"Unit: {config['unit_id']} — {config['site_name']}")
    log.info(f"Location: {config['lat']}, {config['lon']} (///{config['w3w']})")
    log.info(f"Sensors: {config['enabled_sensors']}")

    # Build today's detection schedule
    daily = DailySchedule(config)
    daily.build_for_today()

    # Watchdog — every 15 minutes
    schedule.every(15).minutes.do(watchdog_check, config=config).tag("watchdog")

    # Upload — every 15 minutes
    schedule.every(15).minutes.do(trigger_upload).tag("upload")

    # OTA check — once per day at 03:00 (quiet period)
    schedule.every().day.at("03:00").do(check_for_updates, config=config).tag("ota")

    log.info("Scheduler running. All times local to unit location.")

    # Main loop
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log.error(f"Scheduler error (continuing): {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()
