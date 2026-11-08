"""One-time Google Drive authorization for WorkflowWeaver (Phase 6.1).

    .\\.venv\\Scripts\\python.exe -m scripts.google_drive_auth status     # offline: what is configured
    .\\.venv\\Scripts\\python.exe -m scripts.google_drive_auth authorize  # opens the browser consent screen
    .\\.venv\\Scripts\\python.exe -m scripts.google_drive_auth revoke     # revokes the token at Google, deletes the file

OAuth 2.0 for installed apps: a "Desktop app" client, loopback redirect on 127.0.0.1 (random
port), PKCE S256 and a random ``state``. The ONLY scope requested is drive.file (the app can
access only files it creates); a grant with any other scope is refused and not saved.

The refresh token is written to GOOGLE_DRIVE_TOKEN_FILE (outside the repository, owner-only
permissions where supported). Nothing printed by this script contains a token, the
authorization code or the client secret.
"""

import argparse
import http.server
import secrets
import socketserver
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx

from app.core.config import Settings
from app.core.log_redaction import install_secret_redaction
from app.integrations.config import load_integration_config
from app.integrations.errors import IntegrationConfigError, IntegrationError
from app.integrations.google.oauth import (
    DRIVE_FILE_SCOPE,
    authorization_url,
    exchange_code,
    load_token,
    pkce_pair,
    revoke,
    save_token,
)

CALLBACK_TIMEOUT_SECONDS = 300
_PAGE = (b"<!doctype html><title>WorkflowWeaver</title><p>%s</p><p>You can close this tab and return to the "
         b"terminal.</p>")


@dataclass
class Callback:
    """The single redirect the loopback server waits for."""

    expected_state: str
    code: str | None = field(default=None, repr=False)
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def handle(self, query: str) -> bytes:
        params = parse_qs(query)
        state = (params.get("state") or [""])[0]
        if not secrets.compare_digest(state, self.expected_state):
            self.error = "state mismatch (possible CSRF); nothing was saved"
        elif "error" in params:
            reason = params["error"][0]
            self.error = reason if reason.replace("_", "").isalnum() and len(reason) <= 40 else "authorization_error"
        elif not params.get("code"):
            self.error = "no authorization code in the redirect"
        else:
            self.code = params["code"][0]
        self.done.set()
        return _PAGE % (b"Authorization received." if self.code else b"Authorization failed.")


def _server(callback: Callback) -> socketserver.TCPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib name
            parts = urlsplit(self.path)
            if parts.path != "/":
                self.send_response(404)
                self.end_headers()
                return
            body = callback.handle(parts.query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # the request line contains the code: never log it
            pass

    return socketserver.TCPServer(("127.0.0.1", 0), Handler)


def authorize(settings: Settings, *, open_browser=webbrowser.open, http: httpx.Client | None = None,
              timeout: float = CALLBACK_TIMEOUT_SECONDS) -> int:
    config = load_integration_config(settings)
    rest = config.drive_rest
    if rest is None:
        print("Set GOOGLE_DRIVE_TRANSPORT=rest, GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET first.")
        return 2
    verifier, challenge = pkce_pair()
    callback = Callback(expected_state=secrets.token_urlsafe(24))
    server = _server(callback)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = authorization_url(client_id=rest.client_id, redirect_uri=redirect_uri, state=callback.expected_state,
                                code_challenge=challenge)
        print(f"Requesting ONLY the scope {DRIVE_FILE_SCOPE}")
        print("Opening your browser for Google consent. If it does not open, visit:\n" + url)
        open_browser(url)
        if not callback.done.wait(timeout):
            print("Timed out waiting for the browser redirect. Nothing was saved.")
            return 1
    finally:
        server.shutdown()
        server.server_close()
    if callback.error or not callback.code:
        print(f"Authorization failed: {callback.error}. Nothing was saved.")
        return 1
    own_http = http is None
    http = http or httpx.Client(timeout=30.0)
    try:
        data = exchange_code(http, client_id=rest.client_id, client_secret=rest.client_secret, code=callback.code,
                             code_verifier=verifier, redirect_uri=redirect_uri)
    except IntegrationError as exc:
        print(f"Token exchange failed: {exc.code}: {exc.message} Nothing was saved.")
        return 1
    finally:
        if own_http:
            http.close()
    save_token(rest.token_path, refresh_token=data["refresh_token"], scope=data["scope"], client_id=rest.client_id)
    print(f"Authorized with scope drive.file only. Refresh token saved to {rest.token_path} (outside the repo).")
    print("Next: .\\.venv\\Scripts\\python.exe -m scripts.google_drive_sandbox create-folder --confirm")
    return 0


def status(settings: Settings) -> int:
    config = load_integration_config(settings)
    rest = config.drive_rest
    print(f"GOOGLE_DRIVE_TRANSPORT={config.drive_transport.value}")
    print(f"OAuth client configured: {rest is not None}")
    if rest is None:
        return 0
    print(f"Token file: {rest.token_path} ({'present' if rest.token_path.is_file() else 'missing'})")
    if rest.token_path.is_file():
        try:
            stored = load_token(rest.token_path)
            print(f"Granted scope: {stored.scope} | issued to this client: {stored.client_id == rest.client_id}")
        except IntegrationError as exc:
            print(f"Token file problem: {exc.code}: {exc.message}")
    return 0


def revoke_token(settings: Settings, *, http: httpx.Client | None = None) -> int:
    config = load_integration_config(settings)
    rest = config.drive_rest
    if rest is None or not rest.token_path.is_file():
        print("No token file to revoke.")
        return 0
    stored = load_token(rest.token_path)
    own_http = http is None
    http = http or httpx.Client(timeout=30.0)
    try:
        ok = revoke(http, stored.refresh_token)
    finally:
        if own_http:
            http.close()
    rest.token_path.unlink()
    print(f"Token {'revoked at Google' if ok else 'NOT confirmed revoked (remove access at myaccount.google.com/permissions)'};"
          " local token file deleted.")
    return 0


def main(argv: list[str] | None = None) -> int:
    install_secret_redaction()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["status", "authorize", "revoke"])
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        return {"status": status, "authorize": authorize, "revoke": revoke_token}[args.command](settings)
    except IntegrationConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
