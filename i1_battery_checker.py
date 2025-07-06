import pytz
from datetime import datetime, timedelta
import time
import json
import appdaemon.plugins.hass.hassapi as hass

timezone = pytz.timezone('Europe/Stockholm')

"""
"""

class BatteryCheck(hass.Hass):
  async def initialize(self):
    self.log("Loading BatteryCheck()")
    self.persons = self.args.get("persons", [])
    self.msg_cooldown = {}

    # Schedule daily battery check at 18:15
    self.run_daily(self.daily_battery_check, "18:15:00", timezone=timezone)

    # Run initial battery check on startup
    await self.daily_battery_check()

  async def daily_battery_check(self, kwargs=None):
    """Daily battery check function that runs at 18:15"""
    self.log("Running daily battery check...")
    states = await self.get_state()

    # Lists to collect devices with low battery
    critical_devices = []
    low_devices = []

    """
{
  "entity_id": "sensor.pixel_3_batteriniva",
  "state": "58",
  "attributes": {
    "state_class": "measurement",
    "unit_of_measurement": "%",
    "device_class": "battery",
    "icon": "mdi:battery-50",
    "friendly_name": "Pixel 3 Battery Level"
  },
  "last_changed": "2022-05-04T15:49:06.437474+00:00",
  "last_updated": "2022-05-04T15:49:06.437474+00:00",
  "context": {
    "id": "c44d1b6c450f433aa7cfaba6f753eb2a",
    "parent_id": null,
    "user_id": null
  }
}
    """
    for entity_key in sorted(states):
      entity = states.get(entity_key)
      attributes = entity.get("attributes")
      device_class = attributes.get("device_class")

      # Check for battery-related entities
      if device_class == "battery":
        state = states[entity_key].get("state")
        uof = attributes.get("unit_of_measurement")

        # Skip non-battery level sensors (like charging_status)
        if any(skip_term in entity_key.lower() for skip_term in ["charging_status", "recharge", "power"]):
          continue

        if state not in ["unavailable", "unknown"]:
          self.log("* {} = {}{}".format(entity_key, state, uof))
          # Check for low battery and collect results
          self.check_battery_level(entity_key, state, attributes, critical_devices, low_devices)

      # Check for binary sensors that indicate low battery
      elif entity_key.startswith("binary_sensor.") and any(battery_term in entity_key.lower() for battery_term in ["battery", "batt", "islow", "low_battery"]):
        state = states[entity_key].get("state")
        if state == "on":  # "on" means low battery is detected
          device_name = attributes.get("friendly_name", entity_key)

          # Check if it's a critical battery sensor (contains "islow")
          if "islow" in entity_key.lower():
            critical_devices.append(f"• {device_name}: KRITISK LÅG BATTERI")
            self.log(f"Critical battery detected for {device_name} (binary sensor)")
          else:
            low_devices.append(f"• {device_name}: Lågt batteri")
            self.log(f"Low battery detected for {device_name} (binary sensor)")

    # Send consolidated notifications
    self.send_battery_notifications(critical_devices, low_devices)

  def check_battery_level(self, entity_key, state, attributes, critical_devices, low_devices):
    """Check if battery level is low and collect devices"""
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

  def send_battery_notifications(self, critical_devices, low_devices):
    """Send consolidated battery notifications"""
    if not critical_devices and not low_devices:
      self.log("No low battery devices found")
      return

    # Build the message
    message_parts = []

    if critical_devices:
      message_parts.append("KRITISK LÅG BATTERI:")
      message_parts.extend(critical_devices)
      message_parts.append("")  # Empty line for spacing

    if low_devices:
      message_parts.append("⚠️ Lågt batteri:")
      message_parts.extend(low_devices)

    # Join all parts into one message
    full_message = "\n".join(message_parts)

    # Send the consolidated notification
    self.my_notify("Batterivarning", full_message)
    self.log(f"Sent consolidated battery notification with {len(critical_devices)} critical and {len(low_devices)} low battery devices")

  # notify anyone home
  def my_notify(self, title, message):
    for person in self.persons:
      if time.time() - self.msg_cooldown.get(person.get("notify"), 0) < int(person.get("cooldown", 0)):
        self.log("cooldown activated for {}, last msg sent {}s ago".format(person.get("notify"), time.time() - self.msg_cooldown.get(person.get("notify"), 0)), level="DEBUG")
      #elif person.get("tracker") is not None and self.get_state(person.get("tracker")) == "home":
      else:
        notify_addr = person.get("notify")
        self.call_service(f"notify/{notify_addr}", title=title, message=message, data={"actions":[{"action": f"{self.name}.ignore.{person.get("notify")}", "title":"Ignorera 3d"}]})
        # from washer
        # self.call_service("notify/{}".format(person), title = self.washer_name, message = self.notify_message)
        #self.call_service("notify/send_message", title=title, message=message, service_data={"target": "media_player.tom_office"})
        self.msg_cooldown[person.get("notify")] = time.time()
        self.log("notify/{}".format(person.get("notify")), level="DEBUG")

  # handle notification action
  """
{
    "event_type": "mobile_app_notification_action",
    "data": {
        "action_1_key": "ignore",
        "action_1_title": "Ignorera idag",
        "action_1_uri": "null",
        "message": "Öppna balkingdörren i sovrummet",
        "action": "ignore",
        "device_id": "41e243a31ab95a55"
    },
    "origin": "REMOTE",
    "time_fired": "2022-05-03T14:15:35.176865+00:00",
    "context": {
        "id": "b5df1d663d5dc1b9f1e69f4ef1e20102",
        "parent_id": null,
        "user_id": "80c63e949b2a4d9ea0a47f36d4232529"
    }
}
  """
  def phone_action(self, event_name, data, kwargs):
    action = str(data.get("action")).split(".")
    if action[0] != self.name:
      return


    if action[1] == "ignore":
      dt_now = datetime.now(timezone)
      future_time = datetime(dt_now.year, dt_now.month, dt_now.day, tzinfo=timezone) + timedelta(days=3)
      self.msg_cooldown[action[2]] = future_time.timestamp()
      self.log(f"IGNORE {action[2]} until {self.msg_cooldown}")



