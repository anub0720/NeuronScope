"""First-run project provisioning and OS credential storage."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import webbrowser
from urllib.parse import urlparse

import httpx

from neuronscope.exceptions import ConfigurationError, TransportError

logger = logging.getLogger("neuronscope")
_KEYRING_SERVICE = "neuronscope-project-credentials"


def _keyring_module():
    try:
        import keyring
    except ImportError as exc:
        raise ConfigurationError(
            "First-run setup needs a secure OS keyring. Install the SDK with "
            "credential support or provide NEURONSCOPE_PROJECT_ID and "
            "NEURONSCOPE_API_KEY through environment variables/Kaggle Secrets."
        ) from exc
    try:
        backend = keyring.get_keyring()
        backend_name = type(backend).__module__ + "." + type(backend).__name__
        backend_priority = backend.priority
    except Exception as exc:
        raise ConfigurationError(
            "A secure OS credential store is unavailable. Provide "
            "NEURONSCOPE_PROJECT_ID and NEURONSCOPE_API_KEY through environment "
            "variables/Kaggle Secrets instead."
        ) from exc
    secure_backends = (
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.macOS.Keyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.kwallet.",
    )
    if backend_priority <= 0 or not any(
        marker in backend_name for marker in secure_backends
    ):
        raise ConfigurationError(
            "No secure OS credential store is available. Provide "
            "NEURONSCOPE_PROJECT_ID and NEURONSCOPE_API_KEY through environment "
            "variables/Kaggle Secrets instead of storing a permanent key in a file."
        )
    return keyring


def _ensure_keyring_writable(keyring) -> None:
    probe_account = "probe-" + secrets.token_urlsafe(12)
    try:
        keyring.set_password(_KEYRING_SERVICE, probe_account, "probe")
        keyring.delete_password(_KEYRING_SERVICE, probe_account)
    except Exception as exc:
        raise ConfigurationError(
            "The OS credential store is not writable. Provide "
            "NEURONSCOPE_PROJECT_ID and NEURONSCOPE_API_KEY through environment "
            "variables/Kaggle Secrets instead."
        ) from exc


def _credential_account(endpoint: str, project_id: str | None) -> str:
    origin = urlparse(endpoint)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.netloc
        or not origin.hostname
    ):
        raise ConfigurationError("endpoint must be an absolute HTTP(S) backend URL")
    if origin.username or origin.password or origin.query or origin.fragment:
        raise ConfigurationError(
            "endpoint must be an origin URL without embedded credentials or query values"
        )
    if origin.scheme != "https" and origin.hostname not in {"localhost", "127.0.0.1"}:
        raise ConfigurationError("SDK project onboarding requires HTTPS")
    return f"{origin.scheme}://{origin.netloc}|{project_id or 'default'}"


def _read_stored(
    keyring,
    endpoint: str,
    project_id: str | None,
) -> tuple[str, str] | None:
    value = keyring.get_password(
        _KEYRING_SERVICE, _credential_account(endpoint, project_id)
    )
    if not value and project_id:
        value = keyring.get_password(
            _KEYRING_SERVICE, _credential_account(endpoint, None)
        )
    if not value:
        return None
    try:
        stored = json.loads(value)
        stored_project_id = stored["project_id"]
        stored_api_key = stored["api_key"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigurationError(
            "Stored NeuronScope credentials are unreadable; remove the broken "
            "entry from the OS credential manager and retry."
        ) from exc
    if project_id and project_id != stored_project_id:
        return None
    if not isinstance(stored_api_key, str) or not stored_api_key:
        raise ConfigurationError("Stored NeuronScope project credentials are invalid")
    return str(stored_project_id), stored_api_key


def _store(
    keyring,
    endpoint: str,
    project_id: str,
    api_key: str,
) -> None:
    payload = json.dumps({"project_id": project_id, "api_key": api_key})
    try:
        keyring.set_password(
            _KEYRING_SERVICE,
            _credential_account(endpoint, None),
            payload,
        )
        keyring.set_password(
            _KEYRING_SERVICE,
            _credential_account(endpoint, project_id),
            payload,
        )
    except Exception as exc:
        raise ConfigurationError(
            "NeuronScope credentials could not be saved in the OS credential "
            "store. Retry after fixing the store, or provide credentials through "
            "environment variables/Kaggle Secrets."
        ) from exc


def _provision(
    endpoint: str,
    project_id: str | None,
    project_name: str,
) -> tuple[str, str]:
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{endpoint}/api/v1/auth/device",
                json={
                    "project_id": project_id,
                    "project_name": project_name,
                },
            )
            response.raise_for_status()
            authorization = response.json()
            device_code = authorization["device_code"]
            user_code = authorization["user_code"]
            verification_uri = authorization["verification_uri"]
            expires_in = int(authorization["expires_in"])
            interval = max(1, int(authorization.get("interval", 2)))
            opened = webbrowser.open(verification_uri)
            logger.warning(
                "Authorize NeuronScope in your browser%s. Verification code: %s",
                "" if opened else f" at {verification_uri}",
                user_code,
            )
            deadline = time.monotonic() + expires_in
            while time.monotonic() < deadline:
                time.sleep(interval)
                response = client.post(
                    f"{endpoint}/api/v1/auth/device/token",
                    json={"device_code": device_code},
                )
                if response.status_code == 428:
                    continue
                if response.status_code == 410:
                    raise TransportError("NeuronScope device authorization expired")
                response.raise_for_status()
                credentials = response.json()
                return credentials["project_id"], credentials["api_key"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise TransportError(
            "Could not complete NeuronScope account authorization"
        ) from exc
    raise TransportError("NeuronScope device authorization timed out")


def _validate_project_credentials(
    endpoint: str,
    project_id: str,
    api_key: str,
) -> None:
    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{endpoint}/api/v1/experiments",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-Project-ID": project_id,
                },
            )
    except httpx.HTTPError as exc:
        raise TransportError(
            "Could not verify NeuronScope credentials; check the backend endpoint "
            "and network connection."
        ) from exc

    if response.status_code in {401, 403}:
        raise ConfigurationError(
            "NeuronScope rejected the project credential. It may be invalid, "
            "revoked, or paired with the wrong project ID. Issue a new credential "
            "in the dashboard and update the environment variables or Kaggle Secrets."
        )
    if response.status_code == 404:
        raise ConfigurationError(
            "The backend endpoint does not expose the NeuronScope experiment API; "
            "check NEURONSCOPE_ENDPOINT."
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TransportError(
            f"Could not verify NeuronScope credentials (HTTP {response.status_code})."
        ) from exc
    try:
        experiments = response.json()
    except ValueError as exc:
        raise TransportError(
            "The backend returned an invalid response while checking credentials."
        ) from exc
    if not isinstance(experiments, list):
        raise TransportError(
            "The backend returned an unexpected response while checking credentials."
        )


def resolve_credentials(
    endpoint: str | None,
    project_id: str | None,
    api_key: str | None,
    project_name: str = "NeuronScope",
) -> tuple[str, str, str]:
    resolved_endpoint = (
        endpoint or os.environ.get("NEURONSCOPE_ENDPOINT", "")
    ).strip().rstrip("/")
    if not resolved_endpoint:
        raise ConfigurationError(
            "endpoint is required; set it directly or through NEURONSCOPE_ENDPOINT"
        )
    _credential_account(resolved_endpoint, None)
    explicit_project_id = project_id or os.environ.get("NEURONSCOPE_PROJECT_ID")
    explicit_api_key = api_key or os.environ.get("NEURONSCOPE_API_KEY")
    explicit_project_id = explicit_project_id.strip() if explicit_project_id else None
    explicit_api_key = explicit_api_key.strip() if explicit_api_key else None

    if bool(explicit_project_id) != bool(explicit_api_key):
        raise ConfigurationError(
            "Set both NEURONSCOPE_PROJECT_ID and NEURONSCOPE_API_KEY, or omit both "
            "to use secure SDK device authorization."
        )
    if explicit_project_id and explicit_api_key:
        _validate_project_credentials(
            resolved_endpoint, explicit_project_id, explicit_api_key
        )
        return resolved_endpoint, explicit_project_id, explicit_api_key

    keyring = _keyring_module()
    stored = _read_stored(keyring, resolved_endpoint, explicit_project_id)
    if stored:
        _validate_project_credentials(resolved_endpoint, stored[0], stored[1])
        return resolved_endpoint, stored[0], stored[1]

    _ensure_keyring_writable(keyring)
    new_project_id, new_api_key = _provision(
        resolved_endpoint, explicit_project_id, project_name
    )
    _store(keyring, resolved_endpoint, new_project_id, new_api_key)
    return resolved_endpoint, new_project_id, new_api_key
