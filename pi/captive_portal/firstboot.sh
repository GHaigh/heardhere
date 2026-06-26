#!/usr/bin/env bash
# =============================================================================
# heardhere.uk — firstboot.sh
# Runs at every boot via systemd (heardhere-boot.service).
# Decision tree:
#   - If .configured flag exists → start naturewarden (normal operation)
#   - If not configured → bring up WiFi AP + serve captive portal
# =============================================================================

set -euo pipefail

CONFIGURED_FLAG="/home/pi/sitedata/.configured"
LOG="/home/pi/logs/firstboot.log"

mkdir -p /home/pi/logs /home/pi/sitedata

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [firstboot] $*" | tee -a "$LOG"; }

start_captive_portal() {
    log "Bringing up WiFi access point"

    # Get MAC suffix for SSID
    MAC=$(cat /sys/class/net/wlan0/address 2>/dev/null | tr -d ':' | tail -c 5 | tr '[:lower:]' '[:upper:]')
    SSID="heardhere_${MAC}"
    log "AP SSID: $SSID"

    # Update hostapd config with this unit's SSID
    sed -i "s/^ssid=.*/ssid=${SSID}/" /etc/hostapd/hostapd.conf

    # Stop wpa_supplicant on wlan0 (we're taking over the interface)
    systemctl stop wpa_supplicant 2>/dev/null || true
    ip link set wlan0 down
    sleep 1

    # Assign static IP to wlan0 for AP mode
    ip addr flush dev wlan0
    ip addr add 192.168.4.1/24 dev wlan0
    ip link set wlan0 up

    # Start DHCP server (dnsmasq) and AP (hostapd)
    systemctl start dnsmasq-captive
    systemctl start hostapd

    # Start the captive portal web app
    systemctl start captive-portal

    log "Captive portal active — connect to WiFi '${SSID}' to configure"
}

# ── Decision ──────────────────────────────────────────────────────────────────

log "Boot check starting"

if [[ -f "$CONFIGURED_FLAG" ]]; then
    log "Unit is configured — starting NatureWarden"
    systemctl start naturewarden
    exit 0
fi

log "Unit is NOT yet configured — starting setup mode"
start_captive_portal
