#!/usr/bin/env python3
"""Z.ai usage monitoring plugin for StreamDeck UI."""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

# Add parent directory to path to import base plugin
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from streamdeck_ui.plugin_system.base_plugin import BasePlugin
from streamdeck_ui.plugin_system.protocol import LogLevel

QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
DEFAULT_OPENCODE_AUTH = str(Path.home() / ".local" / "share" / "opencode" / "auth.json")


class ZaiUsagePlugin(BasePlugin):
    """Plugin for monitoring Z.ai plan consumption."""

    def __init__(self, socket_path: str, config: dict[str, Any]):
        super().__init__(socket_path, config)

        self.api_token = config.get('api_token', '')
        self.opencode_auth_path = config.get('opencode_auth_path', '') or DEFAULT_OPENCODE_AUTH
        self.poll_interval = max(int(config.get('poll_interval', 300)), 60)
        self.display_mode = config.get('display_mode', 'compact')
        self.rotate_interval = int(config.get('rotate_interval', 5))

        self.quota_sentinel_url = config.get('quota_sentinel_url', '').split('/v1')[0].rstrip('/')
        self._sentinel_api_key = ''
        self._sentinel_instance_id = ''

        # State
        self.last_poll_time = 0
        self.quota_data: list[dict[str, Any]] | None = None
        self.plan_level: str = ""
        self.error_message: str | None = None
        self.current_view = 0
        self.last_rotate_time = 0

    def _resolve_token(self) -> str:
        """Get API token from config or opencode auth.json."""
        if self.api_token:
            return self.api_token
        try:
            with open(self.opencode_auth_path) as f:
                auth = json.load(f)
            # Try zai-coding-plan first, then zai
            for key_name in ('zai-coding-plan', 'zai'):
                entry = auth.get(key_name, {})
                if entry.get('key'):
                    self.log(LogLevel.INFO, f"Using API key from opencode auth ({key_name})")
                    return entry['key']
        except FileNotFoundError:
            self.log(LogLevel.WARNING, f"opencode auth not found: {self.opencode_auth_path}")
        except (json.JSONDecodeError, KeyError) as e:
            self.log(LogLevel.ERROR, f"Failed to read opencode auth: {e}")
        return ""

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
        token = self._resolve_token()
        if not token:
            return False
        try:
            payload = {
                'project_name': 'streamdeck-zai',
                'framework': 'opencode',
                'auth': {'opencode_auth': {'zai-coding-plan': {'key': token}}},
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

            if not self._provider_exists_in_sentinel('zai'):
                self.log(LogLevel.INFO, "Provider 'zai' not registered in Sentinel")
                return False

            response = requests.get(f"{self.quota_sentinel_url}/v1/providers/zai", headers=self._sentinel_headers(), timeout=10)
            if response.status_code == 404:
                self.log(LogLevel.INFO, "Provider 'zai' not yet registered in Sentinel")
                return False
            response.raise_for_status()
            data = response.json()
            if data.get('error'):
                self.error_message = "Sentinel\nerror"
                return False
            windows = data.get('windows', {})
            self.quota_data = []
            for wname, wdata in windows.items():
                self.quota_data.append({
                    'type': 'TOKENS_LIMIT',
                    'percentage': wdata.get('utilization', 0),
                    'nextResetTime': None,
                    '_label': wname,
                })
            return True
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch from Sentinel: {e}")
            self.error_message = "Sentinel\nerror"
            return False

    def _fetch_usage(self) -> bool:
        """Fetch usage data from Z.ai API or Quota Sentinel."""
        if self.quota_sentinel_url:
            return self._fetch_from_sentinel()

        token = self._resolve_token()
        if not token:
            self.error_message = "No\ntoken"
            return False

        try:
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(QUOTA_URL, headers=headers, timeout=10)

            if response.status_code == 401:
                self.log(LogLevel.ERROR, "Z.ai auth failed (401)")
                self.error_message = "Auth\nfailed"
                return False

            if response.status_code == 429:
                self.log(LogLevel.WARNING, "Z.ai rate limited")
                self.error_message = "Rate\nlimited"
                return False

            response.raise_for_status()
            data = response.json()

            if not data.get('success'):
                self.log(LogLevel.ERROR, f"Z.ai API error: {data}")
                self.error_message = "API\nerror"
                return False

            self.plan_level = data.get('data', {}).get('level', '?')
            self.quota_data = data.get('data', {}).get('limits', [])
            return True

        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch Z.ai usage: {e}")
            self.error_message = "API\nerror"
            return False

    def _format_reset_time(self, epoch_ms: int | None) -> str:
        """Format reset time (epoch ms) as relative human-readable string."""
        if not epoch_ms:
            return "?"
        try:
            reset_dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = reset_dt - now

            total_seconds = int(delta.total_seconds())
            if total_seconds <= 0:
                return "now"

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
        except Exception:
            return "?"

    def _get_bar_color(self, utilization: float) -> tuple[int, int, int]:
        """Get bar fill color based on utilization percentage."""
        if utilization >= 90:
            return (183, 28, 28)
        elif utilization >= 75:
            return (230, 81, 0)
        elif utilization >= 50:
            return (245, 127, 23)
        elif utilization >= 25:
            return (27, 94, 32)
        else:
            return (21, 101, 192)

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """Load a font, falling back to default."""
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
        """Draw a progress bar with border and fill."""
        draw.rectangle([x, y, x + w, y + h], fill=(30, 30, 30), outline=(80, 80, 80))
        fill_w = int(w * min(pct, 100) / 100)
        if fill_w > 0:
            color = self._get_bar_color(pct)
            draw.rectangle([x + 1, y + 1, x + fill_w, y + h - 1], fill=color)

    def _get_token_limits(self) -> list[dict[str, Any]]:
        """Extract TOKENS_LIMIT entries grouped by window."""
        if not self.quota_data:
            return []
        return [l for l in self.quota_data if l.get('type') == 'TOKENS_LIMIT']

    def _unit_label(self, limit: dict[str, Any]) -> str:
        """Get a human-readable label for a quota unit.

        Direct mode payloads carry ``unit`` (integer enum) + ``number``.
        Sentinel mode synthesises entries from a flat window name and
        stores it in ``_label`` — fall back to that so we don't render
        ``uNone`` when the API enum is missing.
        """
        unit = limit.get('unit')
        number = limit.get('number', '')
        if unit == 3:
            return f"{number}h"
        if unit == 6:
            return f"{number}d"
        if unit == 5:
            return "MCP"
        label = limit.get('_label')
        if label:
            return str(label)
        return f"u{unit}" if unit is not None else "Quota"

    def _update_display(self) -> None:
        """Update the button display."""
        try:
            if self.error_message and not self.quota_data:
                self.update_image_render(
                    text=f"Z.ai\n{self.error_message}",
                    background_color="#B71C1C",
                    font_color="#FFFFFF",
                    font_size=11,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return

            if not self.quota_data:
                self.update_image_render(
                    text="Z.ai\n...",
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
        """Render compact view showing token limits with progress bars."""
        limits = self._get_token_limits()

        img = Image.new('RGB', (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_label = self._load_font(11)
        font_small = self._load_font(9)

        if not limits:
            draw.text((36, 36), "Z.ai\nNo data", fill=(255, 255, 255), font=font_label, anchor="mm")
            self.update_image_raw(img)
            return

        # Layout: evenly space the limits vertically
        n = min(len(limits), 2)  # Show at most 2 in compact
        section_h = 72 // n

        for i, limit in enumerate(limits[:n]):
            pct = limit.get('percentage', 0) or 0
            reset = self._format_reset_time(limit.get('nextResetTime'))
            label = self._unit_label(limit)
            y_base = i * section_h

            draw.text((4, y_base + 2), f"{label} {pct:.0f}%", fill=(255, 255, 255), font=font_label)
            self._draw_bar(draw, 4, y_base + 16, 64, 8, pct)
            draw.text((4, y_base + 26), reset, fill=(180, 180, 180), font=font_small)

        self.update_image_raw(img)

    def _render_rotate_view(self) -> None:
        """Render rotating views cycling through quota limits."""
        limits = self._get_token_limits()

        if not limits:
            self.update_image_render(
                text="Z.ai\nNo data",
                background_color="#37474F",
                font_color="#FFFFFF",
                font_size=12,
                text_vertical_align="middle",
                text_horizontal_align="center",
            )
            return

        idx = self.current_view % len(limits)
        limit = limits[idx]
        pct = limit.get('percentage', 0) or 0
        reset = self._format_reset_time(limit.get('nextResetTime'))
        label = self._unit_label(limit)

        img = Image.new('RGB', (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_label = self._load_font(14)
        font_small = self._load_font(11)

        draw.text((36, 8), label, fill=(255, 255, 255), font=font_label, anchor="mt")
        draw.text((36, 24), f"{pct:.0f}%", fill=(255, 255, 255), font=font_label, anchor="mt")
        self._draw_bar(draw, 4, 42, 64, 10, pct)
        draw.text((36, 56), reset, fill=(180, 180, 180), font=font_small, anchor="mt")

        self.update_image_raw(img)

    def on_start(self) -> None:
        self.log(LogLevel.INFO, "Z.ai Usage plugin started")
        self.log(LogLevel.INFO, f"Poll interval: {self.poll_interval}s, Display: {self.display_mode}")
        self._update_display()

    def on_button_pressed(self) -> None:
        """Force refresh on button press."""
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
        self.api_token = config.get('api_token', '')
        self.opencode_auth_path = config.get('opencode_auth_path', '') or DEFAULT_OPENCODE_AUTH
        self.poll_interval = max(int(config.get('poll_interval', 300)), 60)
        self.display_mode = config.get('display_mode', 'compact')
        self.rotate_interval = int(config.get('rotate_interval', 5))
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

        if self.display_mode == 'rotate' and self.quota_data:
            if current_time - self.last_rotate_time >= self.rotate_interval:
                self.current_view += 1
                self.last_rotate_time = current_time
                self._update_display()


def main():
    if len(sys.argv) < 3:
        print("Usage: zai_usage_plugin.py <socket_path> <config_json>")
        sys.exit(1)

    socket_path = sys.argv[1]
    try:
        config = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid config JSON: {e}")
        sys.exit(1)

    plugin = ZaiUsagePlugin(socket_path, config)
    plugin.run()


if __name__ == '__main__':
    main()
