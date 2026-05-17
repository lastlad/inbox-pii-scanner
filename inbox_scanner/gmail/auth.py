"""Google OAuth flow.

Two entry points:

* :func:`run_oauth_flow` — interactive. Triggered by ``inbox-scanner auth``.
  Opens a browser, walks the user through the consent screen, writes
  ``token.json`` to the data dir.
* :func:`load_credentials` — non-interactive. Used by every other command.
  Loads ``token.json``, refreshes if expired (silently), and raises
  :class:`CredentialsMissing` if it can't produce a usable credential
  without user interaction. Callers turn that into a friendly "run
  ``inbox-scanner auth`` first" message.

Scope is locked to ``gmail.readonly`` — the tool never asks for write access.
"""

from __future__ import annotations

from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class CredentialsMissing(RuntimeError):
    """Raised when no usable credential is available without interaction."""


def run_oauth_flow(credentials_path: Path, token_path: Path) -> Credentials:
    if not credentials_path.is_file():
        raise CredentialsMissing(
            f"OAuth client credentials not found at {credentials_path}.\n"
            "Create a Google Cloud project, enable the Gmail API, configure an "
            "OAuth client (application type: Desktop app), download the JSON, "
            f"and save it to {credentials_path}."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(credentials_path), GMAIL_SCOPES
    )
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    return creds


def load_credentials(token_path: Path) -> Credentials:
    """Load and refresh saved credentials. Never triggers an interactive flow.

    Raises :class:`CredentialsMissing` if no token exists, or if the saved
    token is invalid and cannot be refreshed silently.
    """
    if not token_path.is_file():
        raise CredentialsMissing(
            f"No saved OAuth token at {token_path}. "
            "Run `inbox-scanner auth` to authenticate first."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # Refresh token has expired, been revoked, or the OAuth client
            # changed. Either way the only fix is a fresh interactive
            # ``inbox-scanner auth`` — don't let the raw Google traceback
            # bubble up to the user.
            raise CredentialsMissing(
                f"Saved token at {token_path} could not be refreshed "
                f"({e}). This usually means the refresh token has "
                "expired or been revoked.\n"
                "Run `inbox-scanner auth` to re-authenticate."
            ) from None
        token_path.write_text(creds.to_json())
        return creds
    raise CredentialsMissing(
        f"Saved token at {token_path} is invalid and cannot be refreshed silently. "
        "Run `inbox-scanner auth` to re-authenticate."
    )
