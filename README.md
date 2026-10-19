# Lepro LED (Home Assistant Custom Integration)

Monitor and control your **Lepro LED** devices from Home Assistant.  
This custom integration logs in to **Lepro Cloud**, retrieves your lights and strips, and exposes them as controllable lights in HA.

[![MIT License](https://img.shields.io/badge/license-MIT-informational)](LICENSE.md)

> ⚠️ This is a third‑party project, not affiliated with Lepro.

---

## ✨ Features

- Login with your **Lepro** account (email + password) and choose your region (default `na`).
- Discovers lights and strips in your account's first family.
- Light, 25 segment lights (S1-5), speed and sensitivity numbers, a cloud-online diagnostic sensor.
- `lepro_led.set_scene` service: apply one of the six verified DIY effects (`solid`, `breath`, `gradient`, `clockwise`, `counterclockwise`, `circular`) with a custom palette of 1-25 RGB colours, `speed` 0-100 and `brightness` (1-255) or `brightness_pct` (1-100), in one command. At least one field is required; omitted fields keep their current value. `direction` (`clockwise`/`counterclockwise`) only applies to those two effects. If a strip is in music mode, an explicit `effect` is required to change palette/speed (otherwise the call is rejected); brightness-only is still allowed. This is a local palette applied through HA; it does not read, save or name Lepro cloud scenes. Example:
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
- `lepro_led.set_segments` service. Effects, brightness (device range 100-1000) and availability follow the device.
- Local/BLE/LAN control is **not** supported (unproven); see [docs/feature-parity.md](docs/feature-parity.md) for what is implemented, cloud/phone-only, pending or blocked.

---

## 🔧 Installation

### Option A — HACS (recommended)
1. Make sure you have [HACS](https://hacs.xyz/) installed in Home Assistant.
2. In Home Assistant: **HACS → Integrations → ⋮ (three dots) → Custom repositories**.  
   Add `https://github.com/tgmstudios/lepro_led` as **Category: Integration**. The expanded integration is currently on the draft feature branch and is not yet merged into `main`; do not expect the draft features from the default HACS download until they are merged and released.
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

### Entities
- **Lights**: strip power, brightness, RGB color and the documented effects; S1-5 also has 25 segment lights. Color temperature is not exposed for S1-5.
- **Numbers**: effect speed and built-in-effect sensitivity.
- **Binary sensor**: reported cloud-online status. Model/firmware metadata are not separate sensor entities, and no reset buttons are provided.

> Notes:
> - Credentials are stored in Home Assistant’s config entries.
> - The integration communicates with Lepro’s cloud API (internet required).

---

## 🧪 Supported versions
- Declared minimum: **2024.11.0**, based on the reauthentication API used by the integration. This minimum has not been runtime-tested.
- Automated tests: Home Assistant **2026.2.3**, Python **3.13**. Candidate imports and read-only cloud bootstrap also pass on the installed Home Assistant **2026.8.2**, Python **3.14**; this is not a production installation or physical-control test.
- GitHub HACS/hassfest checks have not reported a result for this draft. No passing CI status is claimed.

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
[MIT](LICENSE.md)

