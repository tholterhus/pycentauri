# Chamber light

> Moved out of the README - the overview and quick start live there.

## Chamber-light automation

The daemon can run the little Home-Assistant automation pair that used to
live around the printer — light on when a print starts, light off once the
nozzle has cooled down:

```sh
centauri server --host printer.example --enable-control \
    --light-automation --light-cool-temp 50
```

- **On** at any printer activation — preheating, a print start, a
  filament change, whatever occupies it — and **on** while the nozzle is
  above `--light-cool-temp` (default 50 °C). **Off** only once the
  printer is idle again AND the nozzle has cooled below the threshold:
  the temperature keeps acting as the safety net that eventually darkens
  an idle printer. A light toggled by hand is put back in line with the
  target on the next status push, and attaching to a busy, idle, hot or
  cold printer (service restart) normalizes the state immediately.
  Failed commands back off exponentially instead of retrying every push.

Requires `--enable-control`; the daemon reports the feature in
`/api/info` as `light_automation`.
