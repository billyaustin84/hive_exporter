"""Prometheus collector that reads device state from a Hive session.

Works against the sync ``pyhiveapi`` API (PyPI package ``pyhive-integration``).
Published releases expose camelCase methods and dict devices
(``hive.deviceList``, ``heating.getClimate``); the library's master branch has
moved to snake_case methods and ``Device`` dataclasses. The small compat
helpers below accept either, so the collector keeps working across library
versions.
"""

import logging
import time

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

_LOGGER = logging.getLogger(__name__)

HEATING_MODES = ("SCHEDULE", "MANUAL", "OFF")
HOTWATER_MODES = ("SCHEDULE", "ON", "OFF")

# Entity types that mirror a device's online state; hive_device_online
# already covers them, so they are not exported as hive_sensor_active.
_AVAILABILITY_TYPES = {"availability", "connectivity"}

_ON_STATES = {"on", "true", "open", "heat", "heating", "motion", "detected"}
_OFF_STATES = {"off", "false", "closed", "idle", "none", "clear"}


def to_float(value):
    """Coerce a Hive attribute to float; None if missing or non-numeric."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool01(value):
    """Coerce an on/off-ish Hive state to 1.0/0.0; None if unrecognised."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return 1.0 if value else 0.0
    text = str(value).strip().lower()
    if text in _ON_STATES:
        return 1.0
    if text in _OFF_STATES:
        return 0.0
    return None


def _method(obj, *names):
    """Return the first callable attribute among camelCase/snake_case names."""
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            return fn
    raise AttributeError(f"{obj!r} has none of {names}")


def _dget(device, *keys, default=None):
    """Read a device field, accepting dicts, Device dataclasses and objects."""
    for key in keys:
        if hasattr(device, "get"):
            try:
                value = device.get(key)
            except (KeyError, TypeError):
                value = None
        else:
            value = getattr(device, key, None)
        if value is not None:
            return value
    return default


def _device_name(device):
    return str(
        _dget(device, "haName", "ha_name", "hiveName", "hive_name", "device_name")
        or ""
    )


def _device_type(device):
    return str(_dget(device, "hiveType", "hive_type") or "")


def _device_id(device):
    return _dget(device, "device_id", "deviceId")


def _labels(device):
    return [str(_dget(device, "hiveID", "hive_id") or ""), _device_name(device)]


