#!/usr/bin/env python3
"""
heardhere.uk — Captive Portal
Served on 192.168.4.1 during first-boot AP mode.
User connects to heardhere_XXXX WiFi, visits any URL,
fills in unit config, hits Save — Pi writes unit.json and reboots.

Requires: flask, netifaces
Runs as root (needs to write config files and trigger reboot).
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for

app = Flask(__name__)

CONFIG_PATH = Path("/home/pi/sitedata/unit.json")
CURSOR_PATH = Path("/home/pi/sitedata/upload_cursor.json")
CONFIGURED_FLAG = Path("/home/pi/sitedata/.configured")

# ── Common APN presets ────────────────────────────────────────────────────────
APN_PRESETS = {
    "smarty":    {"name": "SMARTY (Three)",       "apn": "three.co.uk",    "user": "", "pass": ""},
    "giffgaff":  {"name": "giffgaff (O2)",        "apn": "giffgaff.com",   "user": "giffgaff", "pass": "password"},
    "ee":        {"name": "EE",                    "apn": "everywhere",     "user": "eesecure", "pass": "secure"},
    "vodafone":  {"name": "Vodafone",              "apn": "internet",       "user": "web", "pass": "web"},
    "o2":        {"name": "O2",                    "apn": "mobile.o2.co.uk","user": "o2web", "pass": "password"},
    "1nce":      {"name": "1NCE (IoT)",            "apn": "iot.1nce.net",   "user": "", "pass": ""},
    "custom":    {"name": "Custom / Other",        "apn": "",               "user": "", "pass": ""},
}

SENSOR_OPTIONS = [
    {"id": "birds",   "label": "Bird detection (BirdNET-Pi)",     "icon": "🐦"},
    {"id": "bats",    "label": "Bat detection (BatDetect2)",       "icon": "🦇"},
    {"id": "weather", "label": "Weather station (RTL-SDR/USB)",    "icon": "🌦️"},
    {"id": "camera",  "label": "Wildlife camera",                  "icon": "📷"},
]

CONNECTIVITY_OPTIONS = [
    {"id": "4g",   "label": "4G (mobile data — off-grid)"},
    {"id": "wifi", "label": "WiFi (fixed site with network)"},
    {"id": "poe",  "label": "PoE / Ethernet (fixed site with cable)"},
]

# ── HTML template (self-contained, no external deps) ─────────────────────────
PORTAL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>heardhere — Setup</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #1a2e1a;
      color: #e8f4e8;
      min-height: 100vh;
      padding: 20px;
    }
    .container { max-width: 480px; margin: 0 auto; }
    .logo {
      text-align: center;
      padding: 32px 0 24px;
    }
    .logo h1 { font-size: 28px; color: #7bc67e; letter-spacing: -0.5px; }
    .logo p  { color: #8aaa8a; font-size: 14px; margin-top: 4px; }

    .card {
      background: #253525;
      border-radius: 12px;
      padding: 24px;
      margin-bottom: 16px;
      border: 1px solid #2d6a4f30;
    }
    .card h2 {
      font-size: 15px;
      font-weight: 600;
      color: #7bc67e;
      margin-bottom: 16px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    label { display: block; font-size: 14px; color: #aacaaa; margin-bottom: 4px; }
    input[type="text"], input[type="password"], select {
      width: 100%;
      padding: 10px 12px;
      background: #1a2e1a;
      border: 1px solid #3a5a3a;
      border-radius: 8px;
      color: #e8f4e8;
      font-size: 15px;
      margin-bottom: 14px;
    }
    input:focus, select:focus {
      outline: none;
      border-color: #7bc67e;
    }
    select option { background: #1a2e1a; }

    .sensor-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .sensor-item {
      background: #1a2e1a;
      border: 1px solid #3a5a3a;
      border-radius: 8px;
      padding: 10px;
      cursor: pointer;
    }
    .sensor-item input[type="checkbox"] { display: none; }
    .sensor-item.checked {
      border-color: #7bc67e;
      background: #2d6a4f22;
    }
    .sensor-item .icon { font-size: 20px; }
    .sensor-item .label { font-size: 12px; color: #aacaaa; margin-top: 4px; line-height: 1.3; }

    .conn-options { display: flex; flex-direction: column; gap: 8px; }
    .conn-item {
      display: flex;
      align-items: center;
      gap: 10px;
      background: #1a2e1a;
      border: 1px solid #3a5a3a;
      border-radius: 8px;
      padding: 10px 12px;
      cursor: pointer;
    }
    .conn-item.selected { border-color: #7bc67e; background: #2d6a4f22; }
    .conn-item input[type="radio"] { display: none; }
    .conn-item .dot {
      width: 16px; height: 16px;
      border-radius: 50%;
      border: 2px solid #3a5a3a;
      flex-shrink: 0;
    }
    .conn-item.selected .dot { border-color: #7bc67e; background: #7bc67e; }

    #apn-section, #wifi-section { display: none; }
    #apn-section.visible, #wifi-section.visible { display: block; }

    .hint { font-size: 12px; color: #6a8a6a; margin-top: -10px; margin-bottom: 14px; }

    button[type="submit"] {
      width: 100%;
      padding: 14px;
      background: #2d6a4f;
      color: #e8f4e8;
      border: none;
      border-radius: 10px;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      margin-top: 8px;
    }
    button[type="submit"]:active { background: #1a4a33; }

    .error {
      background: #4a1a1a;
      border: 1px solid #8a3a3a;
      color: #f4a0a0;
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 16px;
      font-size: 14px;
    }
    .success {
      background: #1a3a1a;
      border: 1px solid #3a6a3a;
      color: #90c890;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 16px;
      font-size: 14px;
      text-align: center;
    }
    .unit-id-badge {
      text-align: center;
      color: #6a8a6a;
      font-size: 12px;
      margin-bottom: 24px;
    }
    .unit-id-badge code {
      background: #253525;
      padding: 2px 8px;
      border-radius: 4px;
      font-family: monospace;
    }
  </style>
</head>
<body>
<div class="container">
  <div class="logo">
    <h1>heardhere 🎙️</h1>
    <p>Every place has a voice.</p>
  </div>

  {% if unit_id %}
  <div class="unit-id-badge">Unit <code>{{ unit_id }}</code></div>
  {% endif %}

  {% if error %}
  <div class="error">⚠️ {{ error }}</div>
  {% endif %}

  {% if success %}
  <div class="success">
    ✅ {{ success }}<br>
    <small>This page will close. The unit is restarting…</small>
  </div>
  {% else %}

  <form method="POST" action="/save" id="setup-form">

    <!-- Site details -->
    <div class="card">
      <h2>📍 Site Details</h2>
      <label for="site_name">Site name</label>
      <input type="text" id="site_name" name="site_name"
             placeholder="e.g. Burns Farm Campsite"
             value="{{ form.site_name or '' }}" required>

      <label for="w3w">what3words location</label>
      <input type="text" id="w3w" name="w3w"
             placeholder="e.g. filled.count.soap (no ///)"
             value="{{ form.w3w or '' }}" required>
      <div class="hint">Find yours at <strong>what3words.com</strong> — enter without ///</div>

      <label for="ingest_url">Platform URL</label>
      <input type="text" id="ingest_url" name="ingest_url"
             value="{{ form.ingest_url or 'https://ingest.heardhere.uk' }}" required>

      <label for="api_key">Unit API key</label>
      <input type="password" id="api_key" name="api_key"
             placeholder="Provided with your unit"
             value="{{ form.api_key or '' }}" required>
    </div>

    <!-- Sensors -->
    <div class="card">
      <h2>🔌 Fitted Sensors</h2>
      <div class="sensor-grid">
        {% for s in sensors %}
        <label class="sensor-item {% if s.id in (form.sensors or ['birds','bats']) %}checked{% endif %}"
               id="label-{{ s.id }}">
          <input type="checkbox" name="sensors" value="{{ s.id }}"
                 {% if s.id in (form.sensors or ['birds','bats']) %}checked{% endif %}
                 onchange="toggleSensor('{{ s.id }}')">
          <div class="icon">{{ s.icon }}</div>
          <div class="label">{{ s.label }}</div>
        </label>
        {% endfor %}
      </div>
    </div>

    <!-- Connectivity -->
    <div class="card">
      <h2>📡 Connectivity</h2>
      <div class="conn-options">
        {% for c in connectivity %}
        <label class="conn-item {% if (form.connectivity or '4g') == c.id %}selected{% endif %}"
               id="conn-{{ c.id }}">
          <input type="radio" name="connectivity" value="{{ c.id }}"
                 {% if (form.connectivity or '4g') == c.id %}checked{% endif %}
                 onchange="switchConn('{{ c.id }}')">
          <div class="dot"></div>
          <span>{{ c.label }}</span>
        </label>
        {% endfor %}
      </div>

      <!-- 4G APN section -->
      <div id="apn-section" class="{% if (form.connectivity or '4g') == '4g' %}visible{% endif %}"
           style="margin-top:14px">
        <label for="sim_provider">SIM provider</label>
        <select id="sim_provider" name="sim_provider" onchange="apnPreset()">
          {% for key, p in apn_presets.items() %}
          <option value="{{ key }}"
            {% if (form.sim_provider or 'smarty') == key %}selected{% endif %}>
            {{ p.name }}
          </option>
          {% endfor %}
        </select>

        <label for="apn">APN</label>
        <input type="text" id="apn" name="apn"
               value="{{ form.apn or 'three.co.uk' }}">

        <label for="apn_user">APN username <span style="color:#6a8a6a">(often blank)</span></label>
        <input type="text" id="apn_user" name="apn_user"
               value="{{ form.apn_user or '' }}">

        <label for="apn_pass">APN password <span style="color:#6a8a6a">(often blank)</span></label>
        <input type="password" id="apn_pass" name="apn_pass"
               value="{{ form.apn_pass or '' }}">
      </div>

      <!-- WiFi section -->
      <div id="wifi-section" class="{% if form.connectivity == 'wifi' %}visible{% endif %}"
           style="margin-top:14px">
        <label for="wifi_ssid">WiFi network name</label>
        <input type="text" id="wifi_ssid" name="wifi_ssid"
               value="{{ form.wifi_ssid or '' }}">

        <label for="wifi_pass">WiFi password</label>
        <input type="password" id="wifi_pass" name="wifi_pass"
               value="{{ form.wifi_pass or '' }}">
      </div>
    </div>

    <button type="submit">Save & Start Unit →</button>
  </form>
  {% endif %}
</div>

<script>
// APN presets
const presets = {{ apn_presets_json }};
function apnPreset() {
  const key = document.getElementById('sim_provider').value;
  const p = presets[key];
  document.getElementById('apn').value = p.apn;
  document.getElementById('apn_user').value = p.user;
  document.getElementById('apn_pass').value = p.pass;
}

// Connectivity switch
function switchConn(id) {
  document.querySelectorAll('.conn-item').forEach(el => el.classList.remove('selected'));
  document.getElementById('conn-' + id).classList.add('selected');
  document.getElementById('apn-section').classList.toggle('visible', id === '4g');
  document.getElementById('wifi-section').classList.toggle('visible', id === 'wifi');
}

// Sensor toggle
function toggleSensor(id) {
  const label = document.getElementById('label-' + id);
  const cb = label.querySelector('input');
  label.classList.toggle('checked', cb.checked);
}
</script>
</body>
</html>
"""

