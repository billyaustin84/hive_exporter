"""Tests for CLI parsing and the login flow."""

from types import SimpleNamespace

import pytest

from hive_exporter import exporter
from hive_exporter.exporter import LoginError, login, parse_args


class FakeAuth:
    def __init__(self, login_result, sms_result=None):
        self.login_result = login_result
        self.sms_result = sms_result
        self.sms_calls = []

    def login(self):
        return self.login_result

    def sms_2fa(self, code, challenge):
        self.sms_calls.append((code, challenge))
        return self.sms_result


class TestParseArgs:
    def test_defaults(self, monkeypatch):
        for var in ("HIVE_USERNAME", "HIVE_PASSWORD", "HIVE_EXPORTER_PORT"):
            monkeypatch.delenv(var, raising=False)
        args = parse_args([])
        assert args.port == exporter.DEFAULT_PORT
        assert args.scan_interval == 120
        assert args.username is None
        assert not args.demo

    def test_env_fallbacks(self, monkeypatch):
        monkeypatch.setenv("HIVE_USERNAME", "user@example.com")
        monkeypatch.setenv("HIVE_PASSWORD", "hunter2")
        monkeypatch.setenv("HIVE_EXPORTER_PORT", "9001")
        monkeypatch.setenv("HIVE_SCAN_INTERVAL", "60")
        args = parse_args([])
        assert args.username == "user@example.com"
        assert args.password == "hunter2"
        assert args.port == 9001
        assert args.scan_interval == 60

    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("HIVE_USERNAME", "env@example.com")
        args = parse_args(["--username", "cli@example.com"])
        assert args.username == "cli@example.com"


class TestLogin:
    def test_plain_login(self):
        auth = FakeAuth({"AuthenticationResult": {"IdToken": "x"}})
        tokens = login(auth)
        assert tokens["AuthenticationResult"] == {"IdToken": "x"}

    def test_sms_challenge_uses_code_provider(self):
        challenge = {"ChallengeName": "SMS_MFA", "Session": "sess"}
        auth = FakeAuth(challenge, sms_result={"AuthenticationResult": {"IdToken": "y"}})
        tokens = login(auth, sms_code_provider=lambda: "123456")
        assert auth.sms_calls == [("123456", challenge)]
        assert tokens["AuthenticationResult"] == {"IdToken": "y"}

    def test_sms_challenge_without_provider_raises(self):
        auth = FakeAuth({"ChallengeName": "SMS_MFA", "Session": "sess"})
        with pytest.raises(LoginError, match="two-factor"):
            login(auth)

    def test_missing_tokens_raises(self):
        auth = FakeAuth({})
        with pytest.raises(LoginError, match="did not return"):
            login(auth)


class FakeCognitoClient:
    def __init__(self):
        self.calls = []

    def initiate_auth(self, **kwargs):
        self.calls.append(kwargs)
        return {"AuthenticationResult": {"IdToken": "fresh"}}


class TestRefreshTokenPatch:
    def make_hive(self, client_id="client-123", device_key=None):
        client = FakeCognitoClient()
        auth = SimpleNamespace(
            client=client,
            device_key=device_key,
            refresh_token=lambda token: ("original", token),
        )
        setattr(auth, "_HiveAuth__client_id", client_id)
        return SimpleNamespace(auth=auth), client

    def test_patched_refresh_sends_dict_auth_parameters(self):
        hive, client = self.make_hive()
        exporter.patch_refresh_token_bug(hive)

        result = hive.auth.refresh_token("refresh-token")
        assert result == {"AuthenticationResult": {"IdToken": "fresh"}}
        (call,) = client.calls
        assert call["AuthParameters"] == {"REFRESH_TOKEN": "refresh-token"}
        assert call["AuthFlow"] == "REFRESH_TOKEN_AUTH"
        assert call["ClientId"] == "client-123"

    def test_device_key_path_uses_original_method(self):
        hive, client = self.make_hive(device_key="device-key")
        exporter.patch_refresh_token_bug(hive)

        assert hive.auth.refresh_token("tok") == ("original", "tok")
        assert client.calls == []

    def test_fixed_library_versions_left_untouched(self):
        auth = SimpleNamespace(refresh_token=lambda token: ("original", token))
        hive = SimpleNamespace(auth=auth)
        exporter.patch_refresh_token_bug(hive)

        assert hive.auth.refresh_token("tok") == ("original", "tok")

    def test_installed_library_refresh_bug_is_fixed_by_patch(self):
        """Against the real library: reproduce the tuple bug, then verify the patch."""
        pyhiveapi = pytest.importorskip("pyhiveapi")
        hive = pyhiveapi.Hive(username="user@example.com", password="pw")
        client_id = getattr(hive.auth, "_HiveAuth__client_id", None)
        if client_id is None:
            pytest.skip("library version does not have the refresh_token bug")

        client = FakeCognitoClient()
        hive.auth.client = client
        hive.auth.device_key = None
        exporter.patch_refresh_token_bug(hive)

        hive.auth.refresh_token("refresh-token")
        (call,) = client.calls
        assert isinstance(call["AuthParameters"], dict)
        assert call["AuthParameters"] == {"REFRESH_TOKEN": "refresh-token"}


class TestBuildHive:
    def test_missing_username_raises(self):
        args = SimpleNamespace(
            demo=False, username=None, password=None, scan_interval=120
        )
        with pytest.raises(LoginError, match="username"):
            exporter.build_hive(args)

    def test_missing_password_raises_when_not_a_tty(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
        args = SimpleNamespace(
            demo=False, username="user@example.com", password=None, scan_interval=120
        )
        with pytest.raises(LoginError, match="password"):
            exporter.build_hive(args)
