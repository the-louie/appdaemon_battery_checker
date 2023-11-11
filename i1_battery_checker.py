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
    return
    self.log("Loading BatteryCheck()")
    states = await self.get_state()

    """
 {"entity_id": "sensor.pixel_3_batteriniva", "state": "58", "attributes": {"state_class": "measurement", "unit_of_measurement": "%", "device_class": "battery", "icon": "mdi:battery-50", "friendly_name": "Pixel 3 Battery Level"}, "last_changed": "2022-05-04T15:49:06.437474+00:00", "last_updated": "2022-05-04T15:49:06.437474+00:00", "context": {"id": "c44d1b6c450f433aa7cfaba6f753eb2a", "parent_id": null, "user_id": null}}                                                                                                         
    """
    for entity_key in sorted(states):
      entity = states.get(entity_key)
      attributes = entity.get("attributes")
      device_class = attributes.get("device_class")
      if device_class == "battery":
        state = states[entity_key].get("state")
        uof = attributes.get("unit_of_measurement")
        if state not in ["unavailable", "unknown"]:
          self.log("* {} = {}{}".format(entity_key, state, uof))

  def check_temperature(self, kwargs):
    temperature = float(self.get_state(self.temperature.get("sensor")))
    window = self.get_state(self.window.get("sensor")) == "on"

    self.log("temperature: {}, {}-{} window open: {}".format(temperature, self.temperature.get("below"), self.temperature.get("above"), window), level="DEBUG")

    now = datetime.now()
    hour = now.hour
    if (hour < self.when.get("after") or hour >= self.when.get("before")):
      self.log("Hour out of bounds: {} < {} || {} >= {}".format(hour, self.when.get("after"), hour, self.when.get("before")), level="DEBUG")
      return

    if (temperature >= self.temperature.get("above") and window == self.window.get("above")):
      self.log("ALERT: {}".format(self.messages.get("above")), level="DEBUG")
      self.notify(self.messages.get("title"), self.messages.get("above"))
      return

    if (temperature < self.temperature.get("below") and window == self.window.get("below")):
      self.log("ALERT: {}".format(self.messages.get("below")), level="DEBUG")
      self.notify(self.messages.get("title"), self.messages.get("below"))
      return
    

  # notify anyone home
  def notify(self, title, message):
    for person in self.persons:
      if time.time() - self.msg_cooldown.get(person.get("notify"), 0) < int(self.messages.get("cooldown")):
        self.log("cooldown activated for {}, last msg sent {}s ago".format(person.get("notify"), time.time() - self.msg_cooldown.get(person.get("notify"), 0)), level="DEBUG")
      elif person.get("tracker") is not None and self.get_state(person.get("tracker")) == "home":
        self.call_service("notify/{}".format(person.get("notify")), message=message, data={"actions":[{"action": "{}.{}.{}".format(self.name, "ignore", person.get("notify")), "title":"Ignorera idag"}]})
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
      tomorrow_start = datetime(dt_now.year, dt_now.month, dt_now.day, tzinfo=timezone) + timedelta(1)
      self.msg_cooldown[action[2]] = tomorrow_start.timestamp()
      self.log("IGNORE {} until tomorrow {}".format(action[2], self.msg_cooldown), level="DEBUG")
      
            

