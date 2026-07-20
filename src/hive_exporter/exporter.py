"""Entry point: authenticate with Hive and serve Prometheus metrics."""

import argparse
import getpass
import json
import logging
import os
import sys
import time
from importlib import resources

from prometheus_client import REGISTRY, start_http_server

from .collector import HiveCollector, _method

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 9986
DEMO_USERNAME = "use@file.com"
SMS_CHALLENGE = "SMS_MFA"


class LoginError(RuntimeError):
    """Raised when authentication with Hive cannot be completed."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="hive-exporter",
        description="Prometheus exporter for Hive (British Gas) smart home devices",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("HIVE_USERNAME"),
        help="Hive account email (env: HIVE_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("HIVE_PASSWORD"),
        help="Hive account password (env: HIVE_PASSWORD; prompted if omitted on a TTY)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("HIVE_EXPORTER_PORT", DEFAULT_PORT)),
        help=f"Port to serve /metrics on (env: HIVE_EXPORTER_PORT, default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=int(os.environ.get("HIVE_SCAN_INTERVAL", 120)),
        help="Minimum seconds between polls of the Hive API (env: HIVE_SCAN_INTERVAL, default 120)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run against the library's bundled sample data instead of a live account",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("HIVE_LOG_LEVEL", "INFO"),
        help="Logging level (env: HIVE_LOG_LEVEL, default INFO)",
    )
    return parser.parse_args(argv)


def login(auth, sms_code_provider=None):
    """Log in to Hive, completing SMS 2FA via sms_code_provider if required.

    Returns the Cognito token payload to pass to ``Hive.start_session``.
    """
    result = auth.login() or {}
    if result.get("ChallengeName") == SMS_CHALLENGE:
        if sms_code_provider is None:
            raise LoginError(
                "This Hive account has SMS two-factor authentication enabled. "
                "Run the exporter interactively once so the SMS code can be entered."
            )
        result = auth.sms_2fa(sms_code_provider(), result) or {}
    if "AuthenticationResult" not in result:
        raise LoginError("Hive login did not return authentication tokens")
    return result


def _interactive_sms_code():
    return input("Enter the SMS code sent to your phone: ").strip()


def install_sample_data(hive):
    """Serve bundled sample data when the library's own data files are absent.

    pyhiveapi's file mode reads JSON from a path inside the library, but
    published wheels don't include those files; fall back to the copy of the
    sample data shipped with this exporter.
    """
    original = getattr(hive, "openFile", None) or getattr(hive, "open_file", None)

    def open_file(name):
        try:
            return original(name)
        except FileNotFoundError:
            sample = resources.files("hive_exporter").joinpath(
                "data/sample_data.json"
            )
            with sample.open() as handle:
                return json.load(handle)

    if hasattr(hive, "openFile"):
        hive.openFile = open_file
    else:
        hive.open_file = open_file


def build_hive(args):
    """Create and start a Hive session according to the CLI arguments."""
    from pyhiveapi import Auth, Hive  # imported late so tests can stub it

    if args.demo:
        hive = Hive(username=DEMO_USERNAME, password="")
        install_sample_data(hive)
        _method(hive, "startSession", "start_session")({})
    else:
        if not args.username:
            raise LoginError("A Hive username is required (--username or HIVE_USERNAME)")
        password = args.password
        if not password and sys.stdin.isatty():
            password = getpass.getpass("Hive password: ")
        if not password:
            raise LoginError("A Hive password is required (--password or HIVE_PASSWORD)")

        auth = Auth(args.username, password)
        sms_provider = _interactive_sms_code if sys.stdin.isatty() else None
        tokens = login(auth, sms_provider)

        hive = Hive(username=args.username, password=password)
        _method(hive, "startSession", "start_session")({"tokens": tokens})

    # Both API generations accept plain seconds here.
    _method(hive, "updateInterval", "update_interval")(args.scan_interval)
    return hive


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        hive = build_hive(args)
    except LoginError as error:
        _LOGGER.error("%s", error)
        return 1

    device_list = getattr(hive, "device_list", None) or getattr(hive, "deviceList", {})
    device_count = sum(len(devices) for devices in device_list.values())
    _LOGGER.info("Connected to Hive; discovered %d devices", device_count)

    REGISTRY.register(HiveCollector(hive))
    start_http_server(args.port)
    _LOGGER.info("Serving metrics on http://0.0.0.0:%d/metrics", args.port)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _LOGGER.info("Shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
