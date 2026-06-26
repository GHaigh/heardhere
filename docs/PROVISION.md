# Provisioning a New heardhere.uk Unit

Follow these steps exactly, in order. Target time: 30–45 minutes.

---

## Before you start — checklist

- [ ] Pi 4 (4GB) with fresh Raspberry Pi OS Lite 64-bit (Bookworm) on SD card
- [ ] SIM7600G-H 4G HAT fitted, SIM inserted (SMARTY/Three)
- [ ] UPS HAT fitted, 18650 cells inserted (or PV PI HAT)
- [ ] AudioMoth USB mic connected
- [ ] Pi on your local network via ethernet (for initial setup)
- [ ] Your SSH public key ready
- [ ] Unit ID assigned (e.g. `nw-002`) and row added to D1 units table
- [ ] API key generated for this unit and added to Cloudflare KV

---

## Step 1 — Flash SD card

Use Raspberry Pi Imager. Settings:
- OS: Raspberry Pi OS Lite (64-bit)
- Hostname: `nw-XXX` (your unit ID)
- Username: `pi`
- Enable SSH: yes, paste your public key
- Locale: UK / London

---

## Step 2 — First boot and network

Insert SD, connect ethernet, power on. Wait 60 seconds.

```bash
ssh pi@nw-XXX.local
```

Confirm you're in. If `.local` doesn't resolve, find the IP from your router's DHCP table.

---

## Step 3 — Clone the repo

```bash
git clone https://github.com/GHaigh/heardhere.git
cd heardhere
```

---

## Step 4 — Run provision.sh

```bash
sudo bash pi/provision.sh
```

You will be prompted for:
- Unit ID (e.g. `nw-002`)
- Site name (e.g. `Ghyll Head Farm Campsite`)
- What3words (e.g. `fills.count.soap` — no `///`)
- Latitude and longitude (decimal degrees)
- Enabled sensors (e.g. `birds,bats`)
- Ingest API key (from Cloudflare KV — see below)
- Ingest URL: `https://ingest.heardhere.uk`

**Expected runtime: ~25 minutes.** BirdNET-Pi installation is the slow part.

---

## Step 5 — Deploy NatureWarden code

```bash
cp -r pi/naturewarden/* /home/pi/naturewarden/
chown -R pi:pi /home/pi/naturewarden/
```

---

## Step 6 — Verify acoupi DB column names

The uploader queries acoupi's SQLite DB. Column names may vary by version.
Check them before enabling bats:

```bash
sqlite3 /home/pi/storages/metadata.db ".schema detection_predictions"
```

Update `uploader.py` column names if they differ from the defaults.

---

## Step 7 — Test upload (dry run)

```bash
sudo -u pi /home/pi/venv/bin/python3 /home/pi/naturewarden/uploader.py
```

Check for errors. A successful run ends with `Upload successful — cursor updated`
or `No new data and heartbeat is recent — skipping upload`.

---

## Step 8 — Start NatureWarden

```bash
sudo systemctl enable --now naturewarden
```

Check it's running:

```bash
sudo systemctl status naturewarden
tail -f /home/pi/logs/naturewarden.log
```

You should see the schedule logged: dawn/dusk times, next bird and bat sessions.

---

## Step 9 — Test 4G connectivity

With SIM inserted:

```bash
sudo pon 4g
```

Wait 15 seconds, then:

```bash
curl --interface ppp0 https://ingest.heardhere.uk/api/health
```

Should return `ok`. If not, check APN in `/etc/ppp/chat-4g` — edit to match
your SIM's APN if not using SMARTY/Three.

---

## Step 10 — Move to solar/4G (remove ethernet)

Once 4G is confirmed working:
1. Unplug ethernet
2. Power cycle
3. Check admin dashboard — unit should appear with green heartbeat within 15 min

---

## Adding an API key to Cloudflare KV

For each new unit, generate a key and store its SHA-256 hash:

```bash
# Generate key
KEY=$(openssl rand -hex 32)
echo "API key for nw-XXX: $KEY"   # Save this — goes into unit.json

# Get SHA-256 hash
HASH=$(echo -n "$KEY" | sha256sum | cut -d' ' -f1)

# Add to KV via wrangler
wrangler kv:key put --namespace-id=YOUR_KV_ID "unit:nw-XXX:key_hash" "$HASH"
```

---

## Troubleshooting

| Symptom | Check |
|---|---|
| NatureWarden won't start | `journalctl -u naturewarden -n 50` |
| No detections uploading | `tail /home/pi/logs/naturewarden.log` and check cursor.json |
| 4G not connecting | `sudo pppd call 4g debug` to see AT exchange |
| acoupi not starting | Check `/home/pi/logs/acoupi.log`, verify AudioMoth connected |
| BirdNET-Pi stalled | `systemctl status birdnet_analysis` — naturewarden will restart it |

---

## Deployment record

Each unit should have a row in the `units` D1 table and a record in this file:

| Unit | Site | Deployed | W3W | Notes |
|---|---|---|---|---|
| nw-001 | Test — Threlkeld | 2026-06-20 | TBD | Dev unit |
