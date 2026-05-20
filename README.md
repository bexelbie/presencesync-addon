<p align="center">
  <img src="logo.png" alt="PresenceSync" width="160" height="160"/>
</p>

<h1 align="center">PresenceSync</h1>

<p align="center">
  <em>Apple Find My, all of it, in Home Assistant.</em><br>
  <em>AirTags. iPhones. iPads. Macs. Watches. Family devices.</em><br>
  <em>One add-on. No Mac running in the background after setup. No SSH. No YAML.</em>
</p>

<hr>

## What this does

PresenceSync is a Home Assistant add-on that talks to Apple's Find My infrastructure **directly from inside HA** and publishes everything it sees as native `device_tracker.*` entities over MQTT auto-discovery.

It covers two complementary sources, each switchable on or off from the dashboard:

| Source | What it covers | Backend |
| --- | --- | --- |
| **AirTags** | AirTags, AirPods, Find-My-tagged accessories | `findmy.py` → `gateway.icloud.com` (anisette-signed) |
| **iDevices** | Your iPhone / iPad / Mac / Watch, plus everyone in your Family Sharing group | `pyicloud` → `fmipmobile.icloud.com` |

Sessions persist across restarts and add-on updates. 2FA only re-prompts if Apple invalidates the session server-side — typically once every few weeks at most.

## Repository contents

Two Home Assistant apps in one repository:

