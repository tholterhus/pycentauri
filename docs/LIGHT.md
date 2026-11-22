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
  an idle printer. A hand toggle from the dashboard **pins the light**:
  a manually lit chamber is not darkened by the cool-down net — it stays
  on until you turn it off by hand, and a manual off hands control
  straight back to the automation (an active printer re-lights per the
  rule above). A toggle **at the printer itself** is treated the same
  way: lighting it up while the printer sits idle and cold pins it, and
  switching it off unpins - the most recent human action wins. The pin
  survives service restarts: a light already lit while the printer sits
  idle and cold is recognized as a hand toggle. Failed commands back off
  exponentially instead of retrying every push.

Requires `--enable-control`; the daemon reports the feature in
`/api/info` as `light_automation`.
