"""Google Chat notifications via service account impersonation.

The bot's identity is a service account in the admin's GCP project.
No key file is needed: authentication uses the caller's Application Default
Credentials (gcloud auth application-default login) to mint short-lived
tokens for the service account.

Notification flow:
  daemon calls notify(space, sa_email, text)
    → impersonate sa_email via ADC
    → POST https://chat.googleapis.com/v1/{space}/messages

Setup flow (pp chat-setup):
  1. adc_user_id() — identify the caller via ADC + userinfo
  2. find_dm_space(sa_email, user_id) — find DM space between bot and user
     (requires the user to have sent the bot at least one message first)
  3. Caller writes the returned space name to config
"""

from __future__ import annotations

import logging
import warnings

import httpx

# Suppress noisy google-auth diagnostics (quota project, project ID).
warnings.filterwarnings("ignore", category=UserWarning, module="google.auth")
logging.getLogger("google.auth").setLevel(logging.ERROR)
logging.getLogger("google.auth.transport").setLevel(logging.ERROR)

_CHAT_BASE = "https://chat.googleapis.com/v1"
_SCOPES_BOT = ["https://www.googleapis.com/auth/chat.bot"]


def _impersonated_token(service_account_email: str, scopes: list[str]) -> str:
    """Mint a short-lived access token by impersonating a service account via ADC."""
    import google.auth
    from google.auth import impersonated_credentials
    from google.auth.transport.requests import Request

    source, _ = google.auth.default()
    target = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=service_account_email,
        target_scopes=scopes,
    )
    try:
        target.refresh(Request())
    except Exception as e:
        if "iam.serviceAccounts.getAccessToken" in str(e):
            raise PermissionError(
                f"Cannot impersonate {service_account_email}.\n"
                "Grant yourself the Token Creator role:\n"
                f"  gcloud iam service-accounts add-iam-policy-binding {service_account_email} \\\n"
                f"    --member='user:YOUR_EMAIL' --role='roles/iam.serviceAccountTokenCreator'"
            ) from None
        raise
    return target.token


def notify(space: str, service_account_email: str, text: str) -> None:
    """Post a text message to a Chat space as the bot."""
    token = _impersonated_token(service_account_email, _SCOPES_BOT)
    r = httpx.post(
        f"{_CHAT_BASE}/{space}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": text},
        timeout=10,
    )
    r.raise_for_status()


def adc_user_id() -> str:
    """Return the Google account user ID for the current ADC credentials."""
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/userinfo.email"],
    )
    credentials.refresh(Request())
    r = httpx.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {credentials.token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["sub"]


def find_dm_space(service_account_email: str, user_google_id: str) -> str:
    """Find the existing DM space between the bot and a user.

    Requires the user to have opened a DM with the bot in Google Chat first
    (search for "v8-utils-pinpoint" in Google Chat, not the service account email).
    Returns the space resource name, e.g. "spaces/AAA...".
    """
    token = _impersonated_token(service_account_email, _SCOPES_BOT)
    r = httpx.get(
        f"{_CHAT_BASE}/spaces:findDirectMessage",
        params={"name": f"users/{user_google_id}"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code == 404:
        raise ValueError(
            "No DM space found between you and the bot.\n"
            "In Google Chat, search for the bot by its app display name (not the service\n"
            "account email), open a DM, and send it a message. Then run pp chat-setup again."
        )
    if not r.is_success:
        raise RuntimeError(f"findDirectMessage failed ({r.status_code}):\n{r.text}")
    return r.json()["name"]
