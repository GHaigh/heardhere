#!/usr/bin/env bash
# =============================================================================
# heardhere.uk — provision.sh
# Run once on a fresh Raspberry Pi OS Lite (64-bit) image.
# Produces a fully operational NatureWarden unit.
#
# Usage:
#   sudo bash provision.sh
#
# You will be prompted for:
#   - Unit ID        (e.g. nw-001)
#   - Site name      (e.g. "Ghyll Head Farm Campsite")
#   - What3words     (e.g. filled.count.soap)
#   - Latitude       (e.g. 54.6234)
#   - Longitude      (e.g. -3.0521)
#   - Enabled sensors (birds, bats, weather — comma separated)
#   - Ingest API key (from your Cloudflare KV, per-unit)
#   - Ingest URL     (e.g. https://ingest.heardhere.uk)
#
# Expected runtime: ~25 minutes on a fresh image with good connectivity.
# =============================================================================

set -euo pipefail

# --- Colours -----------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m' # No Colour

log()  { echo -e "${GREEN}[provision]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err()  { echo -e "${RED}[error]${NC} $*"; exit 1; }
step() { echo -e "\n${BLUE}══ $* ${NC}"; }

# --- Root check --------------------------------------------------------------
[[ $EUID -eq 0 ]] || err "Run as root: sudo bash provision.sh"

# --- Confirm this is a Pi 4/5 -----------------------------------------------
ARCH=$(uname -m)
[[ "$ARCH" == "aarch64" ]] || err "Expected aarch64 (64-bit Pi OS). Got: $ARCH"

# =============================================================================
# STEP 0 — Gather configuration
# =============================================================================
step "Unit configuration"

read -rp "Unit ID (e.g. nw-001): " UNIT_ID
[[ -n "$UNIT_ID" ]] || err "Unit ID required"

read -rp "Site name (e.g. Ghyll Head Farm Campsite): " SITE_NAME
[[ -n "$SITE_NAME" ]] || err "Site name required"

read -rp "What3words (e.g. filled.count.soap — no ///): " W3W
[[ -n "$W3W" ]] || err "What3words required"

read -rp "Latitude (decimal degrees, e.g. 54.6234): " LAT
read -rp "Longitude (decimal degrees, e.g. -3.0521): " LON

read -rp "Enabled sensors (comma-separated: birds,bats,weather): " SENSORS_RAW
SENSORS="${SENSORS_RAW:-birds,bats}"

read -rp "Ingest API key (from Cloudflare KV): " API_KEY
[[ -n "$API_KEY" ]] || err "API key required"

read -rp "Ingest URL (e.g. https://ingest.heardhere.uk): " INGEST_URL
[[ -n "$INGEST_URL" ]] || err "Ingest URL required"

# Confirm
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Unit ID:  $UNIT_ID"
echo "  Site:     $SITE_NAME"
echo "  W3W:      ///$W3W"
echo "  Location: $LAT, $LON"
echo "  Sensors:  $SENSORS"
echo "  Ingest:   $INGEST_URL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
read -rp "Proceed? [y/N]: " CONFIRM
[[ "${CONFIRM,,}" == "y" ]] || err "Aborted"

# =============================================================================
# STEP 1 — System update
# =============================================================================
step "System update"
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    git curl wget python3 python3-pip python3-venv \
    ffmpeg sox libsox-fmt-all \
    sqlite3 libsqlite3-dev \
    at libatlas-base-dev \
    i2c-tools \
    pps-tools gpsd gpsd-clients \
    ufw fail2ban \
    jq

log "System packages installed"

# =============================================================================
# STEP 2 — Enable required interfaces
# =============================================================================
step "Hardware interfaces"

# I2C (for UPS HAT monitoring)
raspi-config nonint do_i2c 0
log "I2C enabled"

# Serial (for 4G HAT AT commands — disable console on serial first)
raspi-config nonint do_serial_hw 0
raspi-config nonint do_serial_cons 1
log "Serial hardware enabled, console disabled"

# SPI (for future sensors)
raspi-config nonint do_spi 0
log "SPI enabled"

# =============================================================================
# STEP 3 — 4G HAT (SIM7600G-H) setup
# =============================================================================
step "4G modem (SIM7600G-H)"

# Install required packages
apt-get install -y -qq ppp usb-modeswitch

# Write PPP peer config
cat > /etc/ppp/peers/4g << 'PPPEOF'
/dev/ttyUSB1
115200
connect "/usr/sbin/chat -v -f /etc/ppp/chat-4g"
noauth
defaultroute
usepeerdns
persist
maxfail 0
holdoff 10
PPPEOF

# Chat script — APN will be filled from unit config
# SMARTY uses Three network APN: three.co.uk
cat > /etc/ppp/chat-4g << 'CHATEOF'
ABORT "BUSY"
ABORT "NO ANSWER"
ABORT "ERROR"
TIMEOUT 30
"" AT
OK ATZ
OK AT+CGDCONT=1,"IP","three.co.uk"
OK ATD*99#
CONNECT ""
CHATEOF

# udev rule to ensure modem USB device is consistent
cat > /etc/udev/rules.d/99-sim7600.rules << 'UDEVEOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="1e0e", ATTRS{idProduct}=="9001", SYMLINK+="sim7600"
UDEVEOF

udevadm control --reload-rules
log "4G modem configured (SMARTY/Three APN)"
warn "Verify APN matches your SIM: edit /etc/ppp/chat-4g if different network"

# =============================================================================
# STEP 4 — Python virtual environment
# =============================================================================
step "Python environment"

VENV_PATH="/home/pi/venv"
python3 -m venv "$VENV_PATH"
"$VENV_PATH/bin/pip" install --quiet --upgrade pip

"$VENV_PATH/bin/pip" install --quiet \
    requests \
    schedule \
    pyserial \
    smbus2 \
    RPi.GPIO \
    psutil \
    astral

log "Python venv created at $VENV_PATH"

# =============================================================================
# STEP 5 — BirdNET-Pi
# =============================================================================
step "BirdNET-Pi"

if [[ -d /home/pi/BirdNET-Pi ]]; then
    warn "BirdNET-Pi already present — skipping clone"
else
    git clone --quiet https://github.com/mcguirepr89/BirdNET-Pi.git /home/pi/BirdNET-Pi
    log "BirdNET-Pi cloned"
fi

# Run BirdNET-Pi installer only if not already installed
if ! systemctl is-active --quiet birdnet_analysis 2>/dev/null; then
    log "Running BirdNET-Pi installer (this takes a while)..."
    cd /home/pi/BirdNET-Pi
    bash -c "HOME=/home/pi USER=pi ./installer.sh" || warn "BirdNET-Pi installer reported issues — check manually"
    cd /
fi

# IMPORTANT: disable BirdNET-Pi auto-start — naturewarden controls it
systemctl disable birdnet_analysis 2>/dev/null || true
systemctl disable birdnet_recording 2>/dev/null || true
log "BirdNET-Pi installed — auto-start disabled (naturewarden owns scheduling)"

# =============================================================================
# STEP 6 — acoupi + BatDetect2
# =============================================================================
step "acoupi (bat detection)"

if ! "$VENV_PATH/bin/pip" show acoupi &>/dev/null; then
    "$VENV_PATH/bin/pip" install --quiet acoupi[batdetect2]
    log "acoupi + BatDetect2 installed"
else
    warn "acoupi already installed — skipping"
fi

# Create acoupi config if not present
ACOUPI_DIR="/home/pi/.acoupi"
mkdir -p "$ACOUPI_DIR/run" "$ACOUPI_DIR/config"

# Disable acoupi systemd service if it exists — naturewarden controls it
systemctl disable acoupi 2>/dev/null || true
log "acoupi installed — auto-start disabled (naturewarden owns scheduling)"

# Fix known stale PID issue (from your TODO list)
cat > /home/pi/.acoupi/clear_pid.sh << 'PIDEOF'
#!/bin/bash
rm -f /home/pi/.acoupi/run/default.pid
PIDEOF
chmod +x /home/pi/.acoupi/clear_pid.sh
log "acoupi stale PID fix script created"

# =============================================================================
# STEP 7 — Directory structure
# =============================================================================
step "Data directories"

DIRS=(
    /home/pi/naturewarden
    /home/pi/sitedata
    /home/pi/storages/recordings/bats
    /home/pi/storages/recordings/birds
    /home/pi/logs
)

for d in "${DIRS[@]}"; do
    mkdir -p "$d"
done

chown -R pi:pi /home/pi/
log "Directory structure created"

# =============================================================================
# STEP 8 — Unit configuration file
# =============================================================================
step "Unit configuration"

cat > /home/pi/sitedata/unit.json << UNITEOF
{
  "unit_id": "$UNIT_ID",
  "site_name": "$SITE_NAME",
  "w3w": "$W3W",
  "lat": $LAT,
  "lon": $LON,
  "enabled_sensors": [$(echo "$SENSORS" | sed 's/,/","/g' | sed 's/^/"/' | sed 's/$/"/')],
  "ingest_url": "$INGEST_URL",
  "api_key": "$API_KEY",
  "version": "2026-06-20[a]",
  "provisioned_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
UNITEOF

chmod 600 /home/pi/sitedata/unit.json
chown pi:pi /home/pi/sitedata/unit.json
log "unit.json written"

# =============================================================================
# STEP 9 — Upload cursor (starts empty)
# =============================================================================
cat > /home/pi/sitedata/upload_cursor.json << 'CURSOREOF'
{
  "birds_last_id": 0,
  "bats_last_id": 0,
  "last_heartbeat": 0
}
CURSOREOF
chown pi:pi /home/pi/sitedata/upload_cursor.json

# =============================================================================
# STEP 10 — NatureWarden (copy from repo)
# =============================================================================
step "NatureWarden service files"

# These are copied from the cloned repo in production.
# During initial provisioning, they're written inline below.
# See pi/naturewarden/ in the heardhere repo.

# Placeholder — naturewarden files are deployed separately
# (see PROVISION.md Step 10 for manual copy during development)
log "NatureWarden directory ready — deploy naturewarden/*.py separately"
warn "Run: cp -r /path/to/repo/pi/naturewarden/* /home/pi/naturewarden/"

# =============================================================================
# STEP 11 — Systemd service for NatureWarden
# =============================================================================
step "Systemd service"

cat > /etc/systemd/system/naturewarden.service << 'SERVICEEOF'
[Unit]
Description=NatureWarden — heardhere.uk scheduler and watchdog
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/naturewarden
ExecStart=/home/pi/venv/bin/python3 /home/pi/naturewarden/main.py
Restart=always
RestartSec=30
StandardOutput=append:/home/pi/logs/naturewarden.log
StandardError=append:/home/pi/logs/naturewarden.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
log "naturewarden.service registered (not started — deploy code first)"

# =============================================================================
# STEP 12 — Log rotation
# =============================================================================
cat > /etc/logrotate.d/naturewarden << 'LOGEOF'
/home/pi/logs/naturewarden.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
LOGEOF

# =============================================================================
# STEP 13 — Basic firewall
# =============================================================================
step "Firewall"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw --force enable
log "UFW enabled — SSH allowed, all other inbound denied"

# =============================================================================
# STEP 14 — Hostname
# =============================================================================
step "Hostname"
hostnamectl set-hostname "$UNIT_ID"
log "Hostname set to $UNIT_ID"

# =============================================================================
# STEP 15 — SSH hardening (minimal)
# =============================================================================
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config || true
sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config || true
warn "SSH password auth disabled — ensure your key is installed before rebooting"
warn "Add your key: ssh-copy-id pi@$UNIT_ID.local BEFORE rebooting"

# =============================================================================
# DONE
# =============================================================================
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Provisioning complete for unit: $UNIT_ID${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "Next steps:"
echo "  1. Copy SSH key if not done: ssh-copy-id pi@$(hostname).local"
echo "  2. Deploy NatureWarden code:"
echo "       cp -r /path/to/repo/pi/naturewarden/* /home/pi/naturewarden/"
echo "  3. Start NatureWarden:"
echo "       sudo systemctl enable --now naturewarden"
echo "  4. Check logs:"
echo "       tail -f /home/pi/logs/naturewarden.log"
echo "  5. Verify 4G connectivity:"
echo "       sudo pon 4g"
echo ""
echo "Unit config: /home/pi/sitedata/unit.json"
echo ""