class _Metrics:
    """The metric families rebuilt on every collect() call."""

    def __init__(self):
        device_labels = ["id", "name", "type"]
        entity_labels = ["id", "name"]
        mode_labels = ["id", "name", "mode"]

        self.up = GaugeMetricFamily(
            "hive_up", "1 if the last poll of the Hive API succeeded"
        )
        self.scrape_duration = GaugeMetricFamily(
            "hive_scrape_duration_seconds", "Time taken to collect Hive metrics"
        )
        self.scrape_errors = CounterMetricFamily(
            "hive_scrape_errors_total",
            "Total number of errors while polling the Hive API or reading devices",
        )

        self.online = GaugeMetricFamily(
            "hive_device_online", "1 if the device is online", labels=device_labels
        )
        self.battery = GaugeMetricFamily(
            "hive_device_battery_percent",
            "Battery level of the device (0-100)",
            labels=device_labels,
        )

        self.current_temp = GaugeMetricFamily(
            "hive_heating_current_temperature_celsius",
            "Current temperature measured by the thermostat/TRV",
            labels=entity_labels,
        )
        self.target_temp = GaugeMetricFamily(
            "hive_heating_target_temperature_celsius",
            "Target temperature of the heating zone",
            labels=entity_labels,
        )
        self.min_temp = GaugeMetricFamily(
            "hive_heating_min_temperature_celsius",
            "Minimum settable temperature",
            labels=entity_labels,
        )
        self.max_temp = GaugeMetricFamily(
            "hive_heating_max_temperature_celsius",
            "Maximum settable temperature",
            labels=entity_labels,
        )
        self.heating_active = GaugeMetricFamily(
            "hive_heating_active",
            "1 if the zone is currently calling for heat",
            labels=entity_labels,
        )
        self.heating_boost = GaugeMetricFamily(
            "hive_heating_boost_active",
            "1 if heating boost is currently on",
            labels=entity_labels,
        )
        self.heating_mode = GaugeMetricFamily(
            "hive_heating_mode",
            "Heating mode as a one-hot series (1 for the active mode)",
            labels=mode_labels,
        )

        self.hotwater_active = GaugeMetricFamily(
            "hive_hotwater_active",
            "1 if hot water is currently on",
            labels=entity_labels,
        )
        self.hotwater_boost = GaugeMetricFamily(
            "hive_hotwater_boost_active",
            "1 if hot water boost is currently on",
            labels=entity_labels,
        )
        self.hotwater_mode = GaugeMetricFamily(
            "hive_hotwater_mode",
            "Hot water mode as a one-hot series (1 for the active mode)",
            labels=mode_labels,
        )

        self.plug_on = GaugeMetricFamily(
            "hive_plug_on", "1 if the smart plug is switched on", labels=entity_labels
        )
        self.plug_power = GaugeMetricFamily(
            "hive_plug_power_watts",
            "Current power draw of the smart plug",
            labels=entity_labels,
        )

        self.light_on = GaugeMetricFamily(
            "hive_light_on", "1 if the light is on", labels=entity_labels
        )
        self.light_brightness = GaugeMetricFamily(
            "hive_light_brightness",
            "Brightness of the light as reported by Hive (0-100)",
            labels=entity_labels,
        )

        self.sensor_active = GaugeMetricFamily(
            "hive_sensor_active",
            "1 if the binary sensor is triggered (motion detected / contact open)",
            labels=device_labels,
        )

        self._seen_health = set()

    def families(self):
        return [
            value
            for value in vars(self).values()
            if not isinstance(value, set)
        ]


