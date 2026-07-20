"""Entry point: authenticate with Hive and serve Prometheus metrics."""

import argparse
import getpass
import json
import logging
import os
import sys
import time
from importlib import resources
from pathlib import Path

from prometheus_client import REGISTRY, start_http_server

from .collector import HiveCollector, _method

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 9986
DEMO_USERNAME = "use@file.com"
SMS_CHALLENGE = "SMS_MFA"
DEVICE_NAME = "hive-exporter"


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
        "--device-file",
        default=os.environ.get(
            "HIVE_DEVICE_FILE", "~/.config/hive-exporter/device.json"
        ),
        help=(
            "Where to store the registered-device credentials that allow "
            "logging back in without an SMS code (env: HIVE_DEVICE_FILE)"
        ),
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


def load_device_data(path):
    """Load stored device credentials; None if absent or unreadable."""
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, list) and len(data) >= 3 and all(data[:3]):
        return data[:3]
    _LOGGER.warning("Ignoring malformed device credential file %s", path)
    return None


def save_device_data(path, device_data):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(device_data)))
    path.chmod(0o600)


def authenticate(auth_factory, username, password, device_file, sms_code_provider):
    """Log in to Hive, preferring stored device credentials over SMS 2FA.

    Hive's Cognito pool tracks devices: refresh tokens issued after an SMS
    2FA login are bound to a device key and can only be refreshed with it.
    After the first SMS login the device is therefore registered and its
    credentials stored, which both makes token refresh work and lets future
    starts log in without an SMS code.

    Returns a ``(tokens, device_data)`` tuple; ``device_data`` is None when
    the account doesn't use device tracking.
    """
    device_data = load_device_data(device_file)
    if device_data:
        auth = auth_factory(
            username,
            password,
            device_group_key=device_data[0],
            device_key=device_data[1],
            device_password=device_data[2],
        )
        try:
            result = auth.device_login() or {}
            if "AuthenticationResult" in result:
                _LOGGER.info("Logged in with stored device credentials")
                return result, device_data
            _LOGGER.warning("Device login returned no tokens; retrying full login")
        except Exception:
            _LOGGER.warning(
                "Stored device credentials were rejected; retrying full login",
                exc_info=True,
            )
        device_data = None

    auth = auth_factory(username, password)
    tokens = login(auth, sms_code_provider)

    # An SMS login hands back device metadata; register the device so the
    # refresh token stays usable and the next start needs no SMS code.
    if getattr(auth, "device_key", None):
        try:
            auth.device_registration(DEVICE_NAME)
            device_data = list(auth.get_device_data())
            save_device_data(device_file, device_data)
            _LOGGER.info(
                "Registered this exporter as a trusted device; future logins "
                "won't need an SMS code (credentials in %s)",
                device_file,
            )
        except Exception:
            _LOGGER.warning(
                "Device registration failed; an SMS code will be needed again "
                "on the next start",
                exc_info=True,
            )
            device_data = None
    return tokens, device_data


def patch_refresh_token_bug(hive):
    """Work around a bug in published pyhiveapi wheels (<= 1.0.9).

    ``HiveAuth.refresh_token`` builds ``AuthParameters`` as a one-element
    tuple instead of a dict when no device key is registered, so every token
    refresh fails with a botocore ParamValidationError. The session forces a
    refresh on its first device fetch, which makes login unusable. Replace
    the method with a corrected call; newer library versions (which don't
    have the mangled ``__client_id`` attribute) are left untouched.
    """
    auth = getattr(hive, "auth", None)
    client_id = getattr(auth, "_HiveAuth__client_id", None)
    if client_id is None:
        return
    original = auth.refresh_token

    def refresh_token(token):
        if getattr(auth, "device_key", None) is not None:
            return original(token)
        try:
            return auth.client.initiate_auth(
                ClientId=client_id,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={"REFRESH_TOKEN": token},
            )
        except Exception as error:
            if error.__class__.__name__ == "EndpointConnectionError":
                from pyhiveapi.helper.hive_exceptions import HiveApiError

                raise HiveApiError from error
            raise

    auth.refresh_token = refresh_token


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

    # pyhiveapi replaces sys.excepthook with a broken handler on import;
    # restore the default so real tracebacks aren't mangled.
    sys.excepthook = sys.__excepthook__

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

        sms_provider = _interactive_sms_code if sys.stdin.isatty() else None
        tokens, device_data = authenticate(
            Auth, args.username, password, args.device_file, sms_provider
        )

        hive = Hive(username=args.username, password=password)
        patch_refresh_token_bug(hive)
        config = {"tokens": tokens}
        if device_data:
            config["device_data"] = device_data
        _method(hive, "startSession", "start_session")(config)

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
    except Exception:
        _LOGGER.exception("Failed to start a Hive session")
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
