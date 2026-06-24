# hackathon-site — self-hosted event landing page

A single WendyOS app that hosts a hackathon's landing page **on-device** and makes it
easy for people on the venue WiFi to find and use. Three services:

- **`web`** (nginx, port 80) — a clean, dark, mobile-friendly landing page:
  - a "hackathon within the hackathon" **prize banner**
  - **Book a timeslot** + **Question / report a bug** buttons
  - a Wendy Cloud **onboarding guide** (`/onboarding.html`)
  - a scannable **QR page** (`/qr.html`) that encodes the site URL
  - an organizer **feedback inbox** (`/admin.html`)
- **`mdns`** — advertises short `.local` name(s) (e.g. `http://hackathon.local`) so
  people don't have to type an IP. Re-advertises if the device's IP changes.
- **`feedback`** — a small API (behind nginx at `/api/`) that stores question / bug
  submissions to a **persist** volume so they survive reboots.

## Deploy

```sh
wendy init --template hackathon-site --app-id sh.wendy.hackathon.site
cd <rendered-dir>
wendy run --service web      --device <device> -y --detach
wendy run --service mdns     --device <device> -y --detach
wendy run --service feedback --device <device> -y --detach
```

Then share:
- **QR** at `http://<device>/qr.html` (encodes `QR_URL`) — most universal, no typing.
- **`http://<MDNS_ALIASES>.local`** — short name (Safari/iOS; Android/Chrome don't
  resolve `.local`, so give those users the IP / QR).
- Organizer inbox: `http://<device>/admin.html?token=<ADMIN_TOKEN>`.

## Variables

| var | default | meaning |
|-----|---------|---------|
| `APP_ID` | — (required) | app identifier |
| `EVENT_TITLE` | `Robot Dog Hackathon` | hero + tab title |
| `TIMESLOTS_URL` | `https://hackathon.wendy.dev` | "Book a timeslot" link |
| `PRIZE` | `a Jetson Orin Nano with NVMe SSD` | prize banner text |
| `QR_URL` | `http://hackathon.local` | URL the QR encodes (use the device IP for Android) |
| `MDNS_ALIASES` | `hackathon` | comma-separated `.local` names to advertise |
| `ADMIN_TOKEN` | `change-me` | token for the feedback inbox — **change it** |

## Notes

- The page body (framing / goals / hardware / software sections) is **example content** —
  edit `web/index.html` for your event.
- `.local` resolution works great on iPhone/Mac but **not** on Android/Chrome; the QR
  encodes a raw URL so it works everywhere — lead with the QR.
- The feedback backend binds `127.0.0.1` and is reached only via nginx's `/api/` proxy;
  the inbox list requires `ADMIN_TOKEN`.
