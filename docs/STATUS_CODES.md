# Print status codes

> Moved out of the README.

## Print status codes

`print_status` in the API and library uses the CC1 firmware's code
space, extended with three codes for CC2 Canvas operations:

| Code | Meaning | | Code | Meaning |
|---|---|---|---|---|
| 0 | Idle | | 13 | Printing |
| 1 | Homing | | 14 | Error |
| 5 | Pausing | | 15 | Leveling |
| 6 | Paused | | 16 | Preheating |
| 7 | Stopping | | **27** | **Switching filament** (CC2) |
| 8 | Stopped | | **28** | **Filament load complete** (CC2) |
| 9 | Completed | | **29** | **Unloading filament** (CC2) |

The full table (including CC1's resin-inherited codes) is in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

On the CC2, mid-print Canvas filament switches are detected by head
position: the firmware never fully leaves its "printing" state during a
switch, but the head parks at the purge chute behind the bed (y ≥ 258 mm,
physically outside the printable area) for the duration. pycentauri
reports code 27 the entire time the head is parked there mid-print.