# ── Routes ────────────────────────────────────────────────────────────────────

def get_unit_id():
    """Derive unit ID from wlan0 MAC address last 4 digits."""
    try:
        mac = open("/sys/class/net/wlan0/address").read().strip()
        suffix = mac.replace(":", "")[-4:].upper()
        return f"nw-{suffix}"
    except Exception:
        return "nw-0000"


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def index(path):
    """Redirect everything to /setup (captive portal behaviour)."""
    return redirect("/setup")


@app.route("/setup")
def setup():
    return render_template_string(
        PORTAL_HTML,
        error=None,
        success=None,
        form={},
        unit_id=get_unit_id(),
        sensors=SENSOR_OPTIONS,
        connectivity=CONNECTIVITY_OPTIONS,
        apn_presets=APN_PRESETS,
        apn_presets_json=json.dumps(
            {k: {"apn": v["apn"], "user": v["user"], "pass": v["pass"]}
             for k, v in APN_PRESETS.items()}
        ),
    )


@app.route("/save", methods=["POST"])
def save():
    form = request.form
    errors = []

    site_name    = form.get("site_name", "").strip()
    w3w          = form.get("w3w", "").strip().lstrip("/")
    ingest_url   = form.get("ingest_url", "https://ingest.heardhere.uk").strip()
    api_key      = form.get("api_key", "").strip()
    sensors      = form.getlist("sensors") or ["birds"]
    connectivity = form.get("connectivity", "4g")

    # Validation
    if not site_name:
        errors.append("Site name is required")
    if not w3w or len(w3w.split(".")) != 3:
        errors.append("what3words must be three words separated by dots (e.g. filled.count.soap)")
    if not api_key:
        errors.append("API key is required")
    if not sensors:
        errors.append("Select at least one sensor")

    if errors:
        return render_template_string(
            PORTAL_HTML,
            error=" · ".join(errors),
            success=None,
            form={**form, "sensors": sensors},
            unit_id=get_unit_id(),
            sensors=SENSOR_OPTIONS,
            connectivity=CONNECTIVITY_OPTIONS,
            apn_presets=APN_PRESETS,
            apn_presets_json=json.dumps(
                {k: {"apn": v["apn"], "user": v["user"], "pass": v["pass"]}
                 for k, v in APN_PRESETS.items()}
            ),
        )

    unit_id = get_unit_id()

    # Build unit.json
    config = {
        "unit_id": unit_id,
        "site_name": site_name,
        "w3w": w3w,
        "lat": None,   # Will be populated from GNSS once 4G HAT is active
        "lon": None,
        "enabled_sensors": sensors,
        "connectivity": connectivity,
        "ingest_url": ingest_url,
        "api_key": api_key,
        "version": "2026-06-20[a]",
        "provisioned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Write connectivity-specific config
    if connectivity == "4g":
        config["apn"] = {
            "provider": form.get("sim_provider", "custom"),
            "apn":      form.get("apn", ""),
            "user":     form.get("apn_user", ""),
            "password": form.get("apn_pass", ""),
        }
        write_ppp_config(config["apn"])

    elif connectivity == "wifi":
        config["wifi"] = {
            "ssid": form.get("wifi_ssid", ""),
            "password": form.get("wifi_pass", ""),
        }
        write_wifi_config(config["wifi"])

    # Save config
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    CONFIG_PATH.chmod(0o600)

    # Write cursor
    CURSOR_PATH.write_text(json.dumps({
        "birds_last_id": 0,
        "bats_last_id": 0,
        "last_heartbeat": 0,
    }, indent=2))

    # Mark as configured
    CONFIGURED_FLAG.touch()

    # Schedule reboot in 3 seconds (gives Flask time to return the response)
    subprocess.Popen(["bash", "-c", "sleep 3 && systemctl stop captive-portal && systemctl start naturewarden"])

    return render_template_string(
        PORTAL_HTML,
        error=None,
        success=f"Unit '{site_name}' configured. Starting up…",
        form={},
        unit_id=unit_id,
        sensors=SENSOR_OPTIONS,
        connectivity=CONNECTIVITY_OPTIONS,
        apn_presets=APN_PRESETS,
        apn_presets_json="{}",
    )


# ── Config writers ─────────────────────────────────────────────────────────────

def write_ppp_config(apn_config: dict):
    """Write PPP chat script with the provided APN details."""
    chat = f"""ABORT "BUSY"
ABORT "NO ANSWER"
ABORT "ERROR"
TIMEOUT 30
"" AT
OK ATZ
OK AT+CGDCONT=1,"IP","{apn_config['apn']}"
OK ATD*99#
CONNECT ""
"""
    Path("/etc/ppp/chat-4g").write_text(chat)

    peer = f"""/dev/ttyUSB1
115200
connect "/usr/sbin/chat -v -f /etc/ppp/chat-4g"
{"user " + apn_config['user'] if apn_config['user'] else "noauth"}
{"password " + apn_config['password'] if apn_config['password'] else ""}
defaultroute
usepeerdns
persist
maxfail 0
holdoff 10
"""
    Path("/etc/ppp/peers/4g").write_text(peer)


def write_wifi_config(wifi_config: dict):
    """Write wpa_supplicant config for the site WiFi."""
    wpa = f"""ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=GB

network={{
    ssid="{wifi_config['ssid']}"
    psk="{wifi_config['password']}"
    key_mgmt=WPA-PSK
}}
"""
    Path("/etc/wpa_supplicant/wpa_supplicant.conf").write_text(wpa)
    Path("/etc/wpa_supplicant/wpa_supplicant.conf").chmod(0o600)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Runs on port 80, accessible at 192.168.4.1
    # Must be run as root (port 80 requires it, and we write system configs)
    app.run(host="0.0.0.0", port=80, debug=False)
