"""Extract session cookies from local browser stores.

Plugins that authenticate via a browser session (xiaomi, crof, opencode_go,
…) traditionally take a pasted ``session_cookie`` config value.  Pasting
cookies is fragile — the user has to find the right one in DevTools and
re-paste whenever the session rotates.

This module reads cookies directly from the browser profile on disk so the
plugin can refresh itself.  Firefox stores cookies in plaintext SQLite and
needs no external deps; Chromium variants (Chrome / Brave / Chromium / etc.)
encrypt values with AES-CBC and require ``cryptography`` (already a
streamdeck-gui-ng transitive dep) plus the ``secret-tool`` CLI for the
libsecret keyring lookup.

The browser file is **copied** to a temp path before opening so concurrent
browser writes don't trip a "database is locked" error.

Public API:
    list_cookies(domain, browser="auto") -> dict[str, str]
    get_cookie(domain, name=None, browser="auto") -> str
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

# AES-CBC parameters used by all Chromium derivatives on Linux.  Match
# Chromium's ``os_crypt_linux.cc`` — salt + IV + KDF rounds are constants
# baked into the source, the only per-browser knob is the keyring lookup.
_CHROMIUM_SALT = b"saltysalt"
_CHROMIUM_IV = b" " * 16
_CHROMIUM_KEY_LEN = 16
# Hardcoded fallback when no libsecret entry exists (used for v10 prefix
# values, or when the browser was started with ``--password-store=basic``).
_CHROMIUM_FALLBACK_PASS = b"peanuts"

_FIREFOX_ROOTS: tuple[Path, ...] = (Path.home() / ".mozilla" / "firefox",)

# Default cookie-store paths.  Plugins can override via the ``browser``
# config option, but ``auto`` walks this list.
_CHROMIUM_PROFILES: dict[str, Path] = {
    "chrome":   Path.home() / ".config" / "google-chrome" / "Default" / "Cookies",
    "brave":    Path.home() / ".config" / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies",
    "chromium": Path.home() / ".config" / "chromium" / "Default" / "Cookies",
}

# secret-tool ``application`` attribute each Chromium fork registers under.
_KEYRING_APPS: dict[str, str] = {
    "chrome": "chrome",
    "brave": "brave",
    "chromium": "chromium",
}

_BROWSER_ORDER: tuple[str, ...] = ("firefox", "brave", "chrome", "chromium")


class CookieError(RuntimeError):
    """Raised when no matching cookie can be located/decrypted."""


# --------------------------------------------------------------- sqlite helper

def _query_sqlite_copy(db_path: Path, query: str, params: tuple) -> list[tuple]:
    """Copy ``db_path`` to tmp, then run ``query``.  The copy step matters:
    Firefox + Chromium hold an exclusive write lock on the live file."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        shutil.copyfile(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# --------------------------------------------------------------------- firefox

def _firefox_profile_paths() -> list[Path]:
    out: list[Path] = []
    for root in _FIREFOX_ROOTS:
        if not root.is_dir():
            continue
        for profile_dir in root.iterdir():
            db = profile_dir / "cookies.sqlite"
            if db.is_file():
                out.append(db)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def _domain_match_keys(domain: str) -> list[str]:
    """All ``host_key`` values whose cookies a browser would send to ``domain``.

    Per RFC 6265 § 5.1.3 a stored cookie matches a request domain if
    ``stored_domain == request_domain`` or ``request_domain`` ends with
    ``"." + stored_domain``.  So for ``platform.xiaomimimo.com`` we want
    ``platform.xiaomimimo.com``, ``.platform.xiaomimimo.com``,
    ``.xiaomimimo.com``, ``.com`` …  (Two-label public-suffix entries
    like ``.com`` are harmless: browsers don't store cookies that
    short.)
    """
    parts = domain.split(".")
    keys: list[str] = [domain]
    for i in range(len(parts)):
        keys.append("." + ".".join(parts[i:]))
    return keys


