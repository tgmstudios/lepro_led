# Lepro LED (Home Assistant Custom Integration)

Monitor and control your **Lepro LED** devices from Home Assistant.  
This custom integration logs in to **Lepro Cloud**, retrieves your lights and strips, and exposes them as controllable lights in HA.

[![License](https://img.shields.io/badge/license-see%20LICENSE.md-informational)](LICENSE.md)
[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=tgmstudios&repository=lepro_led&category=integration)

> ⚠️ This is a third‑party project, not affiliated with Lepro.

---

## ✨ Features

- Login with your **Lepro** account (email + password) and choose your region (default `na`).
- **Synced with upstream Sanji78/lepro_led 1.4.1** (US region, B-series/SE1 bulbs with colour temperature, power switch, `send_debug_command`/`request_debug_state`, Japanese translation), woven into this fork's cloud runtime. B-series/SE1 payloads come from upstream's app captures and are **not hardware-verified here**.
- Discovers lights and strips in your account's first family.
- Light, 25 segment lights (S1-5), speed and sensitivity numbers, a cloud-online diagnostic sensor.
- `lepro_led.set_scene` service: apply one of the six verified DIY effects (`solid`, `breath`, `gradient`, `clockwise`, `counterclockwise`, `circular`) with a custom palette of 1-25 RGB colours, `speed` 0-100 and `brightness` (1-255) or `brightness_pct` (1-100), in one command. At least one field is required; omitted fields keep their current value. `direction` (`clockwise`/`counterclockwise`) only applies to those two effects. If a strip is in music mode, an explicit `effect` is required to change palette/speed (otherwise the call is rejected); brightness-only is still allowed. This is a local palette applied through HA; it does not read or save Lepro cloud scenes (see cloud scenes below). Example:
  ```yaml
  action: lepro_led.set_scene
  data:
    entity_id: light.strip
    effect: gradient
    colors: [[255, 0, 0], [0, 0, 255]]
    speed: 60
    brightness_pct: 80
  ```
  The palette is supplied to Home Assistant; commands still use cloud MQTT. The strip light also exposes `palette`, `effect_speed` and `direction` attributes. Each strip receives one command, but a multi-strip call is not a distributed transaction: a device report or connection failure during the call can leave only some strips updated.
- **Cloud scenes**: at setup (and on `lepro_led.refresh_cloud_scenes`) the integration reads each strip's saved Lepro cloud scenes (read-only, first 50). They appear as options of the per-strip `Cloud scene` select; `lepro_led.apply_cloud_scene` applies one by `scene_id` (shown in the select's `scene_ids` attribute) with optional brightness. The server's d50 string is replayed unchanged, and a scene is shown as active only after the device reports it. Names are whatever your account returns; if the catalog cannot be read the rest of the integration still works. Only the first 50 scenes of each strip are read, so this is not a complete catalog of everything in your account. Applying to several strips is not a distributed transaction: each strip gets its own publish, so a failure on the second can leave the first already changed. An expired token is renewed with one shared re-login; refreshes run at most two at a time. A label that duplicates another gets its scene id appended; use `scene_id` for automations. Not the same as `set_scene`. The previously documented "40 effects" are 40 parameter-bound config entries, not 40 named presets.
- `lepro_led.set_segments` service. Effects, brightness (device range 100-1000) and availability follow the device.
- Local/BLE/LAN control is **not** supported (unproven); see [docs/feature-parity.md](docs/feature-parity.md) for what is implemented, cloud/phone-only, pending or blocked.

---

## 🔧 Installation

### Option A — HACS (recommended)
1. Make sure you have [HACS](https://hacs.xyz/) installed in Home Assistant.
2. In Home Assistant: **HACS → Integrations → ⋮ (three dots) → Custom repositories**.  
   Add `https://github.com/tgmstudios/lepro_led` as **Category: Integration** (or use the badge above, which opens this repository in your own Home Assistant's HACS).
   This is a custom repository: it is **not** in HACS's default catalog and nothing here claims it is.
3. Find **Lepro LED** in HACS and click **Download**.
4. **Restart** Home Assistant.

### Option B — Manual
1. Copy the folder `custom_components/lepro_led` from this repository into your Home Assistant config folder:
   - `<config>/custom_components/lepro_led`
2. **Restart** Home Assistant.

---

## ⚙️ Configuration

1. Home Assistant → **Settings → Devices & services → Add Integration**.
2. Search for **Lepro LED**.
3. Enter your **Lepro email and password**.
4. On success, entities will be created for each device.

### Options
- `region`: Europe, United States, North America, Far East
- `language`: includes `en`, `it`, and `ja`

### Entities
- **Lights**: strip power, brightness, RGB color and the documented effects; S1-5 also has 25 segment lights. Color temperature is not exposed for S1-5.
- **Numbers**: effect speed and built-in-effect sensitivity.
- **Switch**: per-device power-only switch (`<did>_power`, ported from upstream); sends only `d1`.
- **Select**: per-strip cloud scene selector.
- **Binary sensor**: reported cloud-online status. Model/firmware metadata are not separate sensor entities, and no reset buttons are provided.

> Notes:
> - Credentials are stored in Home Assistant’s config entries.
> - The integration communicates with Lepro’s cloud API (internet required).

---

## B* Notes

The B* protocol does not behave like strip-focused `d50` control, so support has been implemented by comparing Home Assistant traffic with the official app's MQTT payloads.

### Current direction
- RGB mode is driven by `d2=1` plus `d5`.
- White/static mode is driven by `d2=0` plus `d3` and `d4`.
- Some B* behavior is still under active investigation and may require more protocol tuning.

### Observed B* protocol fields

| Field | Observed meaning | Notes |
| --- | --- | --- |
| `d1` | Power state | `1` on, `0` off |
| `d2` | Mode | `0` = white/static, `1` = RGB |
| `d3` | White brightness | In white mode, app writes values like `250`, `500`, `750`, `1000` |
| `d4` | White-mode companion value | Observed as `500` in app-driven white mode |
| `d5` | RGB payload body | Observed format: `{hue_hex}{03E8}{value_hex}` |
| `d30` | Extra device state field | Seen in reports, but not required by the official app's RGB writes |
| `d52` | Generic brightness field | Used by other devices/modes, but not the app's main B* white write path |

### Observed B* examples

| Scenario | App payload |
| --- | --- |
| Red 100% | `{'d2': 1, 'd5': '000003E803E8'}` |
| Green 100% | `{'d2': 1, 'd5': '007803E803E8'}` |
| Blue 100% | `{'d2': 1, 'd5': '00F003E803E8'}` |
| White 25% | `{'d2': 0, 'd3': 250, 'd4': 500}` |
| White 50% | `{'d2': 0, 'd3': 500, 'd4': 500}` |
| White 75% | `{'d2': 0, 'd3': 750, 'd4': 500}` |
| White 100% | `{'d2': 0, 'd3': 1000, 'd4': 500}` |

These values were captured from the official app via MQTT logging and are included here so future protocol work has a stable reference.

---

## 🧪 Supported versions
- Declared minimum: **2024.11.0**, based on the reauthentication API used by the integration. This minimum has not been runtime-tested.
- Automated tests: Home Assistant **2026.2.3**, Python **3.13** (`python -m pytest -q -W error::RuntimeWarning`; also run by the `Tests` workflow). Candidate imports and read-only cloud bootstrap passed earlier on Home Assistant **2026.8.2**; this is not a physical-control test.
- CI: `Validate for HACS` (hacs/action, `ignore: brands` because no brand assets are bundled) and `Validate with hassfest` workflows are included; check the Actions tab for current results. Nothing claims inclusion in the HACS default catalog.
- Not supported / unverified: BLE, LAN control, timers and schedules (see [docs/feature-parity.md](docs/feature-parity.md)).

---

## 🐞 Troubleshooting
- Check **Settings → System → Logs** for messages under `custom_components.lepro_led`.
- If login fails, verify email/password by signing into the official Lepro app.
- If entities don’t update, ensure Home Assistant can reach the internet.

---

## 🙌 Contributing
PRs and issues are welcome. Please open an issue with logs if you hit a bug.

---

## ❤️ Donate
If this project helps you, consider buying me a coffee:  
**[PayPal](https://www.paypal.me/elenacapasso80)**.

..and yes... 😊 the paypal account is correct. Thank you so much!

---

## 📜 License
See [LICENSE.md](LICENSE.md). This repository is a fork of [Sanji78/lepro_led](https://github.com/Sanji78/lepro_led); the upstream license file is kept unchanged (it currently reads "All Rights Reserved", while earlier upstream revisions were MIT). Ask the upstream author if you need clarity on reuse.

