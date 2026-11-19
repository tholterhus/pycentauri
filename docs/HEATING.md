# Chamber heating (heated print room)

> Moved out of the README - the overview and quick start live there.
> This document holds the full details: hardware, setup, automation
> behavior, safety watchdog, overshoot learning and the manual override.

## Chamber heating (heated print room)

ABS and ASA print best in a warm chamber - and this printer cannot warm
its own air. This feature adds that: it drives an external heater for
the room around the printer and holds the chamber at the temperature
you (or your slicer profile) want, automatically and unattended.

### How it works

A cheap PTC fan heater sits in or under the printer and is plugged into
a small smart plug (a Shelly). pycentauri switches that plug with two
plain HTTP addresses (the plug's "on" and "off" commands) - no cloud,
everything stays inside your own network. While a print runs, the
printer's own chamber sensor tells pycentauri how warm it is inside,
and the heater is switched on and off to hold the set temperature.
When the print ends, the heater turns off by itself. A built-in
watchdog watches every heating phase: if the temperature does not rise
while the heater is on (heater faulty or unplugged, Shelly offline, or
a door left open), the heater is switched off and a red warning appears
in the dashboard - nothing keeps heating unattended when something is
wrong.

### What you need

- **A heater that a smart plug can switch.** The popular variant: a
  PTC fan heater (PTC ceramic element with a fan, ~300 W, around
  25-30 EUR) - PTC elements self-regulate and cannot overheat, which
  makes them safe for unattended use. Ready-made heated-chamber
  solutions work too; anything that turns on and off with two switch
  commands is compatible.
- **A smart plug** (e.g. a Shelly) that exposes two simple switch
  commands as HTTP addresses.
- Optional but recommended: **print a bracket** for the heater (ASA or
  ABS - it lives in a hot chamber) and route the cable out of the
  printer. One setup that works: the PTC fan heater hangs in a printed
  holder under the print bed, the cable leaves the printer at the side
  and runs to a Shelly on the outside.

### Setup

1. Start the server with the feature enabled (`centauri server
   --heating` - the ON/OFF URLs can be set later in the web UI).
2. Open the **Chamber heating** section in the dashboard (the gear
   icon), paste the two switch URLs and enable the feature.
3. Tell it when to heat: either a bed temperature threshold, filename
   markers (e.g. ABS,ASA) - or let your slicer set a chamber target.
4. Adjust the target temperature, the hysteresis (how much the
   temperature may swing around it) and the safety cap in the same
   section.

### Details for the curious

- The target temperature follows the slicer when the profile sets a
  chamber temperature - clamped to a safety cap
  (`HEATING_TARGET_MAX`) so nothing can heat beyond it.
- The automation **learns**: it measures how far the chamber overshoots
  after the heater switches off (thermal inertia) and switches the
  heater off a little earlier the next time, so the peak stays inside
  the band.
- A safety watchdog switches the heater off and shows a fat red
  warning when a heating phase does not warm the chamber.
- The graph in the panel shows the chamber temperature of the current
  print with shaded heating phases - and it survives page reloads,
  browser changes and service restarts (the data lives on the box).
- The settings persist on the box (`data/heating.json`), so every
  device shows the same configuration and graph.