| Add-on | What it is | User-facing? |
| --- | --- | --- |
| **PresenceSync** | Main app. FastAPI dashboard in your HA sidebar. Drives `findmy.py` and `pyicloud`, publishes to MQTT. | Yes — setup wizard & status dashboard |
| **PresenceSync Anisette** | Wraps [`dadoum/anisette-v3-server`](https://github.com/Dadoum/anisette-v3-server). Generates the Apple-compatible signed headers `gateway.icloud.com` demands. | No — internal only |

## Quick start

### 1. Add this repository to your HA Apps Store

Settings → **Apps** → **Apps Store** → top-right **⋯** → **Repositories** → paste:

```
https://github.com/PrayerfulDrop/presencesync-addon
```

### 2. Install both apps

Both appear in the store after the repo is added. Install in this order:

- **PresenceSync Anisette** → Install → Start → toggle **Start on boot**
- **PresenceSync** → Install → Start → toggle **Start on boot**

First start of PresenceSync pulls Python dependencies (`findmy`, `pyicloud`, `paho-mqtt`, `fastapi`). Give it 1–2 minutes.

### 3. Open the PresenceSync dashboard

Click **PresenceSync** in the HA left sidebar. You'll see a status dashboard with five health cards — **Apple**, **iCloud**, **MQTT**, **Anisette**, **Bundle** — plus a Tracked items table.

Two source toggles sit next to the table heading: **AirTags** and **iDevices**. Disabling a source greys out its card and tells the coordinator to skip it.

### 4. Set up the source(s) you want

#### AirTags

The AirTag side needs a one-time key bundle extracted from a Mac that's signed in to your Apple ID. **The Mac is only needed for this step** — after the bundle is uploaded, you can shut the Mac down or re-enable its security settings.

➡ **Full Mac walkthrough: [`docs/mac-setup.md`](docs/mac-setup.md)**

TL;DR (assumes SIP and AMFI are temporarily disabled — see the walkthrough):

```bash
git clone https://github.com/PrayerfulDrop/findmy-key-extractor.git ~/src/findmy-key-extractor
cd ~/src/findmy-key-extractor
git checkout x86_64-port

sudo ./extract.sh                # FMIP + FMF + LocalStorage keys
sudo ./extract_beaconstore.sh    # BeaconStore key
./bundle.sh                      # → presencesync-bundle.tar.gz
```

Upload the resulting `.tar.gz` in the PresenceSync dashboard's **Upload extractor bundle** section. The Bundle card flips to green with `N items loaded`. Re-enable SIP + clear the AMFI boot-arg on the Mac if you want.

#### iDevices

Click **Log in** in the **Apple login** section, enter your iCloud email + password.

A 6-digit code arrives as a notification ("Apple ID Sign-In Requested") on your trusted Apple devices — tap **Allow**, the code displays, enter it in PresenceSync and submit.

If the notification doesn't arrive within a minute (Apple intermittently suppresses push for third-party tools), use the manual fallback the UI surfaces:

1. iPhone → **Airplane Mode on**
2. **Settings → [your name] → Sign-In & Security → Get Verification Code**
3. Enter the displayed code in PresenceSync → Submit
4. iPhone → **Airplane Mode off**

(PresenceSync forces IPv4-only DNS for Apple's auth endpoints, which fixes the push-notification reliability — credit to the iCloud3 maintainers for figuring this out. Manual fallback is just a safety net.)

### 5. Done

Within ~60 seconds, every item that has reported a recent location appears in HA at **Settings → Devices & Services → MQTT**, ready to use in Lovelace, automations, history, the map, etc.

## What you get in Home Assistant

For each tracked item, PresenceSync publishes:

- `device_tracker.presencesync_<slug>` — GPS-source tracker with `latitude`, `longitude`, `gps_accuracy`, `last_seen`. HA's zone resolver handles `home` / `not_home` / your custom zones automatically.
- A device card grouping the entity, with manufacturer **Apple** and the real model (e.g. `iPhone17,2`, `Macmini8,1`, `AirPods Pro (2nd generation)`).
- For battery-reporting devices: a `sensor.<slug>_battery` with the level as a percentage.

The dashboard's Tracked items table shows the live state of each item: name, model, home/away (computed against your `zone.home` lat/lon/radius from HA), last-seen relative time, GPS accuracy.

## How it actually works

```
┌──────────────────────────────────────────────────────────────────────┐
│ Home Assistant (Mac not needed at runtime)                           │
│                                                                      │
│  ┌──────────────────────┐                                            │
│  │ PresenceSync add-on  │                                            │
│  │  • FastAPI dashboard │   anisette-signed                          │
│  │  • findmy.py ────────┼──▶ gateway.icloud.com  ──▶ AirTags         │
│  │  • pyicloud  ────────┼──▶ fmipmobile.icloud   ──▶ iDevices/Family │
│  │  • MQTT publish      │                                            │
│  └──────────┬───────────┘                                            │
│             │                                                        │
│             ▼  MQTT auto-discovery                                   │
│  ┌──────────────────────┐                                            │
│  │ Mosquitto add-on     │──▶ device_tracker.presencesync_swim_bag    │
│  │                      │    device_tracker.presencesync_aarons_iph… │
│  └──────────────────────┘    sensor.presencesync_aarons_iph…_battery │
│                                                                      │
│  ┌──────────────────────┐                                            │
│  │ Anisette add-on      │  ◀── findmy.py asks here for the           │
│  │ (dadoum/v3-server)   │      Apple-compatible signed headers       │
│  └──────────────────────┘      Apple's gateway demands               │
└──────────────────────────────────────────────────────────────────────┘
```

Where state lives across restarts:

- `/data/presencesync.json` — MQTT, home location, source toggles, Apple ID + password
- `/data/apple_state.pickle` — findmy.py session (auth tokens, account state)
- `/data/pyicloud-cookies/` — pyicloud session cookies (Apple-issued trust)
- `/data/bundle/` — extracted FindMy key bundle

All four persist across HA restarts and add-on updates. The user actions ("Log in", "Upload bundle", "Submit 2FA") are one-time per Apple-server-side invalidation.

## Configuration reference

Add-on options (Settings → Add-ons → PresenceSync → Configuration):

```yaml
log_level: info             # debug | info | warning | error
poll_interval_seconds: 60   # how often we hit Apple's endpoints
anisette_url: http://homeassistant.local:6969
mqtt_discovery_prefix: homeassistant
state_prefix: presencesync
```

Defaults are usually right. The MQTT broker, home location, and Apple credentials are configured via the web UI, not the add-on options.

## Troubleshooting

**Tracked items table is empty after login**: First poll cycle takes 60–120 s on a fresh login because `findmy.py` has to align rolling keys for each accessory. After the first cycle, subsequent polls are sub-second.

**`iCloud (devices)` keeps showing `needs_login` after restarts**: Run a fresh login once; from then on the saved cookies auto-resume. v0.2.14+ does this automatically on startup if username/password are saved.

**`Apple (AirTags)` healthy but no items**: Make sure the **Bundle** card is also green. Without the bundle the addon has no per-AirTag keys to query the gateway with.

**Names showing as `?`** for one or two devices: those don't have a `BeaconNamingRecord` in `searchpartyd/` (typically the case for one iPhone in some setups). Functional but cosmetically incomplete; a future bundle.sh update will pull names from the FMIP cache to fix this.

**Add-on log full of "Unclosed client session"**: cosmetic; leftover aiohttp sessions from timed-out login attempts. Harmless.

## Requirements

- Home Assistant **OS** or **Supervised**, 2024.1+
- An MQTT broker reachable from HA (the Mosquitto add-on is the easy default)
- For the **AirTags** source: one Mac (Apple Silicon **or** Intel) for the one-time key extraction — SIP + AMFI temporarily disabled, then re-enabled after
- For the **iDevices** source: just your Apple ID + a trusted Apple device for 2FA. No Mac required.
- Both sources are independently toggleable; you can run AirTags only, iDevices only, or both.

## Security Considerations
- this is an unofficial Apple API workaround
- the extractor is a high-trust operation
- the bundle should be treated like a password/location credential
- HA backups may contain sensitive state
- users should re-enable SIP/AMFI and delete temporary files after extraction
- use at your own risk / inspect the code if concerned

## Licensing

- This repository's wrapper code: **MIT**
- `anisette-v3-server`: **GPL-3.0** ([Dadoum/anisette-v3-server](https://github.com/Dadoum/anisette-v3-server))
- `findmy.py`: **MIT** ([malmeloo/FindMy.py](https://github.com/malmeloo/FindMy.py))
- `pyicloud`: **MIT** ([picklepete/pyicloud](https://github.com/picklepete/pyicloud))

## Acknowledgements

- [`findmy-key-extractor`](https://github.com/manonstreet/findmy-key-extractor) by manonstreet — the original ARM64 extractor; we extended it to Intel + BeaconStore in [our fork](https://github.com/PrayerfulDrop/findmy-key-extractor).
- [`FindMy.py`](https://github.com/malmeloo/FindMy.py) by malmeloo — the Apple Find My protocol client our AirTag path drives.
- [iCloud3](https://github.com/gcobb321/icloud3) by gcobb321 (and contributors, especially `@COsm0cats`) — they discovered the IPv4-only-DNS workaround that made HSA2 2FA reliable again after Apple's iOS 2026.4 changes. We adopted the same fix.
- [`pyicloud`](https://github.com/picklepete/pyicloud) by picklepete — the iCloud client library our iDevices path uses.
