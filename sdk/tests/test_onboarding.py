import httpx
import pytest

from neuronscope import onboarding
from neuronscope.exceptions import ConfigurationError, TransportError


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        del self.values[(service, account)]


class FakeDeviceClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, json):
        self.requests.append((url, json))
        response = next(self.responses)
        response._request = httpx.Request("POST", url)
        return response


class FakeCredentialClient:
    def __init__(self, response):
        self.response = response
        self.request = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, headers):
        self.request = (url, headers)
        self.response._request = httpx.Request("GET", url)
        return self.response


def test_provision_waits_for_approval_and_never_logs_the_project_key(
    monkeypatch, caplog
):
    client = FakeDeviceClient(
        [
            httpx.Response(
                200,
                json={
                    "device_code": "device-code-secret",
                    "user_code": "ABCD2345",
                    "verification_uri": "https://dashboard.example.test/?device_code=ABCD2345",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            httpx.Response(428),
            httpx.Response(
                200,
                json={"project_id": "project-1", "api_key": "ns_live_secret"},
            ),
        ]
    )
    monkeypatch.setattr(onboarding.httpx, "Client", lambda timeout: client)
    monkeypatch.setattr(onboarding.webbrowser, "open", lambda _url: True)
    monkeypatch.setattr(onboarding.time, "sleep", lambda _seconds: None)

    result = onboarding._provision("https://api.example.test", None, "Project")

    assert result == ("project-1", "ns_live_secret")
    assert len(client.requests) == 3
    assert "ns_live_secret" not in caplog.text
    assert "device-code-secret" not in caplog.text


def test_device_authorization_expiry_is_reported(monkeypatch):
    client = FakeDeviceClient(
        [
            httpx.Response(
                200,
                json={
                    "device_code": "device-code-secret",
                    "user_code": "ABCD2345",
                    "verification_uri": "https://dashboard.example.test",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            httpx.Response(410),
        ]
    )
    monkeypatch.setattr(onboarding.httpx, "Client", lambda timeout: client)
    monkeypatch.setattr(onboarding.webbrowser, "open", lambda _url: True)
    monkeypatch.setattr(onboarding.time, "sleep", lambda _seconds: None)

    with pytest.raises(TransportError, match="expired"):
        onboarding._provision("https://api.example.test", None, "Project")


def test_provisioned_credentials_are_reused_without_duplicate_projects(
    monkeypatch,
):
    keyring = FakeKeyring()
    calls = []
    validations = []
    monkeypatch.setattr(onboarding, "_keyring_module", lambda: keyring)
    monkeypatch.setattr(
        onboarding,
        "_validate_project_credentials",
        lambda endpoint, project_id, api_key: validations.append(
            (endpoint, project_id, api_key)
        ),
    )
    monkeypatch.setattr(
        onboarding,
        "_provision",
        lambda endpoint, project_id, project_name: (
            calls.append((endpoint, project_id, project_name))
            or ("project-1", "ns_live_secret")
        ),
    )

    first = onboarding.resolve_credentials(
        "https://api.example.test", None, None, "My project"
    )
    second = onboarding.resolve_credentials(
        "https://api.example.test", None, None, "My project"
    )

    assert first == second == (
        "https://api.example.test",
        "project-1",
        "ns_live_secret",
    )
    assert calls == [("https://api.example.test", None, "My project")]
    assert validations == [
        ("https://api.example.test", "project-1", "ns_live_secret")
    ]


def test_explicit_environment_credentials_bypass_device_flow(monkeypatch):
    client = FakeCredentialClient(httpx.Response(200, json=[]))
    monkeypatch.setenv("NEURONSCOPE_ENDPOINT", "https://api.example.test")
    monkeypatch.setenv("NEURONSCOPE_PROJECT_ID", "project-1")
    monkeypatch.setenv("NEURONSCOPE_API_KEY", "ns_live_existing")
    monkeypatch.setattr(onboarding.httpx, "Client", lambda timeout: client)
    monkeypatch.setattr(
        onboarding, "_keyring_module", lambda: pytest.fail("unexpected keyring")
    )

    assert onboarding.resolve_credentials(None, None, None) == (
        "https://api.example.test",
        "project-1",
        "ns_live_existing",
    )
    assert client.request == (
        "https://api.example.test/api/v1/experiments",
        {
            "Authorization": "Bearer ns_live_existing",
            "X-Project-ID": "project-1",
        },
    )


def test_missing_environment_credential_pair_is_reported_without_keyring(
    monkeypatch,
):
    monkeypatch.setenv("NEURONSCOPE_ENDPOINT", "https://api.example.test")
    monkeypatch.setenv("NEURONSCOPE_PROJECT_ID", "project-1")
    monkeypatch.delenv("NEURONSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(
        onboarding, "_keyring_module", lambda: pytest.fail("unexpected keyring")
    )

    with pytest.raises(ConfigurationError, match="Set both"):
        onboarding.resolve_credentials(None, None, None)


@pytest.mark.parametrize("status_code", [401, 403])
def test_invalid_or_revoked_explicit_credentials_fail_before_initialization(
    monkeypatch, status_code
):
    client = FakeCredentialClient(
        httpx.Response(status_code, json={"detail": "Invalid API key"})
    )
    monkeypatch.setattr(onboarding.httpx, "Client", lambda timeout: client)

    with pytest.raises(ConfigurationError, match="rejected the project credential"):
        onboarding.resolve_credentials(
            "https://api.example.test",
            "project-1",
            "ns_live_revoked",
        )


def test_remote_plain_http_is_rejected_before_credentials_are_used():
    with pytest.raises(ConfigurationError, match="requires HTTPS"):
        onboarding.resolve_credentials(
            "http://api.example.test", "project-1", "ns_live_existing"
        )
