"""Unit tests for HiveCollector against a fake Hive session."""

import pytest
from conftest import (
    FakeHeating,
    FakeHotwater,
    FakeLight,
    FakeSensor,
    FakeSwitch,
    FakeAttributes,
    make_device,
    sample_map,
    scrape,
)

from hive_exporter.collector import HiveCollector, to_bool01, to_float


class TestCoercions:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            (21.5, 21.5),
            ("21.5", 21.5),
            (True, 1.0),
            (False, 0.0),
            ("not-a-number", None),
        ],
    )
    def test_to_float(self, value, expected):
        assert to_float(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            (True, 1.0),
            (False, 0.0),
            ("ON", 1.0),
            ("off", 0.0),
            ("OPEN", 1.0),
            ("CLOSED", 0.0),
            ("HEAT", 1.0),
            ("IDLE", 0.0),
            (1, 1.0),
            (0, 0.0),
            ("mystery", None),
        ],
    )
    def test_to_bool01(self, value, expected):
        assert to_bool01(value) == expected


class TestClimate:
    def make_hive(self, hive, status=None, min_temp=7.0, max_temp=32.0):
        device = make_device("therm1", "Living Room", "heating")
        hive.device_list = {"climate": [device]}
        hive.heating = FakeHeating(
            {
                "therm1": {
                    "status": status,
                    "min_temp": min_temp,
                    "max_temp": max_temp,
                }
            }
        )
        hive.attr = FakeAttributes(
            online={device.device_id: True}, batteries={device.device_id: 85}
        )
        hive.config.battery = {device.device_id}
        return device

    def test_temperatures_and_state(self, hive):
        self.make_hive(
            hive,
            status={
                "current_temperature": 19.5,
                "target_temperature": 21.0,
                "action": True,
                "mode": "SCHEDULE",
                "boost": "OFF",
            },
        )
        collector = HiveCollector(hive)

        labels = (("id", "therm1"), ("name", "Living Room"))
        assert sample_map(
            collector, "hive_heating_current_temperature_celsius"
        ) == {labels: 19.5}
        assert sample_map(
            collector, "hive_heating_target_temperature_celsius"
        ) == {labels: 21.0}
        assert sample_map(collector, "hive_heating_min_temperature_celsius") == {
            labels: 7.0
        }
        assert sample_map(collector, "hive_heating_max_temperature_celsius") == {
            labels: 32.0
        }
        assert sample_map(collector, "hive_heating_active") == {labels: 1.0}
        assert sample_map(collector, "hive_heating_boost_active") == {labels: 0.0}

    def test_mode_is_one_hot(self, hive):
        self.make_hive(hive, status={"mode": "MANUAL"})
        collector = HiveCollector(hive)

        modes = sample_map(collector, "hive_heating_mode")
        expected = {"SCHEDULE": 0.0, "MANUAL": 1.0, "OFF": 0.0}
        assert {
            dict(labels)["mode"]: value for labels, value in modes.items()
        } == expected

    def test_unknown_mode_still_reported(self, hive):
        self.make_hive(hive, status={"mode": "HOLIDAY"})
        collector = HiveCollector(hive)

        modes = {
            dict(labels)["mode"]: value
            for labels, value in sample_map(collector, "hive_heating_mode").items()
        }
        assert modes["HOLIDAY"] == 1.0
        assert modes["SCHEDULE"] == 0.0

    def test_offline_device_skips_temperature_but_reports_online(self, hive):
        device = self.make_hive(
            hive,
            status={
                "current_temperature": None,
                "target_temperature": None,
                "action": None,
                "mode": None,
                "boost": None,
            },
            min_temp=None,
            max_temp=None,
        )
        hive.attr = FakeAttributes(online={device.device_id: False})
        collector = HiveCollector(hive)

        assert sample_map(collector, "hive_heating_current_temperature_celsius") == {}
        online = sample_map(collector, "hive_device_online")
        assert list(online.values()) == [0.0]

    def test_battery_and_online_labels(self, hive):
        self.make_hive(hive, status={"mode": "SCHEDULE"})
        collector = HiveCollector(hive)

        labels = (("id", "therm1"), ("name", "Living Room"), ("type", "heating"))
        assert sample_map(collector, "hive_device_online") == {labels: 1.0}
        assert sample_map(collector, "hive_device_battery_percent") == {labels: 85.0}

    def test_no_battery_metric_for_mains_powered_devices(self, hive):
        device = self.make_hive(hive, status={"mode": "SCHEDULE"})
        hive.config.battery = set()
        collector = HiveCollector(hive)

        assert sample_map(collector, "hive_device_battery_percent") == {}


class TestHotwater:
    def make_hive(self, hive, mode="SCHEDULE", state="ON", boost="OFF"):
        device = make_device("hw1", "Hot Water", "hotwater")
        hive.device_list = {"water_heater": [device]}
        hive.hotwater = FakeHotwater(mode=mode, state=state, boost=boost)
        hive.attr = FakeAttributes(online={device.device_id: True})
        return device

    def test_state_mode_and_boost(self, hive):
        self.make_hive(hive)
        collector = HiveCollector(hive)

        labels = (("id", "hw1"), ("name", "Hot Water"))
        assert sample_map(collector, "hive_hotwater_active") == {labels: 1.0}
        assert sample_map(collector, "hive_hotwater_boost_active") == {labels: 0.0}
        modes = {
            dict(l)["mode"]: value
            for l, value in sample_map(collector, "hive_hotwater_mode").items()
        }
        assert modes == {"SCHEDULE": 1.0, "ON": 0.0, "OFF": 0.0}