class HiveCollector:
    """Custom Prometheus collector for a Hive session."""

    def __init__(self, hive):
        self._hive = hive
        self._error_count = 0

    def collect(self):
        start = time.monotonic()
        metrics = _Metrics()

        up = 1.0
        try:
            self._refresh()
        except Exception:
            _LOGGER.exception("Failed to poll the Hive API")
            self._error_count += 1
            up = 0.0

        collectors = {
            "climate": self._collect_climate,
            "water_heater": self._collect_hotwater,
            "switch": self._collect_plug,
            "light": self._collect_light,
            "binary_sensor": self._collect_binary_sensor,
        }
        for category, collect_one in collectors.items():
            for device in self._devices(category):
                try:
                    collect_one(device, metrics)
                except Exception:
                    _LOGGER.exception(
                        "Failed to read Hive device %s (%s)",
                        _device_name(device),
                        category,
                    )
                    self._error_count += 1

        metrics.scrape_errors.add_metric([], self._error_count)
        metrics.up.add_metric([], up)
        metrics.scrape_duration.add_metric([], time.monotonic() - start)
        yield from metrics.families()

    # -- helpers ----------------------------------------------------------

    @property
    def _device_list(self):
        return (
            getattr(self._hive, "device_list", None)
            or getattr(self._hive, "deviceList", None)
            or {}
        )

    def _devices(self, category):
        return list(self._device_list.get(category, []))

    def _refresh(self):
        """Trigger the library's rate-limited poll of the Hive API."""
        update = _method(self._hive, "updateData", "update_data")
        for devices in self._device_list.values():
            for device in devices:
                update(device)
                return

    def _add(self, family, device, value, extra_labels=()):
        if value is None:
            return
        family.add_metric(_labels(device) + list(extra_labels), value)

    def _add_modes(self, family, device, reported_mode, known_modes):
        if reported_mode is None:
            return
        reported = str(reported_mode).upper()
        modes = list(known_modes)
        if reported not in modes:
            modes.append(reported)
        for mode in modes:
            family.add_metric(
                _labels(device) + [mode], 1.0 if mode == reported else 0.0
            )

    def _collect_health(self, device, metrics):
        labels = tuple(_labels(device)) + (_device_type(device),)
        if labels in metrics._seen_health:
            return
        metrics._seen_health.add(labels)

        device_id = _device_id(device)
        attr = self._hive.attr
        online = to_bool01(_method(attr, "onlineOffline", "online_offline")(device_id))
        if online is not None:
            metrics.online.add_metric(list(labels), online)

        battery_devices = getattr(self._hive.config, "battery", ()) or ()
        if device_id in battery_devices:
            battery = to_float(_method(attr, "getBattery", "get_battery")(device_id))
            if battery is not None:
                metrics.battery.add_metric(list(labels), battery)

    # -- per-category collectors ------------------------------------------

    def _collect_climate(self, device, metrics):
        device = _method(self._hive.heating, "getClimate", "get_climate")(device)
        status = _dget(device, "status") or {}
        self._add(
            metrics.current_temp, device, to_float(status.get("current_temperature"))
        )
        self._add(
            metrics.target_temp, device, to_float(status.get("target_temperature"))
        )
        self._add(metrics.min_temp, device, to_float(_dget(device, "min_temp")))
        self._add(metrics.max_temp, device, to_float(_dget(device, "max_temp")))
        self._add(metrics.heating_active, device, to_bool01(status.get("action")))
        self._add(metrics.heating_boost, device, to_bool01(status.get("boost")))
        self._add_modes(metrics.heating_mode, device, status.get("mode"), HEATING_MODES)
        self._collect_health(device, metrics)

    def _collect_hotwater(self, device, metrics):
        hotwater = self._hive.hotwater
        device = _method(hotwater, "getWaterHeater", "get_water_heater")(device)
        status = _dget(device, "status") or {}
        self._add(
            metrics.hotwater_active,
            device,
            to_bool01(_method(hotwater, "getState", "get_state")(device)),
        )
        self._add(
            metrics.hotwater_boost,
            device,
            to_bool01(
                _method(hotwater, "getBoost", "get_boost_status", "get_boost")(device)
            ),
        )
        self._add_modes(
            metrics.hotwater_mode, device, status.get("current_operation"), HOTWATER_MODES
        )
        self._collect_health(device, metrics)

    def _collect_plug(self, device, metrics):
        # The "switch" category also contains virtual switches such as
        # heat-on-demand; only physical smart plugs are exported.
        if _device_type(device).lower() != "activeplug":
            return
        switch = self._hive.switch
        self._add(
            metrics.plug_on,
            device,
            to_bool01(_method(switch, "getState", "get_state")(device)),
        )
        self._add(
            metrics.plug_power,
            device,
            to_float(_method(switch, "getPowerUsage", "get_power_usage")(device)),
        )
        self._collect_health(device, metrics)

    def _collect_light(self, device, metrics):
        light = self._hive.light
        self._add(
            metrics.light_on,
            device,
            to_bool01(_method(light, "getState", "get_state")(device)),
        )
        self._add(
            metrics.light_brightness,
            device,
            to_float(_method(light, "getBrightness", "get_brightness")(device)),
        )
        self._collect_health(device, metrics)

    def _collect_binary_sensor(self, device, metrics):
        if _device_type(device).lower() in _AVAILABILITY_TYPES:
            # These mirror online state, which hive_device_online covers.
            self._collect_health(device, metrics)
            return
        device = _method(self._hive.sensor, "getSensor", "get_sensor")(device)
        status = _dget(device, "status") or {}
        value = to_bool01(status.get("state"))
        if value is not None:
            metrics.sensor_active.add_metric(
                _labels(device) + [_device_type(device)], value
            )
        self._collect_health(device, metrics)
