"""Test doubles mirroring the surface of pyhiveapi that the collector uses."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def make_device(hive_id, name, hive_type, device_id=None, **extra):
    return SimpleNamespace(
        hive_id=hive_id,
        hive_name=name,
        ha_name=name,
        hive_type=hive_type,
        device_id=device_id or f"{hive_id}-dev",
        device_name=name,
        device_data={},
        status=None,
        min_temp=None,
        max_temp=None,
        **extra,
    )


class FakeHeating:
    def __init__(self, climates):
        # climates: {hive_id: {"status": {...}, "min_temp": x, "max_temp": y}}
        self.climates = climates

    def get_climate(self, device):
        data = self.climates[device.hive_id]
        device.status = data.get("status")
        device.min_temp = data.get("min_temp")
        device.max_temp = data.get("max_temp")
        return device


class FakeHotwater:
    def __init__(self, mode=None, state=None, boost=None):
        self.mode = mode
        self.state = state
        self.boost = boost

    def get_water_heater(self, device):
        device.status = {"current_operation": self.mode}
        return device

    def get_state(self, device):
        return self.state

    def get_boost_status(self, device):
        return self.boost


class FakeSwitch:
    def __init__(self, state=None, power=None):
        self.state = state
        self.power = power

    def get_state(self, device):
        return self.state

    def get_power_usage(self, device):
        return self.power


class FakeLight:
    def __init__(self, state=None, brightness=None):
        self.state = state
        self.brightness = brightness

    def get_state(self, device):
        return self.state

    def get_brightness(self, device):
        return self.brightness


class FakeSensor:
    def __init__(self, states):
        # states: {hive_id: state}
        self.states = states

    def get_sensor(self, device):
        device.status = {"state": self.states.get(device.hive_id)}
        return device


class FakeAttributes:
    def __init__(self, online=None, batteries=None):
        # online: {device_id: bool}; batteries: {device_id: level}
        self.online = online or {}
        self.batteries = batteries or {}

    def online_offline(self, device_id):
        return self.online.get(device_id)

    def get_battery(self, device_id):
        return self.batteries.get(device_id)


class FakeHive:
    def __init__(self):
        self.device_list = {}
        self.heating = FakeHeating({})
        self.hotwater = FakeHotwater()
        self.switch = FakeSwitch()
        self.light = FakeLight()
        self.sensor = FakeSensor({})
        self.attr = FakeAttributes()
        self.config = SimpleNamespace(battery=set())
        self.update_calls = 0
        self.update_error = None

    def update_data(self, device):
        self.update_calls += 1
        if self.update_error is not None:
            raise self.update_error
        return True


@pytest.fixture
def hive():
    return FakeHive()


def scrape(collector):
    """Run one collect() and return {metric_name: {label_tuple: value}}."""
    result = {}
    for family in collector.collect():
        for sample in family.samples:
            result.setdefault(sample.name, {})[
                tuple(sorted(sample.labels.items()))
            ] = sample.value
    return result


def sample_map(collector, metric_name):
    """Run one collect() and return {label_tuple: value} for one metric."""
    return scrape(collector).get(metric_name, {})
