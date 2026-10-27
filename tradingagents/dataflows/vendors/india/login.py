"""Daily Kite Connect sign-in: browser login -> request_token -> access_token on disk.

Kite access tokens expire every morning (~06:00 IST), so this runs once per
trading day. The Kite app's redirect URL must be ``http://127.0.0.1:<port>/``
(default 5000) for the token to be captured automatically; otherwise pass the
``request_token`` from the redirected address bar with ``--request-token``.
"""

from __future__ import annotations

import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from tradingagents.dataflows.errors import VendorNotConfiguredError
from tradingagents.dataflows.vendors.india.kite import save_access_token


class KiteLoginError(Exception):
    pass


def _capture_request_token(port: int, timeout: float) -> str:
    box: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            token = (qs.get("request_token") or [""])[0]
            ok = bool(token) and (qs.get("status") or ["success"])[0] == "success"
            if ok:
                box["token"] = token
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Kite sign-in captured. You can close this tab." if ok else "No request_token in this redirect."
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h3>{msg}</h3></body></html>".encode())

        def log_message(self, *a):  # silence
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1.0
    stop = threading.Event()
    threading.Timer(timeout, stop.set).start()
    try:
        while "token" not in box and not stop.is_set():
            server.handle_request()
    finally:
        server.server_close()
    if "token" not in box:
        raise KiteLoginError(f"no Kite redirect reached http://127.0.0.1:{port}/ within {int(timeout)}s")
    return box["token"]


def sign_in(request_token: str | None = None, port: int | None = None, timeout: float = 300,
            open_browser: bool = True, say=print) -> tuple[str, str]:
    """Exchange a request token for today's access token; returns (user_id, token file)."""
    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise VendorNotConfiguredError("set KITE_API_KEY and KITE_API_SECRET (in .env) first")
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:
        raise KiteLoginError('kiteconnect is not installed: pip install "tradingagents[india]"') from exc

    kite = KiteConnect(api_key=api_key)
    if not request_token:
        port = port or int(os.getenv("KITE_REDIRECT_PORT", "5000"))
        url = kite.login_url()
        say(f"Sign in to Kite: {url}")
        if open_browser:
            webbrowser.open(url)
        request_token = _capture_request_token(port, timeout)
    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:  # noqa: BLE001
        raise KiteLoginError(f"Kite refused the request token: {type(exc).__name__}: {exc}") from exc
    path = save_access_token(session["access_token"], session.get("user_id", ""))
    return session.get("user_id", ""), path
