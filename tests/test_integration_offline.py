"""Integration tests against the real pyhiveapi library in file (offline) mode.

pyhiveapi treats the username ``use@file.com`` as an instruction to serve
sample data from a JSON file instead of calling the live Hive API. The
exporter's ``--demo`` mode builds exactly such a session (serving the sample
data bundled with this package), so these tests exercise the full stack —
session setup plus collector — against the real library without credentials.
"""

from types import SimpleNamespace

import pytest

pyhiveapi = pytest.importorskip("pyhiveapi")

from prometheus_client import generate_latest  # noqa: E402
from prometheus_client.core import CollectorRegistry  # noqa: E402

from hive_exporter.collector import HiveCollector  # noqa: E402
from hive_exporter.exporter import build_hive  # noqa: E402


@pytest.fixture(scope="module")
def offline_hive():
    args = SimpleNamespace(
        demo=True, username=None, password=None, scan_interval=120
    )
    return build_hive(args)


@pytest.fixture
def registry(offline_hive):
    registry = CollectorRegistry()
    registry.register(HiveCollector(offline_hive))
    return registry


def collect_all(registry):
    metrics = {}
    for family in registry.collect():
        for sample in family.samples:
            metrics.setdefault(sample.name, []).append(sample)
    return metrics


def test_collector_produces_metrics_from_sample_data(registry):
    metrics = collect_all(registry)

    assert metrics["hive_up"][0].value == 1.0
    assert "hive_scrape_duration_seconds" in metrics

    # The sample data contains a thermostat and a TRV.
    temps = metrics.get("hive_heating_current_temperature_celsius")
    assert temps, f"expected heating metrics; got: {sorted(metrics)}"
    for sample in temps:
        assert sample.labels["name"]
        assert isinstance(sample.value, float)

    # Hot water, plugs, lights and battery levels are also present.
    assert metrics.get("hive_hotwater_mode")
    assert metrics.get("hive_plug_on")
    assert metrics.get("hive_light_on")
    assert metrics.get("hive_device_online")
    batteries = metrics.get("hive_device_battery_percent")
    assert batteries
    for sample in batteries:
        assert 0.0 <= sample.value <= 100.0


def test_heating_modes_are_one_hot(registry):
    metrics = collect_all(registry)
    by_device = {}
    for sample in metrics.get("hive_heating_mode", []):
        by_device.setdefault(sample.labels["id"], []).append(sample.value)
    assert by_device
    for values in by_device.values():
        assert sum(values) == 1.0, "exactly one mode should be active per device"


def test_no_duplicate_series(registry):
    metrics = collect_all(registry)
    for name, samples in metrics.items():
        seen = [tuple(sorted(sample.labels.items())) for sample in samples]
        assert len(seen) == len(set(seen)), f"duplicate series in {name}"


def test_scrape_output_is_valid_exposition_format(registry):
    output = generate_latest(registry).decode()
    assert "hive_up 1.0" in output
    assert "hive_heating_current_temperature_celsius" in output
