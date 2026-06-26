# heardhere.uk 🎙️🦇🐦

> *Every place has a voice.*

A solar-powered, 4G-connected nature monitoring station. Bird detection by day,
bat detection by night — with a per-site public website and AI naturalist commentary.

Built on Raspberry Pi 4, BirdNET-Pi, BatDetect2/acoupi, and Cloudflare's edge platform.

---

## Repository structure

```
heardhere/
├── pi/                         # On-device code (deployed to each Pi unit)
│   ├── provision.sh            # Golden provisioning script — run once on fresh SD
│   └── naturewarden/
│       ├── main.py             # Scheduler + watchdog (the brain)
│       └── uploader.py         # Batches detections → Cloudflare ingest
│
├── workers/                    # Cloudflare Workers
│   ├── ingest/index.js         # Receives data from Pi units
│   ├── api/index.js            # Serves data to public sites
│   ├── update/index.js         # OTA update check + delivery
│   └── schema.sql              # D1 database schema
│
├── site/                       # Public site template (config-driven)
│   ├── index.html
│   ├── birds.html
│   ├── bats.html
│   └── ...
│
├── admin/                      # Fleet dashboard (private, password-protected)
│   └── index.html
│
├── docs/
│   ├── PROVISION.md            # Step-by-step unit deployment guide
│   ├── BUILD.md                # Hardware assembly guide
│   └── DEPLOY.md               # Per-site setup checklist
│
└── wrangler.toml               # Cloudflare Workers config
```

---

## Quick start (new unit)

See [docs/PROVISION.md](docs/PROVISION.md) for the full step-by-step guide.

```bash
# On a fresh Pi OS Lite 64-bit image:
git clone https://github.com/GHaigh/heardhere.git
cd heardhere
sudo bash pi/provision.sh
```

---

## Architecture

```
[Pi Unit — solar/4G]
  ├── BirdNET-Pi     (dawn window)
  ├── acoupi+BD2     (dusk window)
  ├── naturewarden   (scheduler + watchdog + uploader)
  └── unit.json      (site config, API key)
        │ HTTPS / JSON (4G)
        ▼
[Cloudflare]
  ├── Workers (ingest, api, update)
  ├── D1      (detections, unit health)
  ├── R2      (audio clips, backups)
  └── KV      (API key hashes, update manifests)
        │
        ▼
[heardhere.uk public sites]
  └── {sitename}.heardhere.uk — per-unit, config-driven
```

---

## Hardware (v1 unit)

| Component | Purpose |
|---|---|
| Raspberry Pi 4 4GB | Compute |
| Waveshare SIM7600G-H 4G HAT | 4G + GNSS |
| PV PI HAT / Waveshare UPS HAT (B) | Power management |
| 30W 12V solar panel | Generation |
| 20Ah LiFePO4 battery | Storage |
| AudioMoth (384kHz USB) | Bat microphone |
| USB omnidirectional mic | Bird microphone |
| IP66 enclosure | Weather protection |

See [docs/BUILD.md](docs/BUILD.md) for assembly guide.

---

## Pricing model

| Tier | Description | Price |
|---|---|---|
| Own it | Customer buys a unit outright | £450–550 + £10/mo |
| Rent it | Seasonal rental (schools, conservation) | £80–120/month all-in |
| Partner | Conservation orgs (subsidised) | £20–35/month subscription |

---

## Naming / domain

Domain: **heardhere.uk**
Tagline: *Every place has a voice.*

Customer sites: `{sitename}.heardhere.uk` or custom domain (upsell).

---

## Development status

- [x] Architecture designed
- [x] provision.sh — first draft
- [x] naturewarden/main.py — scheduler + watchdog
- [x] naturewarden/uploader.py — ingest pipeline
- [x] ingest Worker — first draft
- [x] D1 schema
- [ ] api Worker
- [ ] update Worker
- [ ] Public site template (birds.html)
- [ ] Admin fleet dashboard
- [ ] OTA updater.sh
- [ ] Hardware v1 assembled and field-tested

---

## Licence

MIT for platform code. BirdNET-Pi and acoupi retain their own licences.
