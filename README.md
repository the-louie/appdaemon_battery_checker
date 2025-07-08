# Home Assistant Battery Checker

An AppDaemon script that automatically monitors battery levels of all devices in Home Assistant and sends consolidated notifications when batteries are low.

## Features

- **Automatic Detection**: Monitors all battery sensors and binary low battery indicators
- **Smart Filtering**: Excludes non-battery sensors (charging status, power, etc.)
- **Consolidated Notifications**: Sends one message with all low battery devices instead of individual alerts
- **Two-Level Alerts**:
  - Critical: ≤10% battery or binary sensors with "islow"
  - Low: ≤20% battery or other low battery indicators
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

## Example Output

```
🚨 KRITISK LÅG BATTERI:
• Motion Sensor: 5%
• Outdoor Camera: KRITISK LÅG BATTERI

⚠️ Lågt batteri:
• Kitchen Temperature: 15%
• Hallway Sensor: 18%
```

## Requirements

- AppDaemon 4.x
- Home Assistant with battery sensors
- Notification services configured (mobile_app, telegram, etc.)

## License

This project is licensed under the BSD 2-Clause License - see the [LICENSE](LICENSE) file for details.
