# App feature parity matrix (S1-5, PID 45 / control 3)

Source: static analysis of the Lepro Android app 1.0.9 (269) plus the existing integration's wire behaviour.
Status values: **Implemented** (tested here), **Phone/cloud only**, **Blocked** (reason given), **Pending** (evidence being gathered; nothing sent).
Nothing here has been verified against hardware by this change; encodings marked "legacy-golden" are byte-identical to what the integration already sent (tests compare against output of commit `da95907`).

| Feature | Status | Evidence / reason |
|---|---|---|
| Power on/off (d1) | Implemented | app `DpSwitchLedValue`; legacy |
| RGB colour, all segments (d50) | Implemented | legacy-golden, 72 vectors |
| Scene palette/speed/brightness for the 6 verified DIY effects | Implemented (local palette only) | `lepro_led.set_scene`; separate from cloud scenes (next rows) |
| Per-segment colour, 25 segments (d50 groups) | Implemented | legacy-golden; `lepro_led.set_segments` service paints 1-25 colours as even blocks |
| Effects solid/breath/gradient/clockwise/counterclockwise/circular (d2=2, d50) | Implemented | legacy-golden. `none` is **not** added; `solid` stays |
| Effects flash, wave_1-4, laser_1-4 (d2=3, d60) | Implemented | legacy-golden prefix table `200..208 0064` |
| Effect speed (d50 speed code) | Implemented | `<did>_speed`; only resent when the active effect carries a speed (solid/d60 effects store the value without sending) |
| Effect sensitivity (d60 byte) | Implemented | `<did>_sensitivity`, 0-100 -> 0x00-0x63; applies to d60 effects only. This is a parameter of the device's built-in effects, **not** microphone music sync |
| Brightness (d52) | Implemented, **encoding changed** | App `SetRequestBuilder.dp52RgbIcMaxBrightness` is `@IntRange(100..1000)`. The old code mapped 0-255 to 0-1000 (could send <100 and 0). Now HA 1-255 -> 100-1000; incoming values are clamped to the app range. Whether the firmware accepts <100 was not tested, so the app range is used. d50 HSV brightness is separate and is not touched |
| Reported availability (`online`, transport state) | Implemented | `binary_sensor <did>_online` (diagnostic); all entities go unavailable when MQTT is down or device reports `online:0` |
| Colour temperature / white (d3/d4) | Not applicable to S1-5 | app config: `colorPicker=diy2`, not rgbcw. Not exposed |
| HSV colour (d5) | Not applicable to S1-5 | used by bulbs; codec removed rather than shipped untested |
| Bulb (B-series) / SE1 support from upstream v1.4.1 | Pending / not ported | Not needed for the registered devices; port separately, keeping `solid` and region default `na` |
| Cloud saved scenes: list + apply | Implemented (read-only GET, runtime-verified once on one account; no hardware apply test) | `GET /v3/scene/list/fid/{fid}/did/{did}/cid/0/limit/50/timestamp/{unix_s}` returns `striplight{dp:d50,res}`; `select.<strip>_cloud_scene`, `lepro_led.apply_cloud_scene` (entity_id + stable `scene_id`, optional brightness) and `lepro_led.refresh_cloud_scenes`. Apply publishes `{d1:1,d2:2,d50:<server res verbatim>[,d52]}` per static app code (`AIotUtils`). Labels come from the server (`groupName`, else `Scene <cid/sid>`). Catalog is the first page only (limit 50, cid 0); pagination and other cids are unproven. A scene shows as active only after the device reports the identical d50 string; otherwise the effect/palette attributes stay empty rather than guessing one of the six local effects |
| "40 builtin effects" | **Corrected: not 40 named presets** | `config_effect.json` holds ~40 *parameter-bound entries* (ids 2-30, 50-60 with ranges), not named effects and not an id->d50 table. The preset list is cloud-provided per account/strip and is shown dynamically; no names are invented. Mapping config ids to d50 remains unproven |
| Custom palette / DIY scene save (`/scene/diy/strip/save`) | Pending | cloud endpoint known, payload not captured |
| Scene save/edit/delete, scene groups, social sharing (`/scene/*` writes) | Phone/cloud only | not implemented; only the list GET is used |
| Music mode (`music2`, d60/d61/d62 streaming) | Phone/cloud only | `voice_controller=0`: the S1-5 has no mic; the app streams phone audio frames. Integration does not claim music sync; it would require an audio source plus a validated d62 frame format |
| Countdown d9 / timers d11-d13,d24 / sleep d17-19 / wake d20-22 | **Pending - not sent** | payload layouts undecoded; no entities, no guessed DPs |
| Power memory d14, blink d16, strip length d53 | Pending | semantic values not verified |
| Groups (d15/d29/d33), firmware OTA, sharing | Phone/cloud only | not implemented |
| AI scenes | Phone/cloud only | endpoint not located |
| BLE control | **Blocked** | GATT service/characteristics observed (read-only probe by parent), but the frame is encrypted (AES) and key derivation / bond handshake is not recovered. No BLE transport is shipped |
| LAN TCP control | **Blocked** | port and discovery not found; role (provisioning vs control) unproven |
| `iot_class` | `cloud_push` | local control is unproven, so it is not `local_*` |