def _firefox_cookies(domain: str) -> dict[str, str]:
    out: dict[str, str] = {}
    keys = _domain_match_keys(domain)
    placeholders = ",".join("?" * len(keys))
    for db in _firefox_profile_paths():
        try:
            rows = _query_sqlite_copy(
                db,
                f"SELECT name, value FROM moz_cookies WHERE host IN ({placeholders})",
                tuple(keys),
            )
        except sqlite3.DatabaseError:
            continue
        for name, value in rows:
            out.setdefault(name, value)
    return out


# ----------------------------------------------------------------- chromium

def _chromium_password(browser: str) -> bytes:
    app = _KEYRING_APPS.get(browser, browser)
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", app],
            capture_output=True, timeout=5, check=False,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.rstrip(b"\n")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return _CHROMIUM_FALLBACK_PASS


def _chromium_decrypt(encrypted: bytes, password: bytes) -> str:
    """Decrypt a Chromium ``encrypted_value`` blob.

    Chromium prefixes each blob with the 3-byte version tag ``v10`` or
    ``v11``.  ``v11`` additionally prepends a SHA-256 of the cookie host
    inside the plaintext as a tamper check; we strip it after decrypt.
    """
    if not encrypted:
        return ""
    if encrypted[:3] not in (b"v10", b"v11"):
        try:
            return encrypted.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=_CHROMIUM_KEY_LEN,
        salt=_CHROMIUM_SALT,
        iterations=1,
    )
    key = kdf.derive(password)
    cipher = Cipher(algorithms.AES(key), modes.CBC(_CHROMIUM_IV))
    decryptor = cipher.decryptor()
    padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
    pad_len = padded[-1] if padded else 0
    plain = padded[:-pad_len] if 1 <= pad_len <= 16 else padded
    if encrypted[:3] == b"v11" and len(plain) >= 32:
        # v11 prepends a 32-byte binary SHA-256.  Strip only if doing so
        # yields a clean UTF-8 string — otherwise the layout was different
        # (e.g. older Brave builds).
        candidate = plain[32:]
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            return plain.decode("utf-8", errors="replace")
    return plain.decode("utf-8", errors="replace")


def _chromium_cookies(browser: str, domain: str) -> dict[str, str]:
    db_path = _CHROMIUM_PROFILES.get(browser)
    if not db_path or not db_path.is_file():
        return {}
    keys = _domain_match_keys(domain)
    placeholders = ",".join("?" * len(keys))
    try:
        rows = _query_sqlite_copy(
            db_path,
            f"SELECT name, encrypted_value FROM cookies "
            f"WHERE host_key IN ({placeholders})",
            tuple(keys),
        )
    except sqlite3.DatabaseError:
        return {}
    password = _chromium_password(browser)
    out: dict[str, str] = {}
    for name, encrypted in rows:
        try:
            value = _chromium_decrypt(bytes(encrypted), password)
        except Exception:  # noqa: BLE001
            continue
        if value:
            out.setdefault(name, value)
    return out


# --------------------------------------------------------------------- public

def list_cookies(domain: str, browser: str = "auto") -> dict[str, str]:
    """Return ``{cookie_name: value}`` for ``domain`` from the chosen browser.

    ``browser="auto"`` walks Firefox first, then the Chromium variants in
    ``_BROWSER_ORDER``, merging results (first hit wins).
    """
    browsers: Iterable[str]
    if browser == "auto":
        browsers = _BROWSER_ORDER
    else:
        browsers = (browser,)
    merged: dict[str, str] = {}
    for b in browsers:
        if b == "firefox":
            cur = _firefox_cookies(domain)
        elif b in _CHROMIUM_PROFILES:
            cur = _chromium_cookies(b, domain)
        else:
            continue
        for k, v in cur.items():
            merged.setdefault(k, v)
        if merged and browser != "auto":
            break
    return merged


def get_cookie(domain: str, name: str | None = None, browser: str = "auto") -> str:
    """Return ``"name=value"`` for one cookie, or the full ``Cookie:`` header
    value (``"k1=v1; k2=v2"``) when ``name`` is None.
    """
    cookies = list_cookies(domain, browser=browser)
    if name:
        v = cookies.get(name)
        if v is None:
            raise CookieError(f"cookie {name!r} not found for {domain}")
        return f"{name}={v}"
    if not cookies:
        raise CookieError(f"no cookies found for {domain}")
    return "; ".join(f"{k}={v}" for k, v in cookies.items())
