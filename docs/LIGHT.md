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

- **On** while the nozzle is above `--light-cool-temp` (default 50 °C),
  **off** while it is below — the print status is irrelevant, only the
  nozzle temperature decides ("nur die Nozzle-Temp entscheidet"). A print
  starting cold lights up as soon as the nozzle passes the threshold on
  its way up; at print end, during a long pause or after an aborted start
  it goes out on the way down. A light toggled by hand is put back in
  line with the temperature on the next status push, and attaching to an
  already hot or cold printer (service restart) normalizes the state
  immediately. Failed commands back off exponentially instead of retrying
  every push.

Requires `--enable-control`; the daemon reports the feature in
`/api/info` as `light_automation`.
