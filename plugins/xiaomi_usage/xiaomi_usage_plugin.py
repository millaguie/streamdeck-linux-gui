#!/usr/bin/env python3
"""Xiaomi MiMo Token Plan monitor for StreamDeck UI.

Xiaomi's Token Plan exposes three concurrent budgets via the
``platform.xiaomimimo.com`` console:

- ``plan``          monthly plan tokens (the headline credit budget)
- ``compensation``  bonus / compensation tokens
- ``monthly``       calendar-month tracker (always present)

The plugin tracks all three.  The headline display shows the *tightest*
(highest-utilisation) window so the badge reflects the binding
constraint — heavy bursts hit ``plan`` before ``monthly`` catches up.

The Token Plan ``tp-*`` API keys work only for model calls (against
``token-plan-{ams,cn,sgp}.xiaomimimo.com/v1``).  The usage console
rejects them, so this plugin requires a browser session cookie from
``platform.xiaomimimo.com`` for direct mode.  In sentinel mode the
cookie is configured on the daemon side.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

# Add parent directory to path to import base plugin
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from streamdeck_ui.plugin_system.base_plugin import BasePlugin
from streamdeck_ui.plugin_system.browser_cookies import CookieError, list_cookies
from streamdeck_ui.plugin_system.protocol import LogLevel

USAGE_URL = "https://platform.xiaomimimo.com/api/v1/tokenPlan/usage"
COOKIE_DOMAIN = "platform.xiaomimimo.com"
DEFAULT_OPENCODE_AUTH = str(Path.home() / ".local" / "share" / "opencode" / "auth.json")

# Order matters for "worst window" tie-breaks: when two windows share a
# utilisation the ``plan`` window is the more actionable one (it
# triggers a hard stop), so it comes first.
_WINDOW_ORDER = ("plan", "compensation", "monthly")
_WINDOW_LABELS = {
    "plan": "Plan",
    "compensation": "Cmp",
    "monthly": "Mon",
}

# Maps the ``items[].name`` strings returned by the console to plugin
# window ids.  Same mapping the sentinel uses — duplicated here so the
# direct-mode code path doesn't depend on quota-sentinel running.
_ITEM_TO_WINDOW: dict[str, str] = {
    "plan_total_token": "plan",
    "compensation_total_token": "compensation",
    "month_total_token": "monthly",
}

# opencode auth.json keys that may carry the Xiaomi Token Plan key.
# Token Plan ships per-region variants — ``ams`` is the European
# cluster, ``cn`` the China cluster, ``sgp`` the Singapore one.  The
# usage console is the same single host for all regions, so any one of
# these auth-key spellings unlocks fingerprinting.
_AUTH_KEYS = (
    "xiaomi-token-plan-ams",
    "xiaomi-token-plan-cn",
    "xiaomi-token-plan-sgp",
    "xiaomi-token-plan",
    "xiaomi-mimo",
    "xiaomi",
    "mimo-token-plan",
    "mimo",
)


class XiaomiUsagePlugin(BasePlugin):
    """Plugin for monitoring Xiaomi MiMo Token Plan budgets."""

    def __init__(self, socket_path: str, config: dict[str, Any]):
        super().__init__(socket_path, config)

        self.session_cookie = config.get("session_cookie", "")
        self.api_token = config.get("api_token", "")
        # ``browser`` is "" (legacy paste-the-cookie flow), or one of
        # "auto"/"firefox"/"chrome"/"brave"/"chromium" — when set the
        # plugin reads the cookie straight from the browser store.
        self.browser = (config.get("browser") or "").strip().lower()
        self.cookie_domain = (config.get("cookie_domain") or COOKIE_DOMAIN).strip()
        self.opencode_auth_path = (
            config.get("opencode_auth_path", "") or DEFAULT_OPENCODE_AUTH
        )
        self.poll_interval = max(int(config.get("poll_interval", 300)), 60)
        self.display_mode = config.get("display_mode", "compact")
        self.rotate_interval = int(config.get("rotate_interval", 5))

        self.quota_sentinel_url = (
            config.get("quota_sentinel_url", "").split("/v1")[0].rstrip("/")
        )
        self._sentinel_api_key = ""
        self._sentinel_instance_id = ""

        # State.  ``windows`` is the per-window dict described in
        # ``_compute_windows_from_console``; ``headline`` is the worst one.
        self.last_poll_time = 0.0
        self.windows: dict[str, dict[str, Any]] = {}
        self.headline: dict[str, Any] | None = None
        self.error_message: str | None = None
        self.current_view = 0
        self.last_rotate_time = 0.0

    # ------------------------------------------------------------------ key

    def _resolve_api_token(self) -> str:
        """Get the Token Plan ``tp-*`` key from config or opencode auth.json.

        The token is only used for fingerprinting in sentinel mode — the
        usage console doesn't accept it.
        """
        if self.api_token:
            return self.api_token
        try:
            with open(self.opencode_auth_path) as f:
                auth = json.load(f)
            for key_name in _AUTH_KEYS:
                entry = auth.get(key_name) or {}
                if entry.get("key"):
                    self.log(
                        LogLevel.INFO,
                        f"Using API key from opencode auth ({key_name})",
                    )
                    return str(entry["key"])
        except FileNotFoundError:
            self.log(
                LogLevel.WARNING,
                f"opencode auth not found: {self.opencode_auth_path}",
            )
        except (json.JSONDecodeError, KeyError) as e:
            self.log(LogLevel.ERROR, f"Failed to read opencode auth: {e}")
        return ""

    def _resolve_session_cookie(self) -> str:
        """Resolve the cookie header value.

        Priority:
        1. ``browser`` config option set → scrape the browser cookie store.
           Fail fast on ``CookieError`` so the user sees "browser cookie not
           found" instead of an opaque "Cookie expired" further down.
        2. Manual ``session_cookie`` config value (legacy).
        """
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
                "is the browser logged in?",
            )
        return (self.session_cookie or "").strip()

    def _build_cookie_header(self) -> str:
        """Accept either ``name=value`` pairs or a bare cookie value."""
        cookie = self._resolve_session_cookie()
        if "=" in cookie:
            return cookie
        return f"session={cookie}"

    # ----------------------------------------------------------- window math

    @staticmethod
    def _clamp_pct(value: Any) -> float:
        try:
            pct = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(100.0, pct))

    @staticmethod
    def _coerce_int(value: Any) -> int:
        if value is None:
            return 0
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _items_to_windows(
        cls, items: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            wid = _ITEM_TO_WINDOW.get(str(item.get("name") or ""))
            if not wid:
                continue
            used = cls._coerce_int(item.get("used"))
            limit = cls._coerce_int(item.get("limit"))
            # Prefer used/limit over the API's ``percent`` field — the
            # console returns ``percent`` as a truncated integer, so a
            # real 3.68% comes back as ``0``.  Only fall back to the API
            # value when ``limit`` is missing.
            if limit > 0:
                pct = cls._clamp_pct(used / limit * 100.0)
            elif item.get("percent") is not None:
                pct = cls._clamp_pct(item.get("percent"))
            else:
                pct = 0.0
            out[wid] = {
                "utilization": pct,
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used) if limit else 0,
                "renews_at": None,  # not exposed by the console
                "unit": "tok",
            }
        return out

    @classmethod
    def _compute_windows_from_console(
        cls, data: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Build per-window dict from the raw ``/tokenPlan/usage`` payload."""
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        usage = payload.get("usage") or {}
        if isinstance(usage, dict):
            out.update(cls._items_to_windows(usage.get("items") or []))
        month_usage = payload.get("monthUsage") or {}
        if isinstance(month_usage, dict):
            out.update(cls._items_to_windows(month_usage.get("items") or []))
        return out

    @staticmethod
    def _windows_from_sentinel(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Project sentinel's ``provider_detail`` shape onto the plugin's window dict."""
        out: dict[str, dict[str, Any]] = {}
        windows = payload.get("windows", {}) or {}
        for wid, w in windows.items():
            if not isinstance(w, dict):
                continue
            meta = w.get("metadata", {}) or {}
            used = int(meta.get("used_tokens") or 0)
            limit = int(meta.get("limit_tokens") or 0)
            out[wid] = {
                "utilization": float(w.get("utilization") or 0.0),
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used) if limit else 0,
                "renews_at": w.get("resets_at"),
                "unit": "tok",
            }
        return out

    @staticmethod
    def _pick_headline(
        windows: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any]] | None:
        if not windows:
            return None
        ranked = sorted(
            windows.items(),
            key=lambda kv: (
                -float(kv[1].get("utilization") or 0.0),
                _WINDOW_ORDER.index(kv[0]) if kv[0] in _WINDOW_ORDER else 99,
            ),
        )
        return ranked[0]

    # -------------------------------------------------------------- sentinel

    def _sentinel_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._sentinel_api_key:
            headers["X-API-Key"] = self._sentinel_api_key
        return headers

    def _register_with_sentinel(self) -> bool:
        if self._sentinel_api_key:
            return True
        cookie = self._resolve_session_cookie()
        if not cookie:
            self.log(
                LogLevel.WARNING,
                "no session_cookie set — sentinel registration needs it for the "
                "Xiaomi console (the tp-* API key is rejected by the usage endpoint)",
            )
            return False
        api_token = self._resolve_api_token()
        try:
            payload = {
                "project_name": "streamdeck-xiaomi",
                "framework": "opencode",
                "auth": {
                    "opencode_auth": {
                        "xiaomi-token-plan-ams": {"key": api_token or ""}
                    }
                },
                "provider_config": {
                    "xiaomi": {"session_cookie": cookie},
                },
            }
            resp = requests.post(
                f"{self.quota_sentinel_url}/v1/instances",
                json=payload,
                timeout=10,
            )
            if not resp.ok:
                self.log(
                    LogLevel.ERROR,
                    f"Sentinel registration HTTP {resp.status_code}: "
                    f"{resp.text[:300]} | cookie_len={len(cookie)} "
                    f"token={'set' if api_token else 'empty'}",
                )
                return False
            data = resp.json()
            self._sentinel_api_key = data.get("api_key", "")
            self._sentinel_instance_id = data.get("instance_id", "")
            self.log(
                LogLevel.INFO,
                f"Registered with Sentinel as {self._sentinel_instance_id}",
            )
            return bool(self._sentinel_api_key)
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Sentinel registration failed: {e}")
            return False

    def _provider_exists_in_sentinel(self, provider: str) -> bool:
        try:
            response = requests.get(
                f"{self.quota_sentinel_url}/v1/providers",
                headers=self._sentinel_headers(),
                timeout=10,
            )
            if response.status_code == 401:
                self.log(LogLevel.WARNING, "Sentinel auth expired, will re-register")
                self._sentinel_api_key = ""
                return False
            response.raise_for_status()
            providers = response.json()
            if isinstance(providers, dict):
                return provider in providers
            return provider in [
                p.get("name", p) if isinstance(p, dict) else p for p in providers
            ]
        except requests.exceptions.RequestException:
            return False

    def _fetch_from_sentinel(self) -> bool:
        try:
            if not self._register_with_sentinel():
                return False
            if not self._provider_exists_in_sentinel("xiaomi"):
                self.log(LogLevel.INFO, "Provider 'xiaomi' not registered in Sentinel")
                return False
            response = requests.get(
                f"{self.quota_sentinel_url}/v1/providers/xiaomi",
                headers=self._sentinel_headers(),
                timeout=10,
            )
            if response.status_code == 404:
                return False
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                self.error_message = "Sentinel\nerror"
                return False
            windows = self._windows_from_sentinel(data)
            if not windows:
                self.error_message = "no\nwindows"
                return False
            self.windows = windows
            head = self._pick_headline(windows)
            self.headline = (
                {"window": head[0], **head[1]} if head is not None else None
            )
            return True
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch from Sentinel: {e}")
            self.error_message = "Sentinel\nerror"
            return False

    # ------------------------------------------------------------------ direct

    def _fetch_direct(self) -> bool:
        cookie = self._resolve_session_cookie()
        if not cookie:
            self.error_message = "No\ncookie"
            return False
        try:
            response = requests.get(
                USAGE_URL,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Cookie": self._build_cookie_header(),
                    "Referer": "https://platform.xiaomimimo.com/",
                },
                timeout=10,
            )
            if response.status_code in (401, 403):
                self.error_message = "Cookie\nexpired"
                return False
            if response.status_code == 429:
                self.error_message = "Rate\nlimited"
                return False
            response.raise_for_status()
            data = response.json()
            code = data.get("code")
            if code not in (0, None):
                self.error_message = "API\nerror"
                return False
            windows = self._compute_windows_from_console(data)
            if not windows:
                self.error_message = "no\nwindows"
                return False
            self.windows = windows
            head = self._pick_headline(windows)
            self.headline = (
                {"window": head[0], **head[1]} if head is not None else None
            )
            return True
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch Xiaomi usage: {e}")
            self.error_message = "API\nerror"
            return False

    def _fetch_usage(self) -> bool:
        # Try sentinel first when configured, but fall back to direct on
        # failure — a misconfigured / outdated sentinel (e.g. running an
        # older version without the xiaomi provider) shouldn't black out
        # the badge as long as we still have a working cookie.
        if self.quota_sentinel_url and self._fetch_from_sentinel():
            self.error_message = None
            return True
        return self._fetch_direct()

    # ---------------------------------------------------------------- rendering

    @staticmethod
    def _bar_color(pct: float) -> tuple[int, int, int]:
        if pct >= 95:
            return (183, 28, 28)
        if pct >= 80:
            return (230, 81, 0)
        if pct >= 50:
            return (245, 127, 23)
        return (21, 101, 192)

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        for path in [
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
        ]:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _format_tokens(value: int) -> str:
        """Compact rendering for token counts: 12_345_678 → '12M'."""
        if value >= 1_000_000_000:
            return f"{value / 1_000_000_000:.1f}B"
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.0f}k"
        return str(value)

    @classmethod
    def _format_used(cls, window: dict[str, Any]) -> str:
        used = int(window.get("used") or 0)
        limit = int(window.get("limit") or 0)
        if not limit:
            return cls._format_tokens(used)
        return f"{cls._format_tokens(used)}/{cls._format_tokens(limit)}"

    def _update_display(self) -> None:
        try:
            if self.error_message and not self.headline:
                self.update_image_render(
                    text=f"MiMo\n{self.error_message}",
                    background_color="#B71C1C",
                    font_color="#FFFFFF",
                    font_size=10,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return
            if not self.headline:
                self.update_image_render(
                    text="MiMo\n…",
                    background_color="#37474F",
                    font_color="#FFFFFF",
                    font_size=11,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return
            if self.display_mode == "rotate":
                self._render_rotate()
            else:
                self._render_compact()
        except Exception as e:  # noqa: BLE001
            self.log(LogLevel.ERROR, f"Display update failed: {e}")

    def _render_compact(self) -> None:
        """Show up to 2 windows side-by-side."""
        ordered = [(w, self.windows[w]) for w in _WINDOW_ORDER if w in self.windows]
        if not ordered:
            return

        img = Image.new("RGB", (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_title = self._load_font(10)
        font_label = self._load_font(9)

        worst_pct = max(float(w.get("utilization") or 0.0) for _, w in ordered)
        dot = (
            (76, 175, 80) if worst_pct < 80
            else (245, 127, 23) if worst_pct < 95
            else (183, 28, 28)
        )
        draw.ellipse([2, 2, 8, 8], fill=dot)
        draw.text((12, 2), "MiMo", fill=(255, 255, 255), font=font_title)

        y = 16
        for wid, w in ordered[:2]:
            pct = float(w.get("utilization") or 0.0)
            label = _WINDOW_LABELS.get(wid, wid[:3])
            draw.text((4, y), label, fill=(180, 180, 180), font=font_label)
            draw.text(
                (68, y), f"{pct:.0f}%", fill=(255, 255, 255),
                font=font_label, anchor="rt",
            )
            bar_y = y + 11
            draw.rectangle(
                [4, bar_y, 68, bar_y + 6], fill=(30, 30, 30), outline=(80, 80, 80)
            )
            fill_w = int(64 * min(pct, 100) / 100)
            if fill_w > 0:
                draw.rectangle(
                    [5, bar_y + 1, 4 + fill_w, bar_y + 5], fill=self._bar_color(pct)
                )
            y += 22

        self.update_image_raw(img)

    def _render_rotate(self) -> None:
        """Cycle one window per tick across whichever windows are exposed."""
        ordered = [w for w in _WINDOW_ORDER if w in self.windows]
        if not ordered:
            self._render_compact()
            return
        idx = self.current_view % len(ordered)
        wid = ordered[idx]
        w = self.windows[wid]
        pct = float(w.get("utilization") or 0.0)
        wlabel = _WINDOW_LABELS.get(wid, wid[:3])

        img = Image.new("RGB", (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_title = self._load_font(10)
        font_value = self._load_font(13)
        font_small = self._load_font(9)

        dot = (
            (76, 175, 80) if pct < 80
            else (245, 127, 23) if pct < 95
            else (183, 28, 28)
        )
        draw.ellipse([2, 2, 8, 8], fill=dot)
        draw.text((12, 2), f"MiMo·{wlabel}", fill=(255, 255, 255), font=font_title)

        draw.text(
            (36, 18), f"{pct:.0f}%",
            fill=self._bar_color(pct), font=font_value, anchor="mt",
        )
        draw.text((4, 38), self._format_used(w), fill=(180, 180, 180), font=font_small)
        draw.text((4, 50), "tokens", fill=(180, 180, 180), font=font_small)

        draw.rectangle([4, 63, 68, 69], fill=(30, 30, 30), outline=(80, 80, 80))
        fill_w = int(64 * min(pct, 100) / 100)
        if fill_w > 0:
            draw.rectangle([5, 64, 4 + fill_w, 68], fill=self._bar_color(pct))

        self.update_image_raw(img)

    # ----------------------------------------------------------- lifecycle

    def on_start(self) -> None:
        self.log(LogLevel.INFO, "Xiaomi MiMo Usage plugin started")
        self.log(
            LogLevel.INFO,
            f"Poll interval: {self.poll_interval}s, display: {self.display_mode}",
        )
        self._update_display()

    def on_button_pressed(self) -> None:
        self.log(LogLevel.INFO, "Button pressed, forcing refresh")
        if self._fetch_usage():
            self.error_message = None
        self.last_poll_time = time.time()
        self._update_display()

    def on_button_released(self) -> None:
        pass

    def on_button_visible(self, page: int, button: int) -> None:
        self._update_display()

    def on_button_hidden(self) -> None:
        pass

    def on_config_update(self, config: dict[str, Any]) -> None:
        self.session_cookie = config.get("session_cookie", "")
        self.api_token = config.get("api_token", "")
        self.browser = (config.get("browser") or "").strip().lower()
        self.cookie_domain = (config.get("cookie_domain") or COOKIE_DOMAIN).strip()
        self.opencode_auth_path = (
            config.get("opencode_auth_path", "") or DEFAULT_OPENCODE_AUTH
        )
        self.poll_interval = max(int(config.get("poll_interval", 300)), 60)
        self.display_mode = config.get("display_mode", "compact")
        self.rotate_interval = int(config.get("rotate_interval", 5))
        self.quota_sentinel_url = (
            config.get("quota_sentinel_url", "").split("/v1")[0].rstrip("/")
        )
        self._sentinel_api_key = ""
        self._sentinel_instance_id = ""
        self.log(LogLevel.INFO, "Configuration updated")
        if self._fetch_usage():
            self.error_message = None
        self.last_poll_time = time.time()
        self._update_display()

    def update(self) -> None:
        current_time = time.time()
        if current_time - self.last_poll_time >= self.poll_interval:
            if self._fetch_usage():
                self.error_message = None
            self.last_poll_time = current_time
            self._update_display()
        if self.display_mode == "rotate" and self.windows:
            if current_time - self.last_rotate_time >= self.rotate_interval:
                self.current_view += 1
                self.last_rotate_time = current_time
                self._update_display()


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: xiaomi_usage_plugin.py <socket_path> <config_json>")
        sys.exit(1)
    socket_path = sys.argv[1]
    try:
        config = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid config JSON: {e}")
        sys.exit(1)
    plugin = XiaomiUsagePlugin(socket_path, config)
    plugin.run()


if __name__ == "__main__":
    main()