## Transport / lifecycle fixes in this change
- MQTT: reconnect loop with backoff; **subscriptions re-established on every connect** (was lost after the first drop); state re-requested for every device on reconnect; publishes wait (10 s) for a connection then fail with an error surfaced to the caller; no unbounded pending queue; one `stop()` disconnects exactly once; unload actually tears the client down (the old `light.async_unload_entry` was never called).
- Setup: auth failure -> `ConfigEntryAuthFailed` (reauth flow); network/API/empty device list/MQTT timeout -> `ConfigEntryNotReady`.
- State is read from `entry.runtime_data`, not `hass.data` polling; commands are built from a copy of the state and published to the broker under a per-device lock; local values are committed only after the publish succeeds and never overwrite fields a device report touched meanwhile (a failed publish changes nothing, so no rollback can discard racing reports). "Published to broker" is not device confirmation: committed fields stay pending (`LeproRuntime.pending_confirmation`) until the device reports them on `le/<did>/prp/rpt` or `getr`. Our own `set` topic echo and `setr` acks (format not observed) are ignored; only exact 4-segment `rpt`/`getr` topics with valid data count as device reports. Full device-ack semantics (desired vs reported) are still unverified. Malformed or partial payloads leave existing state unchanged.
- Config: `region` field (default `na`; migration adds `na` to existing entries), unique id `account_region`, credentials validated in the config flow.
- Removed the hard-coded third-party FCM token; each install gets a random token stored in the entry. **Not live-tested** - if the cloud rejects unknown FCM values for an existing account, login will fail and needs a live check before deploy.
- Not changed: TLS policy (CA + client cert + bundled `client_key.pem`, `ssl.create_default_context()`), login fields, topics, and unique IDs `<did>`, `<did>_speed`, `<did>_sensitivity`, `<did>_segment_NN`. New: `<did>_online`.
- `/statistic/record` telemetry is not called.

## Foundation fixes after independent review
- API: login/profile/family/device-list payloads are type-validated; schema-broken or non-JSON responses raise `LeproApiError` (setup -> retry, config flow -> `cannot_connect`); only an explicit non-zero `code` or HTTP 401/403 raises `LeproAuthError`. Server `msg` is never echoed.
- Migration: legacy entries without `unique_id` get `account.lower()_region`; if another entry already owns it, the legacy entry is left unchanged (no merge, no credential overwrite). The config flow also aborts if a legacy entry with no unique id matches account+region.
- `hacs.json` minimum is now HA 2024.11.0 (`_get_reauth_entry`). This is from API inspection; tests only ran on HA 2026.2.3 (suite) and 2026.8.2 (import check), not on 2024.11.
- Transport exposes `async_wait_connected(timeout)`; setup no longer touches a private attribute.
- Parity is not complete; see the pending rows above.