class TestPlugsLightsSensors:
    def test_plug(self, hive):
        device = make_device("plug1", "TV Plug", "activeplug")
        hive.device_list = {"switch": [device]}
        hive.switch = FakeSwitch(state=True, power=42.5)
        collector = HiveCollector(hive)

        labels = (("id", "plug1"), ("name", "TV Plug"))
        assert sample_map(collector, "hive_plug_on") == {labels: 1.0}
        assert sample_map(collector, "hive_plug_power_watts") == {labels: 42.5}

    def test_light(self, hive):
        device = make_device("light1", "Hall Light", "warmwhitelight")
        hive.device_list = {"light": [device]}
        hive.light = FakeLight(state="ON", brightness=60)
        collector = HiveCollector(hive)

        labels = (("id", "light1"), ("name", "Hall Light"))
        assert sample_map(collector, "hive_light_on") == {labels: 1.0}
        assert sample_map(collector, "hive_light_brightness") == {labels: 60.0}

    def test_binary_sensor(self, hive):
        motion = make_device("m1", "Hall Motion", "motionsensor")
        contact = make_device("c1", "Front Door", "contactsensor")
        hive.device_list = {"binary_sensor": [motion, contact]}
        hive.sensor = FakeSensor({"m1": True, "c1": "CLOSED"})
        collector = HiveCollector(hive)

        values = sample_map(collector, "hive_sensor_active")
        by_id = {dict(labels)["id"]: value for labels, value in values.items()}
        assert by_id == {"m1": 1.0, "c1": 0.0}

    def test_availability_sensor_not_exported_as_sensor_active(self, hive):
        avail = make_device("a1", "Hub", "Connectivity")
        hive.device_list = {"binary_sensor": [avail]}
        hive.sensor = FakeSensor({"a1": True})
        hive.attr = FakeAttributes(online={avail.device_id: True})
        collector = HiveCollector(hive)

        result = scrape(collector)
        assert result.get("hive_sensor_active", {}) == {}
        assert list(result["hive_device_online"].values()) == [1.0]

    def test_non_plug_switch_skipped(self, hive):
        virtual = make_device("hod1", "Heating Zone 1", "Heating_Heat_On_Demand")
        hive.device_list = {"switch": [virtual]}
        hive.switch = FakeSwitch(state=True, power=None)
        collector = HiveCollector(hive)

        assert sample_map(collector, "hive_plug_on") == {}


class TestScrapeHealth:
    def test_up_and_duration(self, hive):
        collector = HiveCollector(hive)
        assert list(sample_map(collector, "hive_up").values()) == [1.0]
        (duration,) = sample_map(collector, "hive_scrape_duration_seconds").values()
        assert duration >= 0.0

    def test_update_data_called_once_per_scrape(self, hive):
        hive.device_list = {"climate": [make_device("t1", "T", "heating")]}
        hive.heating = FakeHeating({"t1": {"status": {}}})
        collector = HiveCollector(hive)
        list(collector.collect())
        assert hive.update_calls == 1

    def test_poll_failure_sets_up_zero_and_counts_error(self, hive):
        hive.device_list = {"climate": [make_device("t1", "T", "heating")]}
        hive.heating = FakeHeating({"t1": {"status": {"current_temperature": 18.0}}})
        hive.update_error = ConnectionError("api down")
        collector = HiveCollector(hive)

        result = scrape(collector)
        assert list(result["hive_up"].values()) == [0.0]
        assert list(result["hive_scrape_errors_total"].values()) == [1.0]
        # Cached device data is still exported even when the poll fails.
        temps = result["hive_heating_current_temperature_celsius"]
        assert list(temps.values()) == [18.0]

    def test_one_broken_device_does_not_break_others(self, hive):
        good = make_device("good", "Good", "heating")
        bad = make_device("bad", "Bad", "heating")
        hive.device_list = {"climate": [bad, good]}
        # "bad" is missing from the fake's data, so get_climate raises KeyError.
        hive.heating = FakeHeating(
            {"good": {"status": {"current_temperature": 20.0}}}
        )
        collector = HiveCollector(hive)

        result = scrape(collector)
        temps = result["hive_heating_current_temperature_celsius"]
        assert {dict(labels)["id"] for labels in temps} == {"good"}
        assert list(result["hive_scrape_errors_total"].values()) == [1.0]

    def test_errors_accumulate_across_scrapes(self, hive):
        hive.device_list = {"climate": [make_device("t1", "T", "heating")]}
        hive.heating = FakeHeating({"t1": {"status": {}}})
        hive.update_error = ConnectionError("api down")
        collector = HiveCollector(hive)
        list(collector.collect())
        list(collector.collect())
        errors = sample_map(collector, "hive_scrape_errors_total")
        assert list(errors.values()) == [3.0]
