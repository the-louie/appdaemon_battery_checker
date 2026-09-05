"""
AppDaemon script for monitoring battery levels in Home Assistant.

This script checks all battery sensors and binary sensors for low battery conditions
and sends consolidated notifications to specified persons.

Features:
- Monitors both percentage-based battery sensors and binary low battery indicators
- Configurable battery thresholds
- Exclude list for specific entities
- Cooldown periods to prevent notification spam
- Action-based notification responses
"""

import json
import os
import pytz
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import time
from typing import List, Dict, Any, Optional, Tuple
import appdaemon.plugins.hass.hassapi as hass

import ha_states
import notification_policy as policy

# Set timezone for the application
timezone = pytz.timezone('Europe/Stockholm')

# States that mean "this entity is not telling us anything".
# The definition is estate-wide since S7-07 (T-06); this alias keeps the
# module's public name for its tests and callers.
NOT_REPORTING = ha_states.HA_UNAVAILABLE_STATES

# How an entity failed to report. The distinction drives the message, because
# the two need different actions from a human.
ABSENT_UNAVAILABLE = "unavailable"  # still registered, not answering
ABSENT_GONE = "gone"                # no longer in Home Assistant at all


@dataclass
class ExpectedAbsence:
    """A battery device that is *supposed* to be silent, for a stated period.

    Copied deliberately from appdaemon_automatic_lights, same two mandatory
    fields and the same reasoning:

    `reason`  -- a suppression nobody can explain is a suppression nobody dares
                 remove, so it becomes permanent by default.
    `review`  -- past this date the entry stops suppressing and starts warning
                 about itself.

    This app needs it more than the lights app does. Measured 2026-09-04, this
    estate already has 17 ZHA battery end devices that last spoke between 27
    days and 2.0 years ago. Reporting all of them every day would be a wall of
    known-dead noise, and the one genuinely new death would be invisible in it.
    """

    entity_id: str
    reason: str
    review: date


@dataclass
class NotifyOutcome:
    """What a notification attempt actually achieved.

    T-52 exists because notifications were being discarded while the log said
    they had been sent. Fixing the delivery mechanism without fixing the
    reporting would leave the same lie in place, one layer down: the caller
    logged "Sent consolidated battery notification" unconditionally, and
    `_notify_persons` can send nothing four different ways.

    `held_quiet_hours` is kept apart from the rest deliberately. A held message
    is a decision working correctly; zero recipients for any other reason is a
    failure, and the two must not produce the same log line.
    """

    sent: int = 0
    held_quiet_hours: bool = False
    cooldown: int = 0
    no_address: int = 0
    failed: int = 0

    @property
    def reached_nobody(self) -> bool:
        """True when the message was meant to go out and did not."""
        return self.sent == 0 and not self.held_quiet_hours

    def summary(self) -> str:
        bits = [f"{self.sent} sent"]
        for n, label in ((self.cooldown, "in cooldown"),
                         (self.no_address, "no address"),
                         (self.failed, "failed")):
            if n:
                bits.append(f"{n} {label}")
        return ", ".join(bits)


@dataclass
class AbsentBattery:
    """One battery entity that is not reporting, and how long that has been so."""

    entity_id: str
    name: str
    kind: str          # ABSENT_UNAVAILABLE | ABSENT_GONE
    since: float       # epoch seconds, first time this app saw it absent

    def age_days(self, now_epoch: float) -> float:
        return max(0.0, (now_epoch - self.since) / 86400.0)


