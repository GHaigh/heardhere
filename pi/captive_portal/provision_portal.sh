#!/usr/bin/env bash
# =============================================================================
# heardhere.uk — provision_portal.sh
# Additional provisioning for captive portal capability.
# Run AFTER provision.sh on the same device.
# (Will be merged into provision.sh for the final image)
# =============================================================================

set -euo pipefail

log()  { echo -e "\033[0;32m[portal]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }

# Install AP + DHCP deps
log "Installing hostapd and dnsmasq"
apt-get install -y -qq hostapd dnsmasq

# Stop and disable both from auto-starting (firstboot.sh controls them)
systemctl stop hostapd dnsmasq 2>/dev/null || true
systemctl disable hostapd dnsmasq 2>/dev/null || true
systemctl unmask hostapd 2>/dev/null || true

# Install Flask for the portal web app
/home/pi/venv/bin/pip install --quiet flask netifaces

# Write hostapd config
cat > /etc/hostapd/hostapd.conf << 'HOSTAPDEOF'
interface=wlan0
driver=nl80211
ssid=heardhere_0000
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=0
country_code=GB
HOSTAPDEOF

# Point hostapd at our config
echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' > /etc/default/hostapd

# Write captive dnsmasq config
cat > /etc/dnsmasq-captive.conf << 'DNSEOF'
interface=wlan0
bind-interfaces
dhcp-range=192.168.4.10,192.168.4.50,255.255.255.0,24h
dhcp-option=3,192.168.4.1
dhcp-option=6,192.168.4.1
address=/#/192.168.4.1
port=5353
DNSEOF

# systemd: heardhere-boot (mode selector)
cat > /etc/systemd/system/heardhere-boot.service << 'BOOTEOF'
[Unit]
Description=heardhere boot mode selector
After=network.target
DefaultDependencies=no

[Service]
Type=oneshot
ExecStart=/home/pi/heardhere/pi/captive_portal/firstboot.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
BOOTEOF

# systemd: captive portal Flask app
cat > /etc/systemd/system/captive-portal.service << 'PORTALEOF'
[Unit]
Description=heardhere captive portal setup UI
After=network.target

[Service]
Type=simple
User=root
ExecStart=/home/pi/venv/bin/python3 /home/pi/heardhere/pi/captive_portal/captive_portal.py
Restart=on-failure
StandardOutput=append:/home/pi/logs/captive_portal.log
StandardError=append:/home/pi/logs/captive_portal.log

[Install]
WantedBy=multi-user.target
PORTALEOF

# systemd: captive dnsmasq
cat > /etc/systemd/system/dnsmasq-captive.service << 'DNSSVCEOF'
[Unit]
Description=dnsmasq DHCP for heardhere captive portal
After=network.target

[Service]
Type=simple
ExecStart=/usr/sbin/dnsmasq --no-daemon --conf-file=/etc/dnsmasq-captive.conf
Restart=on-failure

[Install]
WantedBy=multi-user.target
DNSSVCEOF

# Make firstboot.sh executable
chmod +x /home/pi/heardhere/pi/captive_portal/firstboot.sh

# Enable the boot selector (this is the only thing that auto-starts)
# naturewarden and captive-portal are started BY firstboot.sh, not directly
systemctl daemon-reload
systemctl enable heardhere-boot

# Disable naturewarden auto-start — heardhere-boot decides when to start it
systemctl disable naturewarden 2>/dev/null || true

log "Captive portal provisioned"
log "On next boot (without .configured flag), unit will broadcast heardhere_XXXX"
log ""
log "To test: rm /home/pi/sitedata/.configured && sudo reboot"
log "To skip portal on this boot: touch /home/pi/sitedata/.configured && sudo systemctl start naturewarden"
