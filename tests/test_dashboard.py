"""Consistency checks for the Grafana dashboard JSON."""

import json
import re
from pathlib import Path

import pytest

from hive_exporter.collector import _Metrics

DASHBOARD_PATH = (
    Path(__file__).parent.parent / "grafana" / "hive-dashboard.json"
)


@pytest.fixture(scope="module")
def dashboard():
    return json.loads(DASHBOARD_PATH.read_text())


def exported_metric_names():
    names = set()
    for family in _Metrics().families():
        names.add(family.name)
        names.add(family.name + "_total")
    return names


def test_dashboard_is_valid_json(dashboard):
    assert dashboard["title"]
    assert dashboard["panels"]


def test_every_query_uses_exported_metrics(dashboard):
    valid = exported_metric_names()
    exprs = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]
    assert exprs
    for expr in exprs:
        for metric in re.findall(r"\bhive_[a-z_]+\b", expr):
            assert metric in valid, f"dashboard queries unknown metric {metric}"


def test_panel_ids_are_unique(dashboard):
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))


def test_panels_use_the_datasource_variable(dashboard):
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        assert panel["datasource"]["uid"] == "${datasource}", panel["title"]