class BatteryCheck(hass.Hass):
    """
    Battery monitoring class for Home Assistant.

    Monitors battery levels of devices and sends notifications when batteries are low.
    Supports both percentage-based battery sensors and binary low battery indicators.

    Configuration:
        persons: List of persons to notify with their notification settings
        exclude: List of entity IDs to exclude from battery monitoring
        low_battery_threshold: Percentage below which battery is considered low (default: 20%)
        critical_battery_threshold: Percentage below which battery is critical (default: 10%)
        check_time: Time for daily battery check (default: "18:15:00")
        timezone: Timezone for scheduling (default: "Europe/Stockholm")
    """

    async def initialize(self) -> None:
        """Initialize the battery checker application."""
        self.log("Loading BatteryCheck()")

        # Get configuration from args with defaults
        self.persons = self.args.get("persons", [])
        self.exclude_list = self.args.get("exclude", [])
        self.low_battery_threshold = self.args.get("low_battery_threshold", 20)
        self.critical_battery_threshold = self.args.get("critical_battery_threshold", 10)
        self.check_time = self.args.get("check_time", "18:15:00")
        self.timezone_str = self.args.get("timezone", "Europe/Stockholm")

        # Android companion-app delivery settings. The default HA notification channel
        # can be disabled on the phone, which silently discards every notification sent
        # to it - HA reports success and nothing arrives. Sending on a dedicated channel
        # keeps these alerts independent of that setting and lets them be muted on
        # their own without affecting other apps. See backlog T-52.
        self.notification_channel = self.args.get("notification_channel", "battery_alerts")
        self.notification_priority = self.args.get("notification_priority", "high")

        # Policy D2 quiet hours. A low battery is never urgent enough to wake
        # the house - unlike a watchdog alert, there is no "news" case here, so
        # notifications inside the window are held until it opens. The existing
        # per-person `cooldown` still governs repeat frequency; this only adds
        # the nightly pause.
        self.quiet_hours = self.args.get("quiet_hours", True)
        self.quiet_start = self.args.get("quiet_start", policy.DEFAULT_QUIET_START)
        self.quiet_end = self.args.get("quiet_end", policy.DEFAULT_QUIET_END)

        # Initialize cooldown tracking
        self.msg_cooldown: Dict[str, float] = {}

        # --- Absent-device monitoring (O2) --------------------------------
        #
        # The original app only ever looked at batteries that were still
        # talking: `_check_battery_sensor` skipped anything `unavailable`, and
        # the binary path only counted `on`. That is precisely backwards for
        # the failure this is meant to catch. When a cell finally goes flat the
        # device does not report 1% -- it stops answering, its entities turn
        # `unavailable`, and it drops out of the check entirely. The louder the
        # failure, the quieter this app got.
        self.report_absent = self.args.get("report_absent", True)

        # Where the roster of previously-seen battery entities is kept, so a
        # device that vanishes from Home Assistant altogether is still noticed
        # and so an AppDaemon restart does not re-announce every standing
        # absence as news (the exact problem policy D2 names).
        self.roster_file = self.args.get(
            "roster_file", "/conf/battery_roster.json"
        )

        # Devices allowed to be silent, with a reason and a review date.
        self.expected_absent: Dict[str, ExpectedAbsence] = {}

        # Staleness: an entity that still reports a plausible number but has
        # not been updated in a long time. Default OFF, and that is a measured
        # decision rather than laziness -- see _check_stale().
        self.stale_after_days = float(self.args.get("stale_after_days", 0) or 0)

        # Loaded from disk in initialize(), rewritten after every check.
        self.absent_seen: Dict[str, AbsentBattery] = {}

        # Entities this app has seen report a real value at least once.
        #
        # This is what separates "a battery died" from "this entity never had a
        # value in the first place". Z-Wave devices publish a capability entity
        # for every property in their command classes, and the ones the hardware
        # does not actually implement sit at `unavailable` for life. Measured
        # 2026-09-04: `Motion Ute Norr` alone contributes nine of them --
        # overheating, fluid_is_low, rechargeable, used_as_backup,
        # battery_is_disconnected, battery_temperature_is_low, charging_status,
        # maximum_capacity, recharge_or_replace -- while the device itself is
        # perfectly alive and reporting 60%.
        #
        # An exclude list would work until the next device is added and then rot
        # silently. "Has it ever spoken?" needs no maintenance and is exactly the
        # question that distinguishes the two cases.
        self.seen_healthy: Dict[str, float] = {}

        # Validate configuration
        self._validate_configuration()
        self.expected_absent = self._load_expected_absent()
        self.absent_seen, self.seen_healthy = self._load_roster()

        # Set timezone
        try:
            self.timezone = pytz.timezone(self.timezone_str)
        except pytz.exceptions.UnknownTimeZoneError:
            self.log(f"Invalid timezone '{self.timezone_str}', using default 'Europe/Stockholm'", level="WARNING")
            self.timezone = timezone

        # Schedule daily battery check
        self.run_daily(self.daily_battery_check, self.check_time, timezone=self.timezone)

        # Register event listener for notification actions
        self.listen_event(self.phone_action, "mobile_app_notification_action")

        # Run initial battery check on startup
        await self.daily_battery_check()

    def _validate_configuration(self) -> None:
        """Validate the configuration and log warnings for missing or invalid settings."""
        if not self.persons:
            self.log("No persons configured for notifications", level="WARNING")

        for person in self.persons:
            if not person.get("notify"):
                self.log(f"Person '{person.get('name', 'Unknown')}' missing notify address", level="WARNING")

        if self.exclude_list:
            self.log(f"Excluding {len(self.exclude_list)} entities from battery monitoring: {self.exclude_list}")

        # Validate thresholds
        if self.low_battery_threshold <= self.critical_battery_threshold:
            self.log("Warning: low_battery_threshold should be higher than critical_battery_threshold", level="WARNING")

    async def daily_battery_check(self, kwargs: Optional[Dict[str, Any]] = None) -> None:
        """
        Daily battery check function that runs at the configured time.

        Args:
            kwargs: Optional keyword arguments passed by the scheduler
        """
        self.log("Running daily battery check...")
        states = await self.get_state()

        # Lists to collect devices with low battery
        critical_devices: List[str] = []
        low_devices: List[str] = []

        # Every battery entity present in this pass, reporting or not. Used to
        # decide which roster members have disappeared from Home Assistant.
        present: Dict[str, Dict[str, Any]] = {}
        not_reporting: Dict[str, Dict[str, Any]] = {}

        for entity_key in sorted(states):
            # Skip excluded entities
            if entity_key in self.exclude_list:
                self.log(f"Skipping excluded entity: {entity_key}", level="DEBUG")
                continue

            entity = states.get(entity_key)
            if not entity:
                continue

            attributes = entity.get("attributes", {})
            device_class = attributes.get("device_class")
            is_level = device_class == "battery"
            is_binary = (not is_level) and self._is_battery_binary_sensor(entity_key)

            if is_level or is_binary:
                present[entity_key] = entity
                if self._is_not_reporting(entity):
                    not_reporting[entity_key] = entity

            # Check for battery-related entities
            if is_level:
                self._check_battery_sensor(entity_key, entity, critical_devices, low_devices)

            # Check for binary sensors that indicate low battery
            elif is_binary:
                self._check_battery_binary_sensor(entity_key, entity, critical_devices, low_devices)

        absent_summary = ""
        if self.report_absent:
            absent_summary = self._check_absent(present, not_reporting)
        if self.stale_after_days > 0:
            self._check_stale(present)

        # Send consolidated notifications
        self._send_battery_notifications(critical_devices, low_devices, absent_summary)

    # ── Devices that stopped reporting (O2) ────────────────────────────

    @staticmethod
    def _is_not_reporting(entity: Dict[str, Any]) -> bool:
        """True when an entity is registered but is telling us nothing.

        This is the state a battery device lands in when the cell finally dies:
        not a low number, no number at all.
        """
        return ha_states.not_reporting(entity.get("state"))

    @staticmethod
    def _entity_name(entity: Dict[str, Any], entity_key: str) -> str:
        return (entity.get("attributes") or {}).get("friendly_name") or entity_key

    def _check_absent(
        self,
        present: Dict[str, Dict[str, Any]],
        not_reporting: Dict[str, Dict[str, Any]],
    ) -> str:
        """Classify silent battery devices and report the ones worth reporting.

        Three populations, and conflating them is what makes this kind of alert
        useless:

        **New** -- absent now, was not absent last run. This is the sudden death
        the whole feature exists for, and it is the only one that gets its own
        notification.

        **Standing** -- absent now and absent before. Counted, logged in full,
        but folded into one line of the daily message. Seventeen devices that
        died months ago must not drown out the one that died last night.

        **Returned** -- was absent, is reporting again. Worth saying, because it
        closes the loop after a battery change and because policy D2 forgets a
        cleared condition, so the next failure is news again.

        Returns the line to fold into the daily battery notification, or "".
        """
        now_epoch = datetime.now(self.timezone).timestamp()
        today = datetime.now(self.timezone).date()

        # An entity in the roster that is no longer in the state machine has
        # been removed from Home Assistant, not merely gone quiet. Different
        # cause (integration removed, device deleted, entity renamed) and a
        # different fix, so it is classified separately.
        gone = {
            eid: rec for eid, rec in self.absent_seen.items()
            if eid not in present and eid not in self.exclude_list
        }

        current: Dict[str, AbsentBattery] = {}
        new_absent: List[AbsentBattery] = []
        standing: List[AbsentBattery] = []
        suppressed: List[AbsentBattery] = []
        never_spoke: List[str] = []

        for eid, entity in sorted(not_reporting.items()):
            prior = self.absent_seen.get(eid)

            # Never once reported, and not already on the absent roster: this
            # is an unimplemented capability entity, not a battery that died.
            if prior is None and eid not in self.seen_healthy:
                never_spoke.append(eid)
                continue
            rec = AbsentBattery(
                entity_id=eid,
                name=self._entity_name(entity, eid),
                kind=ABSENT_UNAVAILABLE,
                since=prior.since if prior else now_epoch,
            )
            current[eid] = rec

            verdict, entry = self._absence_verdict(eid, today)
            if verdict == "expected":
                suppressed.append(rec)
                self.log(
                    "[BAT005] {} is silent as expected until {} ({})".format(
                        eid, entry.review, entry.reason
                    )
                )
                continue
            if verdict == "expired":
                self.log(
                    "[BAT006] {} is silent and its expected-absent entry expired "
                    "on {} ({}) -- decide: replace the battery, retire the "
                    "device, or extend the review date".format(
                        eid, entry.review, entry.reason
                    ),
                    level="WARNING",
                )

            (new_absent if prior is None else standing).append(rec)

        for eid, rec in sorted(gone.items()):
            rec = AbsentBattery(eid, rec.name, ABSENT_GONE, rec.since)
            current[eid] = rec
            verdict, entry = self._absence_verdict(eid, today)
            if verdict == "expected":
                suppressed.append(rec)
                continue
            self.log(
                "[BAT004] {} ({}) is no longer in Home Assistant at all -- last "
                "seen by this app {:.1f} day(s) ago. The entity was removed, "
                "renamed, or its integration is unloaded; a battery change will "
                "not bring it back.".format(
                    rec.name, eid, rec.age_days(now_epoch)
                ),
                level="ERROR",
            )
            (new_absent if rec.kind != self.absent_seen[eid].kind else standing).append(rec)

        # Anything that was absent and is now reporting again.
        returned = [
            rec for eid, rec in self.absent_seen.items()
            if eid not in current and eid in present
        ]

        for rec in new_absent:
            self.log(
                "[BAT001] {} ({}) has STOPPED REPORTING. A battery device that "
                "goes silent is the normal way a flat cell presents -- it does "
                "not count down to zero, it stops answering.".format(
                    rec.name, rec.entity_id
                ),
                level="ERROR",
            )
        for rec in returned:
            self.log("[BAT003] {} ({}) is reporting again".format(rec.name, rec.entity_id))
        if standing:
            self.log(
                "[BAT002] {} battery device(s) still silent: {}".format(
                    len(standing),
                    ", ".join(
                        "{} ({:.0f}d)".format(r.entity_id, r.age_days(now_epoch))
                        for r in standing
                    ),
                ),
                level="WARNING",
            )

        # An expected-absent entry for something that is now healthy is stale
        # bookkeeping, and stale suppressions are how a real fault gets hidden
        # later.
        for eid in self.expected_absent:
            if eid in present and eid not in not_reporting:
                self.log(
                    "[BAT007] {} is reporting normally but is still listed as "
                    "expected-absent ({}) -- remove the entry".format(
                        eid, self.expected_absent[eid].reason
                    ),
                    level="WARNING",
                )

        if never_spoke:
            self.log(
                "[BAT012] {} battery-shaped entit(ies) have never reported a "
                "value and are treated as unimplemented, not dead: {}".format(
                    len(never_spoke), ", ".join(never_spoke)
                ),
                level="DEBUG",
            )

        # Anything reporting right now has, by definition, spoken.
        for eid, entity in present.items():
            if eid not in not_reporting:
                self.seen_healthy[eid] = now_epoch

        self.absent_seen = current
        self._save_roster(current, self.seen_healthy)

        self._notify_absent(new_absent, returned)
        if standing or suppressed:
            return "{} sensor(er) tysta sedan tidigare{}".format(
                len(standing),
                " ({} kända/undantagna)".format(len(suppressed)) if suppressed else "",
            )
        return ""

    def _notify_absent(
        self, new_absent: List[AbsentBattery], returned: List[AbsentBattery]
    ) -> None:
        """Tell someone. A device dying quietly is the whole point of O2."""
        if not new_absent and not returned:
            return
        lines: List[str] = []
        if new_absent:
            lines.append("Slutade svara:")
            lines.extend(
                "• {}{}".format(
                    r.name,
                    " (borttagen ur Home Assistant)" if r.kind == ABSENT_GONE else "",
                )
                for r in new_absent
            )
        if returned:
            if lines:
                lines.append("")
            lines.append("Svarar igen:")
            lines.extend("• {}".format(r.name) for r in returned)
        outcome = self._notify_persons("Sensor tyst", "\n".join(lines), cooldown_key="absent")
        if outcome.reached_nobody:
            # O2 says no sensor dies without the owner knowing. An alert that
            # reached no one is that objective failing quietly, so it must not
            # pass without a line of its own.
            self.log(
                "[BAT013] sensor-silence alert reached NOBODY ({}) -- {} newly "
                "silent, {} returned".format(
                    outcome.summary(), len(new_absent), len(returned)
                ),
                level="ERROR",
            )

    def _check_stale(self, present: Dict[str, Dict[str, Any]]) -> None:
        """Report entities that still answer but have not been updated in a while.

        Off by default (`stale_after_days: 0`), and that is deliberate. Home
        Assistant only moves `last_updated` when the state or an attribute
        actually changes, so a healthy sensor sitting at 100% for two months is
        indistinguishable here from one that froze. Worse, a restart rewrites
        `last_updated` for every restored entity, so the measure is blind for a
        while after every restart. Enable it only with a threshold you have a
        reason for.

        The measured estate does not need it: all 17 long-dead ZHA devices show
        up as `unavailable`, which _check_absent catches reliably. This exists
        for the integration that fakes a last-known value instead.
        """
        now_epoch = datetime.now(self.timezone).timestamp()
        cutoff = self.stale_after_days * 86400.0
        for eid, entity in sorted(present.items()):
            if self._is_not_reporting(entity):
                continue
            if eid in self.expected_absent:
                continue
            updated = self._parse_ha_timestamp(entity.get("last_updated"))
            if updated is None:
                continue
            age = now_epoch - updated
            if age > cutoff:
                self.log(
                    "[BAT010] {} last updated {:.1f} day(s) ago, over the {:.0f} "
                    "day staleness threshold -- value may be frozen".format(
                        eid, age / 86400.0, self.stale_after_days
                    ),
                    level="WARNING",
                )

    @staticmethod
    def _parse_ha_timestamp(value) -> Optional[float]:
        """Home Assistant timestamps as epoch seconds, or None.

        Uses .timestamp() rather than subtracting datetimes: two aware
        datetimes that share a tzinfo still subtract naively in Python, which
        is wrong across a DST fold (T-07).
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None

    # ── Expected absence, and the roster that survives a restart ───────

    def _load_expected_absent(self) -> Dict[str, ExpectedAbsence]:
        """Parse `expected_absent`, rejecting entries loudly rather than silently.

        Per D1 (fail loud) a malformed entry is dropped and reported. Dropping
        it means the device goes back to alerting normally, which is the safe
        direction: a broken suppression should make noise, not silence.
        """
        raw = self.args.get("expected_absent") or {}
        if not isinstance(raw, dict):
            self.log(
                "[BAT008] expected_absent must be a mapping of entity_id to "
                "{{reason, review}}, got {} -- ignoring all of it".format(
                    type(raw).__name__
                ),
                level="ERROR",
            )
            return {}

        parsed: Dict[str, ExpectedAbsence] = {}
        for entity_id, spec in raw.items():
            if not isinstance(spec, dict):
                self.log(
                    "[BAT008] expected_absent['{}'] must be a mapping with reason "
                    "and review, got {} -- entry ignored".format(
                        entity_id, type(spec).__name__
                    ),
                    level="ERROR",
                )
                continue

            reason = str(spec.get("reason") or "").strip()
            if not reason:
                self.log(
                    "[BAT008] expected_absent['{}'] has no reason -- entry "
                    "ignored. A suppression nobody can explain is one nobody "
                    "dares remove.".format(entity_id),
                    level="ERROR",
                )
                continue

            review_raw = spec.get("review")
            if review_raw is None:
                self.log(
                    "[BAT008] expected_absent['{}'] has no review date -- entry "
                    "ignored. Without one the suppression is permanent.".format(
                        entity_id
                    ),
                    level="ERROR",
                )
                continue

            review = self._parse_review_date(entity_id, review_raw)
            if review is None:
                continue

            parsed[entity_id] = ExpectedAbsence(entity_id, reason, review)

        if parsed:
            self.log(
                "[BAT009] {} expected-absent entrie(s): {}".format(
                    len(parsed),
                    ", ".join(
                        "{} until {}".format(e.entity_id, e.review)
                        for e in parsed.values()
                    ),
                )
            )
        return parsed

    def _parse_review_date(self, entity_id: str, value) -> Optional[date]:
        """Accept a real date from YAML, or an ISO string. Reject anything else."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip())
        except (ValueError, TypeError):
            self.log(
                "[BAT008] expected_absent['{}'] review '{}' is not a YYYY-MM-DD "
                "date -- entry ignored".format(entity_id, value),
                level="ERROR",
            )
            return None

    def _absence_verdict(
        self, entity_id: str, today: date
    ) -> Tuple[str, Optional[ExpectedAbsence]]:
        """Classify a silent entity: 'unexpected', 'expected', or 'expired'."""
        entry = self.expected_absent.get(entity_id)
        if entry is None:
            return "unexpected", None
        if today > entry.review:
            return "expired", entry
        return "expected", entry

    def _load_roster(self) -> Tuple[Dict[str, AbsentBattery], Dict[str, float]]:
        """Read the previously-absent set, and the ever-reported set, from disk.

        Without this, every AppDaemon restart re-announces every standing
        absence as if it had just happened -- the "redundant restart-time
        re-alerts" failure policy D2 names explicitly. It also makes the
        difference between "not reporting" and "gone from Home Assistant
        entirely" observable at all, since a removed entity leaves no trace in
        the state machine to compare against.

        A missing or unreadable file is not fatal: the app starts with an empty
        roster, which means the first run treats every current absence as news
        once, and is correct from then on.
        """
        try:
            with open(self.roster_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            self.log(
                "[BAT011] no roster at {} yet -- this run learns which entities "
                "report, and reports absences from the next run on".format(
                    self.roster_file
                )
            )
            return {}, {}
        except (OSError, ValueError) as err:
            self.log(
                "[BAT011] could not read roster {}: {} -- continuing with an "
                "empty roster".format(self.roster_file, err),
                level="WARNING",
            )
            return {}, {}

        raw = raw or {}
        absent_raw = raw.get("absent", {})
        healthy_raw = raw.get("seen_healthy", {})

        roster: Dict[str, AbsentBattery] = {}
        for eid, rec in absent_raw.items():
            try:
                roster[eid] = AbsentBattery(
                    entity_id=eid,
                    name=rec.get("name", eid),
                    kind=rec.get("kind", ABSENT_UNAVAILABLE),
                    since=float(rec.get("since", 0.0)),
                )
            except (AttributeError, TypeError, ValueError):
                self.log(
                    "[BAT011] roster entry for {} is malformed -- ignored".format(eid),
                    level="WARNING",
                )

        healthy: Dict[str, float] = {}
        for eid, when in healthy_raw.items():
            try:
                healthy[eid] = float(when)
            except (TypeError, ValueError):
                continue
        return roster, healthy

    def _save_roster(
        self, roster: Dict[str, AbsentBattery], healthy: Dict[str, float]
    ) -> None:
        """Persist both sets. Failure to write must not take the app down."""
        payload = {
            "absent": {
                eid: {"name": r.name, "kind": r.kind, "since": r.since}
                for eid, r in roster.items()
            },
            "seen_healthy": healthy,
        }
        tmp = "{}.tmp".format(self.roster_file)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.roster_file)
        except OSError as err:
            self.log(
                "[BAT011] could not write roster {}: {} -- absences will be "
                "re-announced after the next restart".format(self.roster_file, err),
                level="WARNING",
            )

    def _is_battery_binary_sensor(self, entity_key: str) -> bool:
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

    def _check_battery_sensor(self, entity_key: str, entity: Dict[str, Any],
                            critical_devices: List[str], low_devices: List[str]) -> None:
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
        uof = attributes.get("unit_of_measurement", "")

        # Skip non-battery level sensors (like charging_status)
        skip_terms = ["charging_status", "recharge", "power"]
        if any(term in entity_key.lower() for term in skip_terms):
            return

        # The shared predicate, replacing a private two-state list that let ""
        # through to a float() call (S7-07).
        if ha_states.is_reporting(state):
            self.log(f"* {entity_key} = {state}{uof}")
            self._evaluate_battery_level(entity_key, str(state), attributes, critical_devices, low_devices)

    def _check_battery_binary_sensor(self, entity_key: str, entity: Dict[str, Any],
                                   critical_devices: List[str], low_devices: List[str]) -> None:
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

        if state == "on" and state is not None:  # "on" means low battery is detected
            device_name = attributes.get("friendly_name", entity_key)

            # Check if it's a critical battery sensor (contains "islow")
            if "islow" in entity_key.lower():
                critical_devices.append(f"• {device_name}: KRITISK LÅG BATTERI")
                self.log(f"Critical battery detected for {device_name} (binary sensor)")
            else:
                low_devices.append(f"• {device_name}: Lågt batteri")
                self.log(f"Low battery detected for {device_name} (binary sensor)")

    def _evaluate_battery_level(self, entity_key: str, state: str, attributes: Dict[str, Any],
                              critical_devices: List[str], low_devices: List[str]) -> None:
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

            # Get device name from attributes
            device_name = attributes.get("friendly_name", entity_key)

            # Check if battery is low and add to appropriate list
            if battery_level <= self.critical_battery_threshold:
                critical_devices.append(f"• {device_name}: {battery_level}%")
                self.log(f"Critical battery detected for {device_name}: {battery_level}%")
            elif battery_level <= self.low_battery_threshold:
                low_devices.append(f"• {device_name}: {battery_level}%")
                self.log(f"Low battery detected for {device_name}: {battery_level}%")

        except (ValueError, TypeError):
            # Handle cases where state is not a number
            self.log(f"Could not parse battery level for {entity_key}: {state}", level="DEBUG")

    def _send_battery_notifications(self, critical_devices: List[str], low_devices: List[str],
                                    absent_summary: str = "") -> None:
        """
        Send consolidated battery notifications.

        Args:
            critical_devices: List of devices with critical battery levels
            low_devices: List of devices with low battery levels
            absent_summary: One line about devices that have been silent since
                a previous run. Standing absences ride along with the daily
                message rather than getting their own alert -- a newly silent
                device is notified separately and immediately by _notify_absent,
                and the two must not compete for attention.
        """
        if not critical_devices and not low_devices:
            self.log("No low battery devices found")
            if absent_summary:
                self.log("Standing absences: {}".format(absent_summary))
            return

        # Build the message
        message_parts: List[str] = []

        if critical_devices:
            message_parts.append("🚨 KRITISK LÅG BATTERI:")
            message_parts.extend(critical_devices)
            message_parts.append("")  # Empty line for spacing

        if low_devices:
            message_parts.append("⚠️ Lågt batteri:")
            message_parts.extend(low_devices)

        if absent_summary:
            message_parts.append("")
            message_parts.append("🔇 {}".format(absent_summary))

        # Join all parts into one message
        full_message = "\n".join(message_parts)

        # Send the consolidated notification, and report what actually happened.
        #
        # This used to log "Sent consolidated battery notification ..."
        # unconditionally, immediately after a call that can send nothing four
        # ways. That is the same defect T-52 was opened for -- a log asserting a
        # delivery it never established -- reproduced one layer up.
        outcome = self._notify_persons("Batterivarning", full_message)
        detail = f"{len(critical_devices)} critical and {len(low_devices)} low battery devices"
        if outcome.held_quiet_hours:
            self.log(f"Battery notification held for quiet hours ({detail})")
        elif outcome.reached_nobody:
            self.log(
                f"Battery notification reached NOBODY ({outcome.summary()}) -- "
                f"{detail} went unreported",
                level="WARNING",
            )
        else:
            self.log(f"Battery notification: {outcome.summary()}; {detail}")

    def _notification_data(self) -> dict:
        """Build the companion-app data block for a notification.

        Returns the Android delivery hints every notify call in this app must carry:
        a dedicated channel, plus priority/ttl so the message is not deferred by Doze.
        Returns an empty dict if no channel is configured, so the caller can pass it
        unconditionally.
        """
        if not self.notification_channel:
            return {}
        data = {"channel": self.notification_channel}
        if self.notification_priority:
            data["priority"] = self.notification_priority
            data["ttl"] = 0
        return data

    def _in_quiet_hours(self) -> bool:
        """True when notifications should be held. Uses AppDaemon's clock."""
        if not self.quiet_hours:
            return False
        # datetime.now(self.timezone), NOT self.get_now().
        #
        # This app declares `async def initialize()`, so AppDaemon's get_now()
        # returns a coroutine rather than a datetime -- ".hour" on it raises
        # AttributeError: '_asyncio.Task' object has no attribute 'hour', which
        # fails initialize() and takes the whole app down.
        #
        # This method is sync and sits three frames below an async caller, so
        # awaiting is not available. self.timezone is a pytz timezone set in
        # initialize() and already used elsewhere in this file, so the clock
        # stays timezone-aware (T-07).
        #
        # The identical code IS correct in appdaemon_watchdog, which is a sync
        # app. Copying it here without checking is what broke this.
        return policy.in_quiet_hours(
            datetime.now(self.timezone).hour, self.quiet_start, self.quiet_end
        )

    def _notify_persons(self, title: str, message: str, cooldown_key: str = "") -> NotifyOutcome:
        """
        Send notification to all configured persons.

        Args:
            title: Notification title
            message: Notification message
            cooldown_key: Distinguishes independent conditions so they do not
                consume each other's cooldown. Without it the absence alert and
                the low-battery digest, both sent within the same second of the
                same run, would collide: the first would set the per-address
                cooldown and the second would be silently dropped for the next
                `cooldown` seconds. Empty string keeps the original key, so the
                low-battery digest and its existing "Ignorera 3d" action are
                unchanged.
        """
        outcome = NotifyOutcome()
        if self._in_quiet_hours():
            self.log(
                f"Holding battery notification until {self.quiet_start:02d}:00-"
                f"{self.quiet_end:02d}:00 quiet hours end",
                level="INFO",
            )
            outcome.held_quiet_hours = True
            return outcome
        for person in self.persons:
            notify_addr = person.get("notify")
            cooldown = person.get("cooldown", 0)

            # Validate notify_addr exists
            if not notify_addr:
                self.log(f"Missing notify address for person: {person.get('name', 'Unknown')}", level="WARNING")
                outcome.no_address += 1
                continue

            key = f"{notify_addr}|{cooldown_key}" if cooldown_key else notify_addr

            # Check cooldown (avoid division by zero)
            if cooldown > 0:
                time_since_last = time.time() - self.msg_cooldown.get(key, 0)
                if time_since_last < int(cooldown):
                    self.log(f"Cooldown activated for {key}, last msg sent {time_since_last:.0f}s ago", level="DEBUG")
                    outcome.cooldown += 1
                    continue

            # Send notification
            try:
                self.call_service(
                    f"notify/{notify_addr}",
                    title=title,
                    message=message,
                    data={
                        **self._notification_data(),
                        "actions": [{
                            "action": f"{self.name}.ignore.{key}",
                            "title": "Ignorera 3d"
                        }]
                    }
                )
                self.msg_cooldown[key] = time.time()
                self.log(f"Notification sent to {notify_addr}", level="INFO")
                outcome.sent += 1
            except Exception as e:
                self.log(f"Failed to send notification to {notify_addr}: {e}", level="ERROR")
                outcome.failed += 1
        return outcome

    def phone_action(self, event_name: str, data: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """
        Handle notification action responses.

        Args:
            event_name: The event name
            data: Event data containing action information
            kwargs: Additional keyword arguments
        """
        action = str(data.get("action", "")).split(".")
        if len(action) < 3 or action[0] != self.name:
            return

        if action[1] == "ignore":
            try:
                dt_now = datetime.now(self.timezone)
                future_time = datetime(dt_now.year, dt_now.month, dt_now.day, tzinfo=self.timezone) + timedelta(days=3)
                self.msg_cooldown[action[2]] = future_time.timestamp()
                self.log(f"IGNORE {action[2]} until {future_time.strftime('%Y-%m-%d %H:%M')}")
            except Exception as e:
                self.log(f"Error processing ignore action: {e}", level="ERROR")



