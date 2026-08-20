from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    pass


def safe_filename(filename: str) -> str:
    name = Path(filename).name.strip().replace("\x00", "")
    name = re.sub(r"[^\w.\-() ]+", "_", name, flags=re.UNICODE)
    return name[:180] or "upload"


def validate_public_url(url: str, *, allow_private_networks: bool = False) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only http and https URLs are allowed.")
    if not parsed.hostname:
        raise UnsafeUrlError("The URL must include a hostname.")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("Credentials in URLs are not allowed.")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} and not allow_private_networks:
        raise UnsafeUrlError("Local network URLs are disabled.")

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve hostname: {hostname}") from exc

    if not allow_private_networks:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise UnsafeUrlError(f"Non-public destination is blocked: {address}")
    return url


def sign_action_token(
    payload: dict[str, str],
    secret: str,
    *,
    expires_in_seconds: int = 7 * 24 * 60 * 60,
) -> str:
    body = dict(payload)
    body["exp"] = str(int(time.time()) + expires_in_seconds)
    encoded = base64.urlsafe_b64encode(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).digest()
    return (
        encoded.decode("ascii")
        + "."
        + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    )


def verify_action_token(token: str, secret: str) -> dict[str, str]:
    try:
        encoded_text, signature_text = token.split(".", 1)
        encoded = encoded_text.encode("ascii")
        padding = "=" * (-len(signature_text) % 4)
        signature = base64.urlsafe_b64decode(signature_text + padding)
        expected = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid signature.")
        body_padding = "=" * (-len(encoded_text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded_text + body_padding))
        if int(payload["exp"]) < int(time.time()):
            raise ValueError("Token has expired.")
        return {str(key): str(value) for key, value in payload.items()}
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid or expired action token.") from exc


def webhook_signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

