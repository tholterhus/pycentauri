# Known firmware quirks

> Moved out of the README.

## Known firmware quirks

### CC1 (original Centauri Carbon)

- **5 concurrent WebSocket slots, hard.** The 6th connection gets HTTP
  500 `"too many client"`. Slots free on close. The CLI opens one per
  invocation; the HTTP/MCP servers hold exactly one long-lived slot.
- **Paused/errored states don't push Attributes.** Every SDCP command
  needs the printer's `MainboardID`, which normally arrives in an
  Attributes push — but not while paused or errored. pycentauri
  pre-seeds it from UDP discovery on every connect. If you call
  `Printer.connect()` on a paused printer without discovery, pass
  `mainboard_id=` yourself.
- **Unknown commands crash the firmware.** A few unrecognised SDCP
  commands in quick succession kill the printer's `app` daemon — and any
  active print with it. Don't probe undocumented command codes against a
  printer that's doing something you care about.
- **The push scheduler goes dormant at idle (and a reboot doesn't wake
  it).** The firmware can enter a state where `Cmd 512` subscribes are
  acknowledged but no status frame is ever pushed while the printer
  sits idle — persisting across reboots (verified 2026-07-05 on
  V0.3.0-o). Starting a print revives pushes at full rate. One-shot
  `Cmd 0` requests always work, so pycentauri automatically falls back
  to polling when a subscribe goes quiet (~7 s updates at idle,
  full-rate pushes while printing). Clients that rely purely on
  subscribe pushes will hang forever on an idle printer in this state.

### CC2 (Centauri Carbon 2)

- **No UDP discovery.** The CC2 ignores broadcast probes; specify its IP
  explicitly. Give it a static DHCP lease — it doesn't register a
  hostname with most routers, so its address drifts otherwise.
- **Access code required for MQTT**, passed as the password with
  username `elegoo`. The HTTP bootstrap (`/system/info`) wants the same
  code as an `X-Token` *query parameter* — it ignores the header form.
- **The webcam is unauthenticated.** MJPEG on `:8080` (any path) is open
  to anyone on your LAN, access code or not. That's the firmware's
  choice, not ours.
- **Rate limiting.** Rapid-fire MQTT requests (3+ back-to-back) trip a
  cooldown of a few seconds during which the broker silently drops
  responses. pycentauri's polling cadence stays under it; your scripts
  should too.
- **The firmware resets the speed mode to balanced on every Canvas
  filament switch.** pycentauri pins your chosen mode and re-applies it
  automatically, while still honoring a deliberate balanced from the
  touchscreen — see
  [CC2 speed pinning](#cc2-speed-pinning-the-firmware-fights-you-so-pycentauri-fights-back)
  above for how it tells the two apart.
- **Registrations expire without an app-level PING.** The printer
  forgets a registered client after several quiet minutes and silently
  stops answering that session's requests — the MQTT connection itself
  stays up, so there's no error to catch. pycentauri sends the SDK's
  `{"type": "PING"}` keepalive every 30 s to hold the registration; if
  you write your own client, you must too.
- **File list (method 1044) and video stream (1042) don't respond** on
  firmware 01.03.02.51, so remote print-start on CC2 requires knowing
  the filename in advance.
