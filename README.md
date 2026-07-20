# hive-exporter

[![CI](https://github.com/billyaustin84/hive_exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/billyaustin84/hive_exporter/actions/workflows/ci.yml)

A [Prometheus](https://prometheus.io/) exporter for [Hive](https://www.hivehome.com/)
(British Gas) smart home devices, with a ready-made Grafana dashboard.

It uses [pyhive-integration](https://pypi.org/project/pyhive-integration/) — the
same library that powers Home Assistant's Hive integration — to poll the Hive
API and exposes heating, hot water, smart plug, light and sensor state as
Prometheus metrics.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
HIVE_USERNAME="you@example.com" HIVE_PASSWORD="..." .venv/bin/hive-exporter
```

(The `-e` matters if you're running from this checkout: a plain `pip install .`
snapshots the code, so later `git pull`s won't take effect until you reinstall.)

Then check `http://localhost:9986/metrics`.

No Hive account handy? Run against the bundled sample data:

```bash
hive-exporter --demo
```

### Configuration

| Flag | Env var | Default | Description |
| --- | --- | --- | --- |
| `--username` | `HIVE_USERNAME` | — | Hive account email |
| `--password` | `HIVE_PASSWORD` | — | Hive account password (prompted on a TTY if omitted) |
| `--port` | `HIVE_EXPORTER_PORT` | `9986` | Port to serve `/metrics` on |
| `--scan-interval` | `HIVE_SCAN_INTERVAL` | `120` | Minimum seconds between polls of the Hive API |
| `--device-file` | `HIVE_DEVICE_FILE` | `~/.config/hive-exporter/device.json` | Where registered-device credentials are stored |
| `--demo` | — | off | Serve the bundled sample data instead of a live account |
| `--log-level` | `HIVE_LOG_LEVEL` | `INFO` | Logging level |

Notes:

- Only the Hive account **owner** can log in through the API; guest accounts
  are not supported.
- If your account has **SMS two-factor authentication** enabled, run the
  exporter interactively once — it will prompt for the SMS code, then register
  itself as a trusted device and store the device credentials in
  `--device-file` (mode 0600). Later starts use those credentials and need no
  SMS code, so unattended restarts work. Delete the file (and the "hive-exporter"
  device in your Hive account) to revoke it.
- The Hive API is polled at most every `--scan-interval` seconds regardless of
  how often Prometheus scrapes; scraping more frequently just re-serves cached
  state.

### Docker

```bash
docker build -t hive-exporter .
docker run -e HIVE_USERNAME=you@example.com -e HIVE_PASSWORD=... -p 9986:9986 hive-exporter
```

### Running under systemd

Two ready-made units are provided; each has its full setup steps in comments
at the top of the file:

- [`systemd/user/hive-exporter.service`](systemd/user/hive-exporter.service) —
  user service running the exporter straight from this checkout. Put your
  credentials in `~/.config/hive-exporter/env`, copy the unit to
  `~/.config/systemd/user/`, then `systemctl --user enable --now hive-exporter`.
  Use `loginctl enable-linger $USER` so it keeps running while you're logged
  out.
- [`systemd/system/hive-exporter.service`](systemd/system/hive-exporter.service) —
  hardened system-wide service with a dedicated `hive-exporter` user, config in
  `/etc/hive-exporter/env` and state in `/var/lib/hive-exporter`.

With SMS 2FA enabled, run the exporter interactively once (with the same
device-file location the service will use) before starting the service, so the
trusted-device credentials exist and the service never needs to prompt.

### Prometheus scrape config

```yaml
scrape_configs:
  - job_name: hive
    scrape_interval: 60s
    static_configs:
      - targets: ["localhost:9986"]
```

## Metrics

| Metric | Labels | Description |
| --- | --- | --- |
| `hive_up` | — | 1 if the last poll of the Hive API succeeded |
| `hive_scrape_duration_seconds` | — | Time taken to collect metrics |
| `hive_scrape_errors_total` | — | Errors while polling the API or reading devices |
| `hive_device_online` | `id, name, type` | 1 if the device is online |
| `hive_device_battery_percent` | `id, name, type` | Battery level (battery-powered devices only) |
| `hive_heating_current_temperature_celsius` | `id, name` | Current temperature per thermostat/TRV |
| `hive_heating_target_temperature_celsius` | `id, name` | Target temperature |
| `hive_heating_min_temperature_celsius` | `id, name` | Minimum settable temperature |
| `hive_heating_max_temperature_celsius` | `id, name` | Maximum settable temperature |
| `hive_heating_active` | `id, name` | 1 if the zone is calling for heat |
| `hive_heating_boost_active` | `id, name` | 1 if heating boost is on |
| `hive_heating_mode` | `id, name, mode` | One-hot: 1 for the active mode (`SCHEDULE`/`MANUAL`/`OFF`) |
| `hive_hotwater_active` | `id, name` | 1 if hot water is on |
| `hive_hotwater_boost_active` | `id, name` | 1 if hot water boost is on |
| `hive_hotwater_mode` | `id, name, mode` | One-hot: 1 for the active mode (`SCHEDULE`/`ON`/`OFF`) |
| `hive_plug_on` | `id, name` | 1 if the smart plug is on |
| `hive_plug_power_watts` | `id, name` | Smart plug power draw |
| `hive_light_on` | `id, name` | 1 if the light is on |
| `hive_light_brightness` | `id, name` | Brightness as reported by Hive (0–100) |
| `hive_sensor_active` | `id, name, type` | 1 if a motion/contact sensor is triggered |

## Grafana dashboard

Import [`grafana/hive-dashboard.json`](grafana/hive-dashboard.json)
(Dashboards → New → Import) and pick your Prometheus data source when asked.
It includes:

- **Overview** — exporter status, devices online, hot water state and current
  temperatures at a glance
- **Heating & hot water** — measured vs target temperature (targets dashed),
  battery levels, calling-for-heat and boost timelines
- **Plugs, lights & sensors** — plug power draw, light brightness,
  motion/contact activity and a per-device status table
- **Exporter health** — scrape duration, error rate and current
  heating/hot-water modes

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check src tests
```

The test suite includes unit tests against a fake Hive session and integration
tests that run the real `pyhiveapi` library in its offline file mode (the same
mechanism `--demo` uses), so no credentials are needed.

CI (GitHub Actions) runs the tests on Python 3.10–3.13, lints with ruff, and
builds the Docker image with a demo-mode smoke test on every push and pull
request. Dependabot keeps the Python dependencies, GitHub Actions and the
Docker base image up to date with weekly grouped PRs.

## License

[MIT](LICENSE)
