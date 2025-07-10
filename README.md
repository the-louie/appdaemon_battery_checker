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
