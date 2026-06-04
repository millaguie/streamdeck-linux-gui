#!/usr/bin/env python3
"""MiniMax Coding Plan usage monitoring plugin for StreamDeck UI.

As of early 2026 the ``/coding_plan/remains`` endpoint no longer accepts
the ``sk-cp-*`` API key on its own — calling it with just the Bearer
header now returns ``status_code 1004 "cookie is missing, log in again"``.
The endpoint is gated behind the operator's logged-in browser session at
``platform.minimax.io``, so direct mode now requires a session cookie
(scraped from the browser or pasted manually).  In sentinel mode the
cookie is configured on the daemon side.
"""

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

REMAINS_URL = "https://platform.minimax.io/v1/api/openplatform/coding_plan/remains"
COOKIE_DOMAIN = "platform.minimax.io"
DEFAULT_OPENCODE_AUTH = str(Path.home() / ".local" / "share" / "opencode" / "auth.json")


class MiniMaxUsagePlugin(BasePlugin):
    """Plugin for monitoring MiniMax Coding Plan consumption."""

    def __init__(self, socket_path: str, config: dict[str, Any]):
        super().__init__(socket_path, config)

        self.api_key = config.get('api_key', '')
        self.group_id = config.get('group_id', '')
        # The /coding_plan/remains endpoint now needs the browser session
        # cookie from platform.minimax.io (the API key alone returns
        # "cookie is missing, log in again").  ``browser`` is "" (legacy
        # paste-the-cookie flow) or one of "auto"/"firefox"/"chrome"/
        # "brave"/"chromium" — when set, the cookie is scraped straight
        # from the browser store.
        self.session_cookie = config.get('session_cookie', '')
        self.browser = (config.get('browser') or '').strip().lower()
        self.cookie_domain = (config.get('cookie_domain') or COOKIE_DOMAIN).strip()
        self.opencode_auth_path = config.get('opencode_auth_path', '') or DEFAULT_OPENCODE_AUTH
        self.poll_interval = max(int(config.get('poll_interval', 300)), 60)
        self.display_mode = config.get('display_mode', 'compact')
        self.rotate_interval = int(config.get('rotate_interval', 5))
        self.show_models = [s.strip().lower() for s in config.get('show_models', '').split(',') if s.strip()]
        self.quota_sentinel_url = config.get('quota_sentinel_url', '').split('/v1')[0].rstrip('/')
        self._sentinel_api_key = ''
        self._sentinel_instance_id = ''

        # State
        self.last_poll_time = 0
        self.model_remains: list[dict[str, Any]] = []
        self.error_message: str | None = None
        self.current_view = 0
        self.last_rotate_time = 0

    def _resolve_key(self) -> str:
        """Get API key from config or opencode auth.json."""
        if self.api_key:
            return self.api_key
        try:
            with open(self.opencode_auth_path) as f:
                auth = json.load(f)
            for key_name in ('minimax-coding-plan', 'minimax'):
                entry = auth.get(key_name, {})
                if entry.get('key'):
                    self.log(LogLevel.INFO, f"Using API key from opencode auth ({key_name})")
                    return entry['key']
        except FileNotFoundError:
            self.log(LogLevel.WARNING, f"opencode auth not found: {self.opencode_auth_path}")
        except (json.JSONDecodeError, KeyError) as e:
            self.log(LogLevel.ERROR, f"Failed to read opencode auth: {e}")
        return ""

    def _resolve_session_cookie(self) -> str:
        """Resolve the platform.minimax.io session cookie header value.

        Priority:
        1. ``browser`` config option set → scrape the browser cookie store.
           Warn (not fail) on ``CookieError`` so we still fall back to a
           manually pasted cookie if one is configured.
        2. Manual ``session_cookie`` config value (legacy / fallback).
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

    def _sentinel_headers(self) -> dict[str, str]:
        """Build headers for Quota Sentinel requests."""
        headers: dict[str, str] = {}
        if self._sentinel_api_key:
            headers['X-API-Key'] = self._sentinel_api_key
        return headers

    def _register_with_sentinel(self) -> bool:
        """Auto-register with Quota Sentinel and obtain API key."""
        if self._sentinel_api_key:
            return True
        key = self._resolve_key()
        if not key:
            return False
        cookie = self._resolve_session_cookie()
        if not cookie:
            self.log(
                LogLevel.WARNING,
                "no session_cookie set — sentinel registration needs it for the "
                "MiniMax coding-plan console (the API key alone is rejected by "
                "the /coding_plan/remains endpoint)",
            )
            return False
        try:
            payload: dict[str, Any] = {
                'project_name': 'streamdeck-minimax',
                'framework': 'opencode',
                'auth': {'opencode_auth': {'minimax': {'key': key}}},
                'provider_config': {
                    'minimax': {
                        'group_id': self.group_id,
                        'session_cookie': cookie,
                    }
                },
            }
            resp = requests.post(f"{self.quota_sentinel_url}/v1/instances", json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            self._sentinel_api_key = data.get('api_key', '')
            self._sentinel_instance_id = data.get('instance_id', '')
            self.log(LogLevel.INFO, f"Registered with Sentinel as {self._sentinel_instance_id}")
            return bool(self._sentinel_api_key)
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Sentinel registration failed: {e}")
            return False

    def _provider_exists_in_sentinel(self, provider: str) -> bool:
        """Check if a provider is registered in Quota Sentinel."""
        try:
            response = requests.get(f"{self.quota_sentinel_url}/v1/providers", headers=self._sentinel_headers(), timeout=10)
            if response.status_code == 401:
                self.log(LogLevel.WARNING, "Sentinel auth expired, will re-register")
                self._sentinel_api_key = ''
                return False
            response.raise_for_status()
            providers = response.json()
            if isinstance(providers, dict):
                return provider in providers
            return provider in [p.get('name', p) if isinstance(p, dict) else p for p in providers]
        except requests.exceptions.RequestException:
            return False

    def _fetch_from_sentinel(self) -> bool:
        """Fetch usage data from Quota Sentinel."""
        try:
            if not self._register_with_sentinel():
                return False

            if not self._provider_exists_in_sentinel('minimax'):
                self.log(LogLevel.INFO, "Provider 'minimax' not registered in Sentinel")
                return False

            response = requests.get(f"{self.quota_sentinel_url}/v1/providers/minimax", headers=self._sentinel_headers(), timeout=10)
            if response.status_code == 404:
                self.log(LogLevel.INFO, "Provider 'minimax' not yet registered in Sentinel")
                return False
            response.raise_for_status()
            data = response.json()
            if data.get('error'):
                self.error_message = "Sentinel\nerror"
                return False
            windows = data.get('windows', {})
            if not windows:
                # Sentinel knows the provider but has no usage windows yet
                # (daemon hasn't polled, or the coding-plan format the daemon
                # expects changed).  Treat it as "no data" and fall through to
                # direct mode so the badge still shows live numbers instead of
                # sticking on the "..." placeholder.
                self.log(LogLevel.INFO, "Sentinel returned no windows for minimax; falling back to direct mode")
                return False
            self.model_remains = []
            for wname, wdata in windows.items():
                pct = wdata.get('utilization', 0) or 0
                reset_iso = wdata.get('resets_at')
                remains_ms = 0
                if reset_iso:
                    try:
                        from datetime import datetime, timezone
                        reset_dt = datetime.fromisoformat(reset_iso)
                        remains_ms = max(0, int((reset_dt - datetime.now(timezone.utc)).total_seconds() * 1000))
                    except Exception:
                        pass
                # Window names from sentinel: "MM-01_interval", "MM-01_weekly"
                # Convert back to model_name for display
                model_name = wname.replace('_interval', '').replace('_weekly', '')
                suffix = ' (wk)' if '_weekly' in wname else ''
                self.model_remains.append({
                    'model_name': f"{model_name}{suffix}",
                    'current_interval_total_count': 100,
                    'current_interval_usage_count': int(100 - pct),
                    'remains_time': remains_ms,
                })
            return True
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch from Sentinel: {e}")
            self.error_message = "Sentinel\nerror"
            return False

    def _fetch_usage(self) -> bool:
        """Fetch coding plan remains from MiniMax API or Quota Sentinel.

        Sentinel is preferred when configured but failures (401, provider
        not yet polled, sentinel restarting, …) fall through to direct
        mode so the badge keeps working instead of going blank.
        """
        if self.quota_sentinel_url and self._fetch_from_sentinel():
            self.error_message = None
            return True

        key = self._resolve_key()
        if not key:
            self.error_message = "No\nkey"
            return False

        if not self.group_id:
            self.error_message = "No\ngroup"
            return False

        cookie = self._build_cookie_header()
        if not self._resolve_session_cookie():
            self.error_message = "No\ncookie"
            return False

        try:
            response = requests.get(
                REMAINS_URL,
                params={"GroupId": self.group_id},
                headers={
                    "accept": "application/json, text/plain, */*",
                    "authorization": f"Bearer {key}",
                    "cookie": cookie,
                    "referer": "https://platform.minimax.io/user-center/payment/coding-plan",
                    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)",
                },
                timeout=10,
            )

            if response.status_code in (401, 403):
                self.error_message = "Cookie\nexpired"
                self.log(LogLevel.ERROR, f"MiniMax auth failed ({response.status_code})")
                return False
            if response.status_code == 429:
                self.error_message = "Rate\nlimit"
                self.log(LogLevel.WARNING, "MiniMax rate limited")
                return False

            response.raise_for_status()
            data = response.json()

            status = data.get('base_resp', {}).get('status_code', -1)
            if status != 0:
                msg = data.get('base_resp', {}).get('status_msg', 'unknown error')
                self.log(LogLevel.ERROR, f"MiniMax API error: {msg}")
                # 1004 == "cookie is missing, log in again" — the session
                # cookie went stale; tell the operator to refresh it.
                if status == 1004 or 'cookie' in msg.lower():
                    self.error_message = "Cookie\nexpired"
                else:
                    self.error_message = "API\nerror"
                return False

            self.model_remains = data.get('model_remains', [])
            self.log(LogLevel.INFO, f"MiniMax: got {len(self.model_remains)} model(s)")
            return True

        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch MiniMax usage: {e}")
            self.error_message = "API\nerror"
            return False

    def _format_reset_time(self, remains_ms: int) -> str:
        """Format remaining time in milliseconds as human-readable."""
        if remains_ms <= 0:
            return "now"

        total_seconds = remains_ms // 1000
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        if hours >= 24:
            days = hours // 24
            remaining_hours = hours % 24
            return f"{days}d{remaining_hours}h"
        elif hours > 0:
            return f"{hours}h{minutes:02d}m"
        else:
            return f"{minutes}m"

    def _format_count(self, n: int) -> str:
        """Format large numbers compactly: 1500 -> 1.5k, 150000 -> 150k."""
        if n >= 1_000_000:
            v = n / 1_000_000
            return f"{v:.1f}M" if v != int(v) else f"{int(v)}M"
        elif n >= 1000:
            v = n / 1000
            return f"{v:.1f}k" if v != int(v) else f"{int(v)}k"
        return str(n)

    def _short_model_name(self, model: str) -> str:
        """Shorten model name for display."""
        replacements = {
            "MiniMax-M2.7": "M2.7",
            "MiniMax-M2.5": "M2.5",
            "MiniMax-": "MM-",
            "minimax-": "mm-",
        }
        for old, new in replacements.items():
            model = model.replace(old, new)
        # Truncate if still too long
        if len(model) > 8:
            model = model[:8]
        return model

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

    def _draw_bar(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, pct: float) -> None:
        draw.rectangle([x, y, x + w, y + h], fill=(30, 30, 30), outline=(80, 80, 80))
        fill_w = int(w * min(pct, 100) / 100)
        if fill_w > 0:
            color = self._get_bar_color(pct)
            draw.rectangle([x + 1, y + 1, x + fill_w, y + h - 1], fill=color)

    def _filtered_models(self) -> list[dict[str, Any]]:
        """Return model_remains filtered by show_models config. If empty, return all.

        Sentinel mode rewrites ``MiniMax-M2.5`` to ``MM-M2.5`` to keep
        the window-name short, so a filter like ``MiniMax-M*`` would
        miss every sentinel-sourced row.  Expand each filter to both
        naming conventions before matching.
        """
        if not self.show_models:
            return self.model_remains
        expanded: list[str] = []
        for f in self.show_models:
            expanded.append(f)
            if f.startswith("minimax-"):
                expanded.append("mm-" + f[len("minimax-"):])
            elif f.startswith("mm-"):
                expanded.append("minimax-" + f[len("mm-"):])
        return [
            m for m in self.model_remains
            if any(f in m.get('model_name', '').lower() for f in expanded)
        ]

    def _build_display_rows(self) -> list[dict[str, Any]]:
        """Build display rows from filtered models, expanding interval + weekly.

        Note: current_interval_usage_count is actually the REMAINING count, not used.
        """
        models = self._filtered_models()
        rows = []
        for m in models:
            name = self._short_model_name(m.get('model_name', '?'))
            total = m.get('current_interval_total_count', 0)
            remaining = m.get('current_interval_usage_count', 0)
            used = total - remaining
            pct = (used / total * 100) if total > 0 else 0
            reset = self._format_reset_time(m.get('remains_time', 0))
            rows.append({'label': name, 'used': used, 'total': total, 'pct': pct, 'reset': reset})

            weekly_total = m.get('current_weekly_total_count', 0)
            if weekly_total > 0:
                weekly_remaining = m.get('current_weekly_usage_count', 0)
                weekly_used = weekly_total - weekly_remaining
                weekly_pct = (weekly_used / weekly_total * 100) if weekly_total > 0 else 0
                weekly_reset = self._format_reset_time(m.get('weekly_remains_time', 0))
                rows.append({'label': f"{name}/w", 'used': weekly_used, 'total': weekly_total, 'pct': weekly_pct, 'reset': weekly_reset})
        return rows

    def _update_display(self) -> None:
        try:
            if self.error_message and not self.model_remains:
                self.update_image_render(
                    text=f"MiniMax\n{self.error_message}",
                    background_color="#B71C1C",
                    font_color="#FFFFFF",
                    font_size=11,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return

            if not self.model_remains:
                self.update_image_render(
                    text="MiniMax\n...",
                    background_color="#37474F",
                    font_color="#FFFFFF",
                    font_size=12,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return

            if self.display_mode == 'rotate':
                self._render_rotate_view()
            else:
                self._render_compact_view()

        except Exception as e:
            self.log(LogLevel.ERROR, f"Display update failed: {e}")

    def _render_compact_view(self) -> None:
        """Render compact view showing filtered models with progress bars."""
        rows = self._build_display_rows()
        img = Image.new('RGB', (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)

        if not rows:
            font_label = self._load_font(11)
            draw.text((36, 36), "MiniMax\nNo match", fill=(255, 255, 255), font=font_label, anchor="mm")
            self.update_image_raw(img)
            return

        n = min(len(rows), 2)
        section_h = 72 // n
        font_label = self._load_font(11)
        font_small = self._load_font(9)

        for i, row in enumerate(rows[:n]):
            y_base = i * section_h
            draw.text((4, y_base + 2), f"{row['label']} {row['pct']:.0f}%", fill=(255, 255, 255), font=font_label)
            self._draw_bar(draw, 4, y_base + 16, 64, 8, row['pct'])
            draw.text((4, y_base + 26), f"{self._format_count(row['used'])}/{self._format_count(row['total'])} {row['reset']}", fill=(180, 180, 180), font=font_small)

        self.update_image_raw(img)

    def _render_rotate_view(self) -> None:
        """Render rotating views cycling through display rows."""
        rows = self._build_display_rows()
        if not rows:
            return

        idx = self.current_view % len(rows)
        row = rows[idx]

        img = Image.new('RGB', (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_label = self._load_font(14)
        font_mid = self._load_font(11)
        font_small = self._load_font(9)

        draw.text((36, 6), row['label'], fill=(255, 255, 255), font=font_label, anchor="mt")
        draw.text((36, 22), f"{self._format_count(row['used'])}/{self._format_count(row['total'])}", fill=(255, 255, 255), font=font_mid, anchor="mt")
        self._draw_bar(draw, 4, 38, 64, 10, row['pct'])
        draw.text((36, 41), f"{row['pct']:.0f}%", fill=(255, 255, 255), font=font_small, anchor="mm")
        draw.text((36, 56), row['reset'], fill=(180, 180, 180), font=font_small, anchor="mt")

        self.update_image_raw(img)

    def on_start(self) -> None:
        self.log(LogLevel.INFO, f"MiniMax Usage plugin started, Group: {self.group_id}")
        self.log(LogLevel.INFO, f"Poll interval: {self.poll_interval}s, Display: {self.display_mode}")
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
        self.api_key = config.get('api_key', '')
        self.group_id = config.get('group_id', '')
        self.session_cookie = config.get('session_cookie', '')
        self.browser = (config.get('browser') or '').strip().lower()
        self.cookie_domain = (config.get('cookie_domain') or COOKIE_DOMAIN).strip()
        self.opencode_auth_path = config.get('opencode_auth_path', '') or DEFAULT_OPENCODE_AUTH
        self.poll_interval = max(int(config.get('poll_interval', 300)), 60)
        self.display_mode = config.get('display_mode', 'compact')
        self.rotate_interval = int(config.get('rotate_interval', 5))
        self.show_models = [s.strip().lower() for s in config.get('show_models', '').split(',') if s.strip()]
        self.quota_sentinel_url = config.get('quota_sentinel_url', '').split('/v1')[0].rstrip('/')
        self._sentinel_api_key = ''
        self._sentinel_instance_id = ''
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

        if self.display_mode == 'rotate' and self._build_display_rows():
            if current_time - self.last_rotate_time >= self.rotate_interval:
                self.current_view += 1
                self.last_rotate_time = current_time
                self._update_display()


def main():
    if len(sys.argv) < 3:
        print("Usage: minimax_usage_plugin.py <socket_path> <config_json>")
        sys.exit(1)

    socket_path = sys.argv[1]
    try:
        config = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid config JSON: {e}")
        sys.exit(1)

    plugin = MiniMaxUsagePlugin(socket_path, config)
    plugin.run()


if __name__ == '__main__':
    main()
