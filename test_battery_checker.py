#!/usr/bin/env python3
"""
Simple test script for the battery checker functionality.

This script tests the core logic of the battery checker without requiring AppDaemon.
"""

import sys
from typing import List, Dict, Any, Tuple


class MockBatteryCheck:
    """Mock version of BatteryCheck for testing purposes."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize with test configuration."""
        self.persons = config.get("persons", [])
        self.exclude_list = config.get("exclude", [])
        self.low_battery_threshold = config.get("low_battery_threshold", 20)
        self.critical_battery_threshold = config.get("critical_battery_threshold", 10)

    def _is_battery_binary_sensor(self, entity_key: str) -> bool:
        """Check if entity is a battery-related binary sensor."""
        if not entity_key.startswith("binary_sensor."):
            return False
        battery_terms = ["battery", "batt", "islow", "low_battery"]
        return any(term in entity_key.lower() for term in battery_terms)

    def _check_battery_sensor(self, entity_key: str, entity: Dict[str, Any],
                            critical_devices: List[str], low_devices: List[str]) -> None:
        """Check a percentage-based battery sensor for low battery conditions."""
        state = entity.get("state")
        attributes = entity.get("attributes", {})

        # Skip non-battery level sensors
        skip_terms = ["charging_status", "recharge", "power"]
        if any(term in entity_key.lower() for term in skip_terms):
            return

        if state not in ["unavailable", "unknown"] and state is not None:
            self._evaluate_battery_level(entity_key, str(state), attributes, critical_devices, low_devices)

    def _check_battery_binary_sensor(self, entity_key: str, entity: Dict[str, Any],
                                   critical_devices: List[str], low_devices: List[str]) -> None:
        """Check a binary sensor for low battery indication."""
        state = entity.get("state")
        attributes = entity.get("attributes", {})

        if state == "on" and state is not None:
            device_name = attributes.get("friendly_name", entity_key)
            if "islow" in entity_key.lower():
                critical_devices.append(f"• {device_name}: KRITISK LÅG BATTERI")
            else:
                low_devices.append(f"• {device_name}: Lågt batteri")

    def _evaluate_battery_level(self, entity_key: str, state: str, attributes: Dict[str, Any],
                              critical_devices: List[str], low_devices: List[str]) -> None:
        """Evaluate battery level and categorize as critical or low."""
        try:
            battery_level = float(state)
            device_name = attributes.get("friendly_name", entity_key)

            if battery_level <= self.critical_battery_threshold:
                critical_devices.append(f"• {device_name}: {battery_level}%")
            elif battery_level <= self.low_battery_threshold:
                low_devices.append(f"• {device_name}: {battery_level}%")
        except (ValueError, TypeError):
            pass

    def check_batteries(self, states: Dict[str, Dict[str, Any]]) -> Tuple[List[str], List[str]]:
        """Main battery check function."""
        critical_devices: List[str] = []
        low_devices: List[str] = []

        for entity_key in sorted(states):
            # Skip excluded entities
            if entity_key in self.exclude_list:
                print(f"SKIPPING excluded entity: {entity_key}")
                continue

            entity = states.get(entity_key)
            if not entity:
                continue

            attributes = entity.get("attributes", {})
            device_class = attributes.get("device_class")

            if device_class == "battery":
                self._check_battery_sensor(entity_key, entity, critical_devices, low_devices)
            elif self._is_battery_binary_sensor(entity_key):
                self._check_battery_binary_sensor(entity_key, entity, critical_devices, low_devices)

        return critical_devices, low_devices


def test_exclude_functionality():
    """Test the exclude functionality."""
    print("Testing exclude functionality...")

    # Test configuration with exclude list
    config = {
        "persons": [{"name": "test", "notify": "test_notify"}],
        "exclude": ["sensor.louies_iphone_2028", "sensor.excluded_device"],
        "low_battery_threshold": 20,
        "critical_battery_threshold": 10
    }

    # Mock Home Assistant states
    states = {
        "sensor.louies_iphone_2028": {
            "state": "15",
            "attributes": {"friendly_name": "Louie's iPhone", "device_class": "battery"}
        },
        "sensor.excluded_device": {
            "state": "5",
            "attributes": {"friendly_name": "Excluded Device", "device_class": "battery"}
        },
        "sensor.test_device": {
            "state": "25",
            "attributes": {"friendly_name": "Test Device", "device_class": "battery"}
        },
        "sensor.low_battery_device": {
            "state": "15",
            "attributes": {"friendly_name": "Low Battery Device", "device_class": "battery"}
        },
        "binary_sensor.test_low_battery": {
            "state": "on",
            "attributes": {"friendly_name": "Test Low Battery Binary"}
        }
    }

    checker = MockBatteryCheck(config)
    critical_devices, low_devices = checker.check_batteries(states)

    print(f"Critical devices: {critical_devices}")
    print(f"Low devices: {low_devices}")

    # Verify that excluded devices are not in the results
    excluded_found = any("Louie's iPhone" in device for device in critical_devices + low_devices)
    excluded_found |= any("Excluded Device" in device for device in critical_devices + low_devices)

    if not excluded_found:
        print("✅ Exclude functionality working correctly - excluded devices not found in results")
    else:
        print("❌ Exclude functionality failed - excluded devices found in results")
        return False

    # Verify that non-excluded devices are in the results
    if any("Low Battery Device" in device for device in low_devices):
        print("✅ Non-excluded devices correctly included in results")
    else:
        print("❌ Non-excluded devices not found in results")
        return False

    return True


def test_threshold_configuration():
    """Test configurable thresholds."""
    print("\nTesting configurable thresholds...")

    config = {
        "persons": [{"name": "test", "notify": "test_notify"}],
        "exclude": [],
        "low_battery_threshold": 30,  # Custom threshold
        "critical_battery_threshold": 15  # Custom threshold
    }

    states = {
        "sensor.device_25": {
            "state": "25",
            "attributes": {"friendly_name": "Device 25%", "device_class": "battery"}
        },
        "sensor.device_10": {
            "state": "10",
            "attributes": {"friendly_name": "Device 10%", "device_class": "battery"}
        }
    }

    checker = MockBatteryCheck(config)
    critical_devices, low_devices = checker.check_batteries(states)

    # Device with 25% should be low (below 30% threshold)
    # Device with 10% should be critical (below 15% threshold)

    low_found = any("Device 25%" in device for device in low_devices)
    critical_found = any("Device 10%" in device for device in critical_devices)

    if low_found and critical_found:
        print("✅ Custom thresholds working correctly")
        return True
    else:
        print("❌ Custom thresholds not working correctly")
        print(f"Low devices: {low_devices}")
        print(f"Critical devices: {critical_devices}")
        return False


if __name__ == "__main__":
    print("Running battery checker tests...")

    success = True
    success &= test_exclude_functionality()
    success &= test_threshold_configuration()

    if success:
        print("\n🎉 All tests passed!")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed!")
        sys.exit(1)