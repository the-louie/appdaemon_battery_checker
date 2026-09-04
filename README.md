# Home Assistant Battery Checker

An AppDaemon script that automatically monitors battery levels of all devices in Home Assistant and sends consolidated notifications when batteries are low.

## Features

- **Automatic Detection**: Monitors all battery sensors and binary low battery indicators
- **Smart Filtering**: Excludes non-battery sensors (charging status, power, etc.)
- **Entity Exclusion**: Easily exclude specific sensors from notifications using the `exclude` list
- **Consolidated Notifications**: Sends one message with all low battery devices instead of individual alerts
- **Two-Level Alerts**:
  - Critical: ≤10% battery or binary sensors with "islow" (configurable)
  - Low: ≤20% battery or other low battery indicators (configurable)
- **Daily Scheduling**: Runs automatically at 18:15 daily (configurable)
- **Cooldown Protection**: Prevents notification spam with configurable cooldown periods
- **Action Support**: Users can dismiss notifications for 3 days via mobile app actions
- **Timezone Support**: Configurable timezone for scheduling

## Supported Sensor Types

- **Percentage Sensors**: `sensor.device_battery_level` with values like "15%"
- **Binary Sensors**: `binary_sensor.device_battery_islow` with "on"/"off" states
- **Low Battery Indicators**: Any binary sensor containing "battery", "batt", "islow", or "low_battery"

## Installation

1. Copy `i1_battery_checker.py` to your AppDaemon `apps` directory
2. Copy `config.yaml.example` to `config.yaml` and customize
3. Restart AppDaemon

## Configuration

See `config.yaml.example` for detailed configuration options. Key settings:

- `persons`: List of people to notify with their notification services
- `cooldown`: Seconds between notifications (prevents spam)
- `tracker`: Optional device tracker to only notify when home
- `exclude`: List of entity IDs to exclude from battery monitoring and notifications
- `low_battery_threshold`: Battery percentage below which a device is considered low (default: 20)
- `critical_battery_threshold`: Battery percentage below which a device is considered critical (default: 10)
- `check_time`: Time of day for daily check (default: "18:15:00")
- `timezone`: Timezone for scheduling (default: "Europe/Stockholm")

### Example: Exclude List

To exclude specific sensors from notifications, add them to the `exclude` list in your config:

```yaml
check_all_batteries:
  module: i1_battery_checker
  class: BatteryCheck

  exclude:
    - sensor.louies_iphone_2028
    - sensor.some_other_sensor

  persons:
    - name: louie
      notify: mobile_app_iphone_28
      tracker: device_tracker.iphone_28
      cooldown: 120
```

## Example Output

```
🚨 KRITISK LÅG BATTERI:
• Motion Sensor: 5%
• Outdoor Camera: KRITISK LÅG BATTERI

⚠️ Lågt batteri:
• Kitchen Temperature: 15%
• Hallway Sensor: 18%
```

## Testing

A standalone test script (`test_battery_checker.py`) is included to verify the exclude logic and threshold configuration. Run it with:

```
python test_battery_checker.py
```

## Requirements

- AppDaemon 4.x
- Home Assistant with battery sensors
- Notification services configured (mobile_app, telegram, etc.)

## License

Copyright (c) the_louie

This project is licensed under the BSD 2-Clause License - see the [LICENSE](LICENSE) file for details.

## Devices that stop reporting

A flat cell does not report 1%. The device drops off the mesh, every one of its
entities turns `unavailable`, and until 2026-09-04 this app skipped exactly
that state — so the louder the failure, the quieter it got. Measured on the
estate it runs on, seventeen battery devices had died that way, between 27 days
and two years earlier, without a single notification (see H-27).

`report_absent: true` (the default) adds the other half. It classifies silent
battery entities into populations, because conflating them is what makes this
kind of alert useless:

| | what it means | what happens |
|---|---|---|
| **new** | silent now, reporting at the last check | its own notification, `[BAT001]` at ERROR |
| **standing** | silent now and silent before | one summary line, `[BAT002]` at WARNING |
| **returned** | was silent, reporting again | notification, `[BAT003]` — closes the loop |
| **gone** | no longer in Home Assistant at all | `[BAT004]` at ERROR; a battery will not fix it |

Seventeen months-old corpses must not drown out the one device that died last
night, which is the whole reason for the split.

### Two rules that keep it honest

**An entity that has never reported a value is not a dead battery.** Z-Wave
publishes a capability entity per command-class property, and the ones the
hardware does not implement sit at `unavailable` for life — `Motion Ute Norr`
alone contributes nine while the device itself reports 60%. The app only counts
an entity as absent if it has seen it report at least once, which needs no
maintenance and does not rot the way an exclude list would.

**Suppression needs a reason and an expiry.** `expected_absent` takes both and
rejects an entry missing either, at ERROR. A suppression nobody can explain is
one nobody dares remove; one with no review date outlives the person who set it.

### The roster file

State lives in `roster_file` (default `/conf/battery_roster.json`), deliberately
outside the app directory so a redeploy does not wipe it and re-announce every
standing absence as news.

A first run with no roster is **quiet by design**: it learns which entities
report and alerts from the second run on. To start with real history instead,
copy `battery_roster.seed.json` — measured 2026-09-04, with `since` timestamps
taken from ZHA's own `last_seen`, so the ages in `[BAT002]` are true — to that
path before the first run.

### One AppDaemon setting these log lines depend on

`[BAT001]` and `[BAT002]` embed the device's `friendly_name`, so a name like
`Källare t/h batteri` only survives to the log if `appdaemon.yaml` sets

    ascii_encode: false

AppDaemon defaults this to **true**, which encodes every message to UTF-8 and
then decodes it back as ASCII with `errors="replace"` — turning each `ö` into
two U+FFFD. Notifications are unaffected either way; `call_service` does not go
through `log()`. See H-29.
