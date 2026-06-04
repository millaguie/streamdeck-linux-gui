#!/usr/bin/env python3
"""OpenCode Go subscription usage monitoring plugin for StreamDeck UI.

OpenCode Go (https://opencode.ai/docs/go/) has no public usage API —
anomalyco/opencode#10448 and #18648 are still open upstream. The only
way to read the user's $/5h, weekly and monthly utilisation today is to
scrape the workspace dashboard (``https://opencode.ai/workspace/<id>/go``),
which embeds the numbers in a SolidJS SSR hydration payload.

The plugin has two modes:

1. **Sentinel mode** (preferred): set ``quota_sentinel_url`` and the
   plugin will read pre-aggregated usage from ``/v1/providers/opencode_go``.
   The sentinel handles workspace_id + auth_cookie centrally and shares
   them across every other consumer (Hefestia, web UI, etc.).

2. **Direct mode**: leave ``quota_sentinel_url`` empty and provide
   ``workspace_id`` + ``auth_cookie`` so the plugin scrapes the
   dashboard itself. Same regex as the slkiser/opencode-quota project.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from streamdeck_ui.plugin_system.base_plugin import BasePlugin  # noqa: E402
from streamdeck_ui.plugin_system.browser_cookies import CookieError, list_cookies  # noqa: E402
from streamdeck_ui.plugin_system.protocol import LogLevel  # noqa: E402

DASHBOARD_URL = "https://opencode.ai/workspace/{workspace_id}/go"
COOKIE_DOMAIN = "opencode.ai"
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Regexes for the SolidJS SSR hydration payload (see slkiser/opencode-quota).
_NUM = r"(-?\d+(?:\.\d+)?)"


def _make_window_patterns(key: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    pct_first = re.compile(
        rf"{key}:\$R\[\d+\]=\{{[^}}]*usagePercent:{_NUM}[^}}]*resetInSec:{_NUM}[^}}]*\}}"
    )
    reset_first = re.compile(
        rf"{key}:\$R\[\d+\]=\{{[^}}]*resetInSec:{_NUM}[^}}]*usagePercent:{_NUM}[^}}]*\}}"
    )
    return pct_first, reset_first


_WINDOW_PATTERNS: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    "rolling_5h": _make_window_patterns("rollingUsage"),
    "weekly": _make_window_patterns("weeklyUsage"),
    "monthly": _make_window_patterns("monthlyUsage"),
}


def _parse_window(
    html: str, patterns: tuple[re.Pattern[str], re.Pattern[str]]
) -> tuple[float, float] | None:
    pct_first, reset_first = patterns
    m = pct_first.search(html)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except (TypeError, ValueError):
            pass
    m = reset_first.search(html)
    if m:
        try:
            return float(m.group(2)), float(m.group(1))
        except (TypeError, ValueError):
            pass
    return None


class OpencodeGoUsagePlugin(BasePlugin):
    """Plugin for monitoring OpenCode Go subscription utilisation."""

    def __init__(self, socket_path: str, config: dict[str, Any]):
        super().__init__(socket_path, config)

        self.workspace_id = (config.get("workspace_id") or "").strip()
        self.auth_cookie = (config.get("auth_cookie") or "").strip()
        self.browser = (config.get("browser") or "").strip().lower()
        self.cookie_domain = (config.get("cookie_domain") or COOKIE_DOMAIN).strip()
        self.poll_interval = max(int(config.get("poll_interval", 300)), 60)
        self.display_mode = config.get("display_mode", "compact")
        self.rotate_interval = int(config.get("rotate_interval", 5))

        self.quota_sentinel_url = (
            (config.get("quota_sentinel_url") or "").split("/v1")[0].rstrip("/")
        )
        self._sentinel_api_key = ""
        self._sentinel_instance_id = ""

        # State: {window_name: utilization_percent}
        self.windows: dict[str, float] = {}
        self.resets: dict[str, float] = {}  # seconds-until-reset
        self.last_poll_time = 0.0
        self.error_message: str | None = None
        self.has_data = False
        self.current_view = 0
        self.last_rotate_time = 0.0

    # ── Sentinel-mode helpers ──────────────────────────────────────────────

    def _sentinel_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._sentinel_api_key:
            headers["X-API-Key"] = self._sentinel_api_key
        return headers

    def _register_with_sentinel(self) -> bool:
        if self._sentinel_api_key:
            return True
        # Sentinel needs *something* to register against — we use the same
        # workspace_id+auth_cookie as direct mode and let the sentinel's
        # provider_config:opencode_go take precedence later if it exists.
        cookie = self._resolve_auth_cookie()
        if not self.workspace_id or not cookie:
            self.log(
                LogLevel.INFO,
                "Sentinel mode set but workspace_id/auth_cookie are empty — "
                "the sentinel needs them to seed registration",
            )
            return False
        try:
            payload = {
                "project_name": "streamdeck-opencode-go",
                "framework": "opencode",
                "auth": {"opencode_auth": {"opencode-go": {"key": "streamdeck"}}},
                "provider_config": {
                    "opencode_go": {
                        "workspace_id": self.workspace_id,
                        "auth_cookie": cookie,
                    }
                },
            }
            resp = requests.post(
                f"{self.quota_sentinel_url}/v1/instances",
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
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

    def _fetch_from_sentinel(self) -> bool:
        try:
            if not self._register_with_sentinel():
                # Maybe another consumer already registered; query directly.
                pass
            response = requests.get(
                f"{self.quota_sentinel_url}/v1/providers/opencode_go",
                headers=self._sentinel_headers(),
                timeout=10,
            )
            if response.status_code == 404:
                self.error_message = "Not\nregistered"
                return False
            if response.status_code == 401:
                self.log(LogLevel.WARNING, "Sentinel auth expired, will re-register")
                self._sentinel_api_key = ""
                return False
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                self.error_message = "Sentinel\nerror"
                return False
            windows = data.get("windows", {})
            self.windows = {
                name: float(w.get("utilization", 0) or 0)
                for name, w in windows.items()
            }
            # Sentinel reports resets_at as ISO timestamps; convert to
            # seconds-until-reset for display.
            self.resets = {}
            for name, w in windows.items():
                meta = w.get("metadata") or {}
                if "reset_in_sec" in meta:
                    try:
                        self.resets[name] = float(meta["reset_in_sec"])
                    except (TypeError, ValueError):
                        pass
            self.has_data = True
            return True
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch from Sentinel: {e}")
            self.error_message = "Sentinel\nerror"
            return False

    # ── Direct-mode scrape ─────────────────────────────────────────────────

    def _resolve_auth_cookie(self) -> str:
        """Browser-extraction wins over the pasted value when ``browser`` is set."""
        if not self.browser:
            return self.auth_cookie
        try:
            cookies = list_cookies(self.cookie_domain, browser=self.browser)
        except CookieError as e:
            self.log(LogLevel.WARNING, f"browser cookie lookup failed: {e}")
            return self.auth_cookie
        value = cookies.get("auth")
        if not value:
            self.log(
                LogLevel.WARNING,
                f"no 'auth' cookie for {self.cookie_domain} in {self.browser}",
            )
            return self.auth_cookie
        return value

    def _fetch_direct(self) -> bool:
        if not self.workspace_id:
            self.error_message = "No\nworkspace"
            return False
        cookie = self._resolve_auth_cookie()
        if not cookie:
            self.error_message = "No\ncookie"
            return False

        url = DASHBOARD_URL.format(workspace_id=self.workspace_id)
        headers = {
            "User-Agent": _BROWSER_UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Cookie": f"auth={cookie}",
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code in (401, 403):
                self.log(LogLevel.ERROR, "auth cookie expired (HTTP 401/403)")
                self.error_message = "Cookie\nexpired"
                return False
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"dashboard fetch failed: {e}")
            self.error_message = "Fetch\nerror"
            return False

        html = response.text
        self.windows = {}
        self.resets = {}
        for name, patterns in _WINDOW_PATTERNS.items():
            parsed = _parse_window(html, patterns)
            if parsed is None:
                continue
            usage_pct, reset_in_sec = parsed
            self.windows[name] = max(0.0, min(100.0, usage_pct))
            self.resets[name] = max(0.0, reset_in_sec)

        if not self.windows:
            self.log(LogLevel.ERROR, "could not parse dashboard hydration payload")
            self.error_message = "Parse\nfailed"
            return False

        self.has_data = True
        return True

    def _fetch_usage(self) -> bool:
        # Sentinel-first when configured, with fall-through to direct
        # mode so a misconfigured / restarting / outdated sentinel never
        # blacks out the badge as long as direct mode still has creds.
        if self.quota_sentinel_url and self._fetch_from_sentinel():
            self.error_message = None
            return True
        return self._fetch_direct()

    # ── Drawing primitives ────────────────────────────────────────────────

    def _get_bar_color(self, pct: float) -> tuple[int, int, int]:
        if pct >= 90:
            return (183, 28, 28)
        elif pct >= 75:
            return (230, 81, 0)
        elif pct >= 50:
            return (245, 127, 23)
        elif pct >= 25:
            return (27, 94, 32)
        else:
            return (250, 199, 29)  # OpenCode ember / yellow

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

    def _draw_bar(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        w: int,
        h: int,
        pct: float,
    ) -> None:
        draw.rectangle([x, y, x + w, y + h], fill=(30, 30, 30), outline=(80, 80, 80))
        fill_w = int(w * min(pct, 100) / 100)
        if fill_w > 0:
            color = self._get_bar_color(pct)
            draw.rectangle([x + 1, y + 1, x + fill_w, y + h - 1], fill=color)

    def _ordered_windows(self) -> list[tuple[str, float]]:
        priority = ["rolling_5h", "weekly", "monthly"]
        ordered = [(n, self.windows[n]) for n in priority if n in self.windows]
        ordered += [(n, p) for n, p in self.windows.items() if n not in priority]
        return ordered

    def _short_label(self, name: str) -> str:
        return {
            "rolling_5h": "5H",
            "weekly": "WEEK",
            "monthly": "MONTH",
        }.get(name, name[:5].upper())

    # ── Display ───────────────────────────────────────────────────────────

    def _update_display(self) -> None:
        try:
            if self.error_message and not self.has_data:
                self.update_image_render(
                    text=f"OC Go\n{self.error_message}",
                    background_color="#B71C1C",
                    font_color="#FFFFFF",
                    font_size=10,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return

            if not self.has_data:
                self.update_image_render(
                    text="OC Go\n…",
                    background_color="#1F2937",
                    font_color="#FAC71D",
                    font_size=11,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return

            if not self.windows:
                self.update_image_render(
                    text="OC Go\nNo data",
                    background_color="#37474F",
                    font_color="#FFFFFF",
                    font_size=10,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return

            if self.display_mode == "rotate":
                self._render_rotate_view()
            else:
                self._render_compact_view()

        except Exception as e:  # noqa: BLE001
            self.log(LogLevel.ERROR, f"Display update failed: {e}")

    def _render_compact_view(self) -> None:
        img = Image.new("RGB", (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_title = self._load_font(10)
        font_label = self._load_font(8)

        max_pct = max(self.windows.values()) if self.windows else 0
        status_color = self._get_bar_color(max_pct)
        draw.ellipse([2, 2, 8, 8], fill=status_color)

        draw.text((12, 2), "OC Go", fill=(250, 199, 29), font=font_title)

        ordered = self._ordered_windows()
        y = 16
        for name, pct in ordered[:2]:
            label = self._short_label(name)
            draw.text((4, y), label, fill=(180, 180, 180), font=font_label)
            draw.text(
                (68, y),
                f"{pct:.0f}%",
                fill=(255, 255, 255),
                font=font_label,
                anchor="rt",
            )
            self._draw_bar(draw, 4, y + 11, 64, 6, pct)
            y += 22

        self.update_image_raw(img)

    def _render_rotate_view(self) -> None:
        ordered = self._ordered_windows()
        if not ordered:
            return

        idx = self.current_view % len(ordered)
        name, pct = ordered[idx]

        img = Image.new("RGB", (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_title = self._load_font(10)
        font_label = self._load_font(11)
        font_value = self._load_font(18)

        max_pct = max(self.windows.values()) if self.windows else 0
        status_color = self._get_bar_color(max_pct)
        draw.ellipse([2, 2, 8, 8], fill=status_color)

        draw.text((12, 2), "OC Go", fill=(250, 199, 29), font=font_title)
        draw.text(
            (36, 18),
            self._short_label(name),
            fill=(180, 180, 180),
            font=font_label,
            anchor="mt",
        )

        color = self._get_bar_color(pct)
        draw.text((36, 34), f"{pct:.0f}%", fill=color, font=font_value, anchor="mt")

        self._draw_bar(draw, 4, 60, 64, 8, pct)

        self.update_image_raw(img)

    # ── BasePlugin lifecycle ──────────────────────────────────────────────

    def on_start(self) -> None:
        self.log(LogLevel.INFO, "OpenCode Go usage plugin started")
        mode = "sentinel" if self.quota_sentinel_url else "direct"
        self.log(
            LogLevel.INFO,
            f"Mode: {mode}, poll: {self.poll_interval}s, display: {self.display_mode}",
        )
        self._update_display()

    def on_button_pressed(self) -> None:
        self.log(LogLevel.INFO, "Button pressed, forcing usage refresh")
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
        self.workspace_id = (config.get("workspace_id") or "").strip()
        self.auth_cookie = (config.get("auth_cookie") or "").strip()
        self.browser = (config.get("browser") or "").strip().lower()
        self.cookie_domain = (config.get("cookie_domain") or COOKIE_DOMAIN).strip()
        self.poll_interval = max(int(config.get("poll_interval", 300)), 60)
        self.display_mode = config.get("display_mode", "compact")
        self.rotate_interval = int(config.get("rotate_interval", 5))
        self.quota_sentinel_url = (
            (config.get("quota_sentinel_url") or "").split("/v1")[0].rstrip("/")
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

        if self.display_mode == "rotate" and self.has_data and self.windows:
            if current_time - self.last_rotate_time >= self.rotate_interval:
                self.current_view += 1
                self.last_rotate_time = current_time
                self._update_display()


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: opencode_go_usage_plugin.py <socket_path> <config_json>")
        sys.exit(1)

    socket_path = sys.argv[1]
    try:
        config = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid config JSON: {e}")
        sys.exit(1)

    plugin = OpencodeGoUsagePlugin(socket_path, config)
    plugin.run()


if __name__ == "__main__":
    main()
