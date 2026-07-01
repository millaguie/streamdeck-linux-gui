#!/usr/bin/env python3
"""Alibaba Token Plan (Team Edition) usage monitor for StreamDeck UI.

Feeder + display for the Alibaba Cloud Model Studio **Token Plan (Team
Edition)** Credits balance (the seat-based subscription the Hermes fleet
runs on).  There is **no API-key usage endpoint** for the Token Plan; the
only way to read remaining Credits is the undocumented console RPC
``GetSubscriptionSummary`` (BssOpenAPI-V3), gated behind the operator's
logged-in aliyun/alibabacloud console session.

This plugin scrapes that session cookie (and optional ``sec_token``) from
the browser and registers it with Quota Sentinel, whose
``alibaba_token_plan`` provider performs the actual RPC and normalises the
result to a single ``credits`` window.  The badge then renders the Credits
utilisation reported back by the sentinel.

⚠️ 0.1.0 scaffold.  Before relying on it, verify against one real console
capture (DevTools → Network → the ``GetSubscriptionSummary`` request):
  - ``cookie_domain`` (intl ~ ``alibabacloud.com``, cn ~ ``aliyun.com``);
  - whether ``sec_token`` is needed and where it lives;
  - the intl ``ProductCode`` (sentinel side: ``sfm_tokenplanteams_dp_intl``?).
See ``NOTES-alibaba-token-plan.md`` in the quota-sentinel repo.
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from streamdeck_ui.plugin_system.base_plugin import BasePlugin
from streamdeck_ui.plugin_system.browser_cookies import CookieError, list_cookies
from streamdeck_ui.plugin_system.protocol import LogLevel

# Auth-key understood by the sentinel's AUTH_KEY_TO_PROVIDER map, and the
# resolved provider name used to key provider_config / query results.
SENTINEL_AUTH_KEY = "alibaba-token-plan"
SENTINEL_PROVIDER = "alibaba_token_plan"

# ⚠️ VERIFY against a real login session — the console RPC host is
# modelstudio.console.alibabacloud.com (intl) / bailian.console.aliyun.com (cn);
# the login session cookie typically lives on the parent domain.
DEFAULT_COOKIE_DOMAIN = {"intl": "alibabacloud.com", "cn": "aliyun.com"}


class AlibabaTokenPlanUsagePlugin(BasePlugin):
    """Feeds the aliyun console cookie to Quota Sentinel and renders Credits."""

    def __init__(self, socket_path: str, config: dict[str, Any]):
        super().__init__(socket_path, config)
        self._apply_config(config)
        # State
        self.last_poll_time = 0.0
        self.credits: dict[str, Any] | None = None  # {pct,total,remaining,reset_ms}
        self.error_message: str | None = None

    def _apply_config(self, config: dict[str, Any]) -> None:
        self.api_key = config.get("api_key", "")
        self.region = (config.get("region") or "intl").strip().lower()
        self.browser = (config.get("browser") or "").strip().lower()
        self.cookie_domain = (
            config.get("cookie_domain")
            or DEFAULT_COOKIE_DOMAIN.get(self.region, "alibabacloud.com")
        ).strip()
        self.session_cookie = config.get("session_cookie", "")
        # The Token Plan always needs the console session cookie. If the
        # operator pinned neither a browser to scrape from nor a manual
        # cookie, default to scraping Chrome (where the console login lives).
        if not self.browser and not self.session_cookie:
            self.browser = "chrome"
        self.sec_token = (config.get("sec_token") or "").strip()
        self.poll_interval = max(int(config.get("poll_interval", 300)), 60)
        self.quota_sentinel_url = (
            config.get("quota_sentinel_url", "").split("/v1")[0].rstrip("/")
        )
        self._sentinel_api_key = ""
        self._sentinel_instance_id = ""

    # ── credential resolution ────────────────────────────────────────────
    def _resolve_session_cookie(self) -> str:
        if self.browser:
            try:
                cookies = list_cookies(self.cookie_domain, browser=self.browser)
            except CookieError as e:
                self.log(LogLevel.WARNING, f"browser cookie lookup failed: {e}")
                cookies = {}
            if cookies:
                return "; ".join(f"{k}={v}" for k, v in cookies.items())
            self.log(
                LogLevel.WARNING,
                f"no cookies for {self.cookie_domain} in {self.browser} — "
                "is the console logged in?",
            )
        return (self.session_cookie or "").strip()

    def _sentinel_headers(self) -> dict[str, str]:
        return {"X-API-Key": self._sentinel_api_key} if self._sentinel_api_key else {}

    # ── sentinel I/O ─────────────────────────────────────────────────────
    def _register_with_sentinel(self) -> bool:
        if self._sentinel_api_key:
            return True
        cookie = self._resolve_session_cookie()
        if not cookie:
            self.error_message = "No\ncookie"
            self.log(
                LogLevel.WARNING,
                "no session cookie — the Token Plan console RPC needs the "
                "logged-in aliyun console session (API key alone is rejected)",
            )
            return False
        # The sentinel requires a non-empty 'key' in the auth entry for
        # fingerprinting; fall back to the cookie hash when no workspace key
        # is configured so distinct logins still register distinctly.
        key = self.api_key or f"cookie:{hash(cookie) & 0xFFFFFFFF:08x}"
        try:
            payload: dict[str, Any] = {
                "project_name": "streamdeck-alibaba-token-plan",
                "framework": "opencode",
                "auth": {"opencode_auth": {SENTINEL_AUTH_KEY: {"key": key}}},
                "provider_config": {
                    SENTINEL_PROVIDER: {
                        "session_cookie": cookie,
                        "sec_token": self.sec_token,
                        "region": self.region,
                    }
                },
            }
            resp = requests.post(
                f"{self.quota_sentinel_url}/v1/instances", json=payload, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            self._sentinel_api_key = data.get("api_key", "")
            self._sentinel_instance_id = data.get("instance_id", "")
            self.log(
                LogLevel.INFO, f"Registered with Sentinel as {self._sentinel_instance_id}"
            )
            return bool(self._sentinel_api_key)
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Sentinel registration failed: {e}")
            return False

    def _fetch_from_sentinel(self) -> bool:
        if not self.quota_sentinel_url:
            self.error_message = "No\nsentinel"
            return False
        if not self._register_with_sentinel():
            return False
        try:
            resp = requests.get(
                f"{self.quota_sentinel_url}/v1/providers/{SENTINEL_PROVIDER}",
                headers=self._sentinel_headers(),
                timeout=10,
            )
            if resp.status_code == 401:
                self._sentinel_api_key = ""  # force re-register next tick
                return False
            if resp.status_code == 404:
                self.log(LogLevel.INFO, "Token Plan provider not registered yet")
                return False
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                self.error_message = "Cookie\nexpired"
                return False
            window = (data.get("windows") or {}).get("credits")
            if not window:
                return False
            meta = window.get("metadata") or {}
            reset_ms = 0
            reset_iso = window.get("resets_at")
            if reset_iso:
                try:
                    from datetime import datetime, timezone

                    dt = datetime.fromisoformat(reset_iso)
                    reset_ms = max(
                        0, int((dt - datetime.now(timezone.utc)).total_seconds() * 1000)
                    )
                except Exception:
                    pass
            self.credits = {
                "pct": float(window.get("utilization", 0) or 0),
                "total": meta.get("total_credits"),
                "remaining": meta.get("remaining_credits"),
                "reset_ms": reset_ms,
            }
            self.error_message = None
            return True
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch from Sentinel: {e}")
            self.error_message = "Sentinel\nerror"
            return False

    # ── display ──────────────────────────────────────────────────────────
    @staticmethod
    def _bar_color(pct: float) -> tuple[int, int, int]:
        if pct >= 90:
            return (183, 28, 28)
        if pct >= 75:
            return (230, 81, 0)
        if pct >= 50:
            return (245, 127, 23)
        if pct >= 25:
            return (27, 94, 32)
        return (21, 101, 192)

    @staticmethod
    def _fmt(n: Any) -> str:
        try:
            n = float(n)
        except (TypeError, ValueError):
            return "?"
        if n >= 1_000_000:
            return f"{n / 1e6:.1f}M"
        if n >= 1000:
            return f"{n / 1e3:.1f}k"
        return str(int(n))

    @staticmethod
    def _fmt_reset(ms: int) -> str:
        if ms <= 0:
            return ""
        h = ms // 3_600_000
        if h >= 24:
            return f"{h // 24}d"
        if h > 0:
            return f"{h}h"
        return f"{ms // 60000}m"

    def _load_font(self, size: int):
        for p in (
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _update_display(self) -> None:
        try:
            if self.error_message and not self.credits:
                self.update_image_render(
                    text=f"TokenPlan\n{self.error_message}",
                    background_color="#B71C1C",
                    font_color="#FFFFFF",
                    font_size=11,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return
            if not self.credits:
                self.update_image_render(
                    text="TokenPlan\n...",
                    background_color="#37474F",
                    font_color="#FFFFFF",
                    font_size=12,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return
            c = self.credits
            pct = c["pct"]
            img = Image.new("RGB", (72, 72), (20, 20, 20))
            draw = ImageDraw.Draw(img)
            draw.text((4, 4), "Token Plan", fill=(255, 255, 255), font=self._load_font(11))
            draw.text(
                (4, 20), f"{pct:.0f}% used", fill=(255, 255, 255), font=self._load_font(12)
            )
            draw.rectangle([4, 38, 68, 48], fill=(30, 30, 30), outline=(80, 80, 80))
            fill_w = int(64 * min(pct, 100) / 100)
            if fill_w > 0:
                draw.rectangle([5, 39, 4 + fill_w, 47], fill=self._bar_color(pct))
            rem = self._fmt(c.get("remaining"))
            tot = self._fmt(c.get("total"))
            reset = self._fmt_reset(c.get("reset_ms", 0))
            draw.text(
                (4, 54),
                f"{rem}/{tot} Cr {reset}".strip(),
                fill=(180, 180, 180),
                font=self._load_font(9),
            )
            self.update_image_raw(img)
        except Exception as e:
            self.log(LogLevel.ERROR, f"Display update failed: {e}")

    # ── lifecycle ────────────────────────────────────────────────────────
    def on_start(self) -> None:
        self.log(LogLevel.INFO, f"Alibaba Token Plan plugin started (region={self.region})")
        self._update_display()

    def on_button_pressed(self) -> None:
        self._fetch_from_sentinel()
        self.last_poll_time = time.time()
        self._update_display()

    def on_button_released(self) -> None:
        pass

    def on_button_visible(self, page: int, button: int) -> None:
        self._update_display()

    def on_button_hidden(self) -> None:
        pass

    def on_config_update(self, config: dict[str, Any]) -> None:
        self._apply_config(config)
        self.log(LogLevel.INFO, "Configuration updated")
        self._fetch_from_sentinel()
        self.last_poll_time = time.time()
        self._update_display()

    def update(self) -> None:
        now = time.time()
        if now - self.last_poll_time >= self.poll_interval:
            self._fetch_from_sentinel()
            self.last_poll_time = now
            self._update_display()


def main():
    if len(sys.argv) < 3:
        print("Usage: alibaba_tokenplan_usage_plugin.py <socket_path> <config_json>")
        sys.exit(1)
    socket_path = sys.argv[1]
    try:
        config = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid config JSON: {e}")
        sys.exit(1)
    AlibabaTokenPlanUsagePlugin(socket_path, config).run()


if __name__ == "__main__":
    main()
