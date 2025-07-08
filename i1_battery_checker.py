"""
AppDaemon script for monitoring battery levels in Home Assistant.

This script checks all battery sensors and binary sensors for low battery conditions
and sends consolidated notifications to specified persons.
"""

import pytz
from datetime import datetime, timedelta
import time
import appdaemon.plugins.hass.hassapi as hass

# Set timezone for the application
timezone = pytz.timezone('Europe/Stockholm')


class BatteryCheck(hass.Hass):
    """
    Battery monitoring class for Home Assistant.

    Monitors battery levels of devices and sends notifications when batteries are low.
    Supports both percentage-based battery sensors and binary low battery indicators.
    """

    async def initialize(self):
        """Initialize the battery checker application."""
        self.log("Loading BatteryCheck()")

        # Get configuration from args
        self.persons = self.args.get("persons", [])
        self.msg_cooldown = {}

        # Schedule daily battery check at 18:15
        self.run_daily(self.daily_battery_check, "18:15:00", timezone=timezone)

        # Register event listener for notification actions
        self.listen_event(self.phone_action, "mobile_app_notification_action")

        # Run initial battery check on startup
        await self.daily_battery_check()

    async def daily_battery_check(self, kwargs=None):
        """
        Daily battery check function that runs at 18:15.

        Args:
            kwargs: Optional keyword arguments passed by the scheduler
        """
        self.log("Running daily battery check...")
        states = await self.get_state()

        # Lists to collect devices with low battery
        critical_devices = []
        low_devices = []

        for entity_key in sorted(states):
            entity = states.get(entity_key)
            attributes = entity.get("attributes", {})
            device_class = attributes.get("device_class")

            # Check for battery-related entities
            if device_class == "battery":
                self._check_battery_sensor(entity_key, entity, critical_devices, low_devices)

            # Check for binary sensors that indicate low battery
            elif self._is_battery_binary_sensor(entity_key):
                self._check_battery_binary_sensor(entity_key, entity, critical_devices, low_devices)

        # Send consolidated notifications
        self._send_battery_notifications(critical_devices, low_devices)

    def _is_battery_binary_sensor(self, entity_key):
        """
        Check if entity is a battery-related binary sensor.

        Args:
            entity_key: The entity ID to check

        Returns:
            bool: True if it's a battery binary sensor, False otherwise
        """
        if not entity_key.startswith("binary_sensor."):
            return False

        battery_terms = ["battery", "batt", "islow", "low_battery"]
        return any(term in entity_key.lower() for term in battery_terms)

    def _check_battery_sensor(self, entity_key, entity, critical_devices, low_devices):
        """
        Check a percentage-based battery sensor for low battery conditions.

        Args:
            entity_key: The entity ID
            entity: The entity state data
            critical_devices: List to append critical battery devices
            low_devices: List to append low battery devices
        """
        state = entity.get("state")
        attributes = entity.get("attributes", {})
        uof = attributes.get("unit_of_measurement")

        # Skip non-battery level sensors (like charging_status)
        skip_terms = ["charging_status", "recharge", "power"]
        if any(term in entity_key.lower() for term in skip_terms):
            return

        if state not in ["unavailable", "unknown"]:
            self.log(f"* {entity_key} = {state}{uof}")
            self._evaluate_battery_level(entity_key, state, attributes, critical_devices, low_devices)

    def _check_battery_binary_sensor(self, entity_key, entity, critical_devices, low_devices):
        """
        Check a binary sensor for low battery indication.

        Args:
            entity_key: The entity ID
            entity: The entity state data
            critical_devices: List to append critical battery devices
            low_devices: List to append low battery devices
        """
        state = entity.get("state")
        attributes = entity.get("attributes", {})

        if state == "on":  # "on" means low battery is detected
            device_name = attributes.get("friendly_name", entity_key)

            # Check if it's a critical battery sensor (contains "islow")
            if "islow" in entity_key.lower():
                critical_devices.append(f"• {device_name}: KRITISK LÅG BATTERI")
                self.log(f"Critical battery detected for {device_name} (binary sensor)")
            else:
                low_devices.append(f"• {device_name}: Lågt batteri")
                self.log(f"Low battery detected for {device_name} (binary sensor)")

    def _evaluate_battery_level(self, entity_key, state, attributes, critical_devices, low_devices):
        """
        Evaluate battery level and categorize as critical or low.

        Args:
            entity_key: The entity ID
            state: The battery level state
            attributes: Entity attributes
            critical_devices: List to append critical battery devices
            low_devices: List to append low battery devices
        """
        try:
            # Convert state to float for comparison
            battery_level = float(state)

            # Define low battery thresholds
            low_battery_threshold = 20  # 20% or below is considered low
            critical_battery_threshold = 10  # 10% or below is critical

            # Get device name from attributes
            device_name = attributes.get("friendly_name", entity_key)

            # Check if battery is low and add to appropriate list
            if battery_level <= critical_battery_threshold:
                critical_devices.append(f"• {device_name}: {battery_level}%")
                self.log(f"Critical battery detected for {device_name}: {battery_level}%")
            elif battery_level <= low_battery_threshold:
                low_devices.append(f"• {device_name}: {battery_level}%")
                self.log(f"Low battery detected for {device_name}: {battery_level}%")

        except (ValueError, TypeError):
            # Handle cases where state is not a number
            self.log(f"Could not parse battery level for {entity_key}: {state}", level="DEBUG")

    def _send_battery_notifications(self, critical_devices, low_devices):
        """
        Send consolidated battery notifications.

        Args:
            critical_devices: List of devices with critical battery levels
            low_devices: List of devices with low battery levels
        """
        if not critical_devices and not low_devices:
            self.log("No low battery devices found")
            return

        # Build the message
        message_parts = []

        if critical_devices:
            message_parts.append("🚨 KRITISK LÅG BATTERI:")
            message_parts.extend(critical_devices)
            message_parts.append("")  # Empty line for spacing

        if low_devices:
            message_parts.append("⚠️ Lågt batteri:")
            message_parts.extend(low_devices)

        # Join all parts into one message
        full_message = "\n".join(message_parts)

        # Send the consolidated notification
        self._notify_persons("Batterivarning", full_message)
        self.log(f"Sent consolidated battery notification with {len(critical_devices)} critical and {len(low_devices)} low battery devices")

    def _notify_persons(self, title, message):
        """
        Send notification to all configured persons.

        Args:
            title: Notification title
            message: Notification message
        """
        for person in self.persons:
            notify_addr = person.get("notify")
            cooldown = person.get("cooldown", 0)

            # Validate notify_addr exists
            if not notify_addr:
                self.log(f"Missing notify address for person: {person.get('name', 'Unknown')}", level="WARNING")
                continue

            # Check cooldown (avoid division by zero)
            if cooldown > 0:
                time_since_last = time.time() - self.msg_cooldown.get(notify_addr, 0)
                if time_since_last < int(cooldown):
                    self.log(f"Cooldown activated for {notify_addr}, last msg sent {time_since_last:.0f}s ago", level="DEBUG")
                    continue

            # Send notification
            self.call_service(
                f"notify/{notify_addr}",
                title=title,
                message=message,
                data={
                    "actions": [{
                        "action": f"{self.name}.ignore.{notify_addr}",
                        "title": "Ignorera 3d"
                    }]
                }
            )
            self.msg_cooldown[notify_addr] = time.time()
            self.log(f"Notification sent to {notify_addr}", level="DEBUG")

    def phone_action(self, event_name, data, kwargs):
        """
        Handle notification action responses.

        Args:
            event_name: The event name
            data: Event data containing action information
            kwargs: Additional keyword arguments
        """
        action = str(data.get("action")).split(".")
        if action[0] != self.name:
            return

        if action[1] == "ignore":
            dt_now = datetime.now(timezone)
            future_time = datetime(dt_now.year, dt_now.month, dt_now.day, tzinfo=timezone) + timedelta(days=3)
            self.msg_cooldown[action[2]] = future_time.timestamp()
            self.log(f"IGNORE {action[2]} until {future_time.strftime('%Y-%m-%d %H:%M')}")



