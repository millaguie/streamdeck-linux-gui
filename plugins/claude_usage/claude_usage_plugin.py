#!/usr/bin/env python3
"""Claude Code usage monitoring plugin for StreamDeck UI."""

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

DEFAULT_CREDENTIALS_PATH = str(Path.home() / ".claude" / ".credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


class ClaudeUsagePlugin(BasePlugin):
    """Plugin for monitoring Claude Code plan consumption."""

    def __init__(self, socket_path: str, config: dict[str, Any]):
        super().__init__(socket_path, config)

        self.poll_interval = max(int(config.get('poll_interval', 300)), 60)
        self.credentials_path = config.get('credentials_path', '') or DEFAULT_CREDENTIALS_PATH
        self.display_mode = config.get('display_mode', 'compact')
        self.rotate_interval = int(config.get('rotate_interval', 5))

        self.quota_sentinel_url = config.get('quota_sentinel_url', '').split('/v1')[0].rstrip('/')
        self.account = config.get('account', '').strip()
        self._sentinel_api_key = ''
        self._sentinel_instance_id = ''

        # State
        self.last_poll_time = 0
        self.usage_data: dict[str, Any] | None = None
        self.error_message: str | None = None
        self.current_view = 0  # For rotate mode: 0=5h, 1=7d, 2=sonnet/opus
        self.last_rotate_time = 0
        self.plan_type = ""

    def _load_credentials(self) -> dict[str, Any] | None:
        """Load OAuth credentials from file."""
        try:
            with open(self.credentials_path) as f:
                creds = json.load(f)
            oauth = creds.get('claudeAiOauth', {})
            if not oauth.get('accessToken'):
                self.log(LogLevel.ERROR, "No access token found in credentials")
                return None
            self.plan_type = oauth.get('subscriptionType', 'unknown')
            return oauth
        except FileNotFoundError:
            self.log(LogLevel.ERROR, f"Credentials file not found: {self.credentials_path}")
            return None
        except json.JSONDecodeError as e:
            self.log(LogLevel.ERROR, f"Invalid credentials JSON: {e}")
            return None

    def _is_token_expired(self, oauth: dict[str, Any]) -> bool:
        """Check if the access token is expired."""
        expires_at = oauth.get('expiresAt', 0)
        # expiresAt is in milliseconds
        return time.time() * 1000 >= expires_at

    def _refresh_token(self, oauth: dict[str, Any]) -> dict[str, Any] | None:
        """Refresh the OAuth token and save new credentials."""
        refresh_token = oauth.get('refreshToken')
        if not refresh_token:
            self.log(LogLevel.ERROR, "No refresh token available")
            return None

        try:
            response = requests.post(
                TOKEN_REFRESH_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": OAUTH_CLIENT_ID,
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            # Update credentials file with new tokens
            with open(self.credentials_path) as f:
                creds = json.load(f)

            creds['claudeAiOauth']['accessToken'] = data['access_token']
            creds['claudeAiOauth']['refreshToken'] = data['refresh_token']
            creds['claudeAiOauth']['expiresAt'] = int(time.time() * 1000) + data.get('expires_in', 3600) * 1000

            with open(self.credentials_path, 'w') as f:
                json.dump(creds, f, indent=2)

            self.log(LogLevel.INFO, "OAuth token refreshed successfully")
            return creds['claudeAiOauth']

        except Exception as e:
            self.log(LogLevel.ERROR, f"Token refresh failed: {e}")
            return None

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
        oauth = self._load_credentials()
        if not oauth:
            return False
        try:
            payload: dict[str, Any] = {
                'project_name': 'streamdeck-claude',
                'framework': 'claude',
                'auth': {
                    'claude_credentials': {
                        'accessToken': oauth.get('accessToken', ''),
                        'refreshToken': oauth.get('refreshToken', ''),
                        'expiresAt': oauth.get('expiresAt', 0),
                    }
                },
            }
            # Optional friendly account label so multiple Claude accounts stay
            # distinct in the sentinel (otherwise they collide on provider name).
            if self.account:
                payload['account'] = self.account
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

    def _fetch_from_sentinel(self) -> dict[str, Any] | None:
        """Fetch usage data from Quota Sentinel (http://localhost:7878)."""
        try:
            if not self._register_with_sentinel():
                return None

            if not self._provider_exists_in_sentinel('claude'):
                self.log(LogLevel.INFO, "Provider 'claude' not registered in Sentinel")
                return None

            url = f"{self.quota_sentinel_url}/v1/providers/claude"
            if self.account:
                url += f"?account={self.account}"
            response = requests.get(url, headers=self._sentinel_headers(), timeout=10)
            if response.status_code == 404:
                self.log(LogLevel.INFO, "Provider 'claude' not yet registered in Sentinel")
                return None
            response.raise_for_status()
            data = response.json()

            if data.get('error'):
                self.log(LogLevel.ERROR, f"Sentinel error: {data['error']}")
                self.error_message = "Sentinel\nerror"
                return None

            # Convert sentinel format to the same format as the direct API
            windows = data.get('windows', {})
            result: dict[str, Any] = {}
            sentinel_to_api = {
                'five_hour': 'five_hour',
                'seven_day': 'seven_day',
                'seven_day_sonnet': 'seven_day_sonnet',
                'seven_day_opus': 'seven_day_opus',
            }
            for sentinel_key, api_key in sentinel_to_api.items():
                if sentinel_key in windows:
                    w = windows[sentinel_key]
                    result[api_key] = {
                        'utilization': w.get('utilization'),
                        'resets_at': w.get('resets_at'),
                    }
            return result if result else None

        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch from Sentinel: {e}")
            self.error_message = "Sentinel\nerror"
            return None

    def _fetch_usage(self) -> dict[str, Any] | None:
        """Fetch usage data from Claude API or Quota Sentinel.

        Sentinel-first when configured but if it returns nothing (the
        deployed sentinel is restarting / outdated / not yet polled) we
        fall through to a direct OAuth fetch so the badge keeps showing
        the right percentage.
        """
        if self.quota_sentinel_url:
            sentinel_result = self._fetch_from_sentinel()
            if sentinel_result:
                self.error_message = None
                return sentinel_result

        oauth = self._load_credentials()
        if not oauth:
            self.error_message = "No creds"
            return None

        # Refresh token if expired
        if self._is_token_expired(oauth):
            self.log(LogLevel.INFO, "Access token expired, refreshing...")
            oauth = self._refresh_token(oauth)
            if not oauth:
                self.error_message = "Token\nexpired"
                return None

        try:
            response = requests.get(
                USAGE_URL,
                headers={
                    "Authorization": f"Bearer {oauth['accessToken']}",
                    "anthropic-beta": "oauth-2025-04-20",
                },
                timeout=10,
            )

            if response.status_code == 429:
                self.log(LogLevel.WARNING, "Rate limited on usage endpoint")
                self.error_message = "Rate\nlimited"
                return None

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch usage: {e}")
            self.error_message = "API\nerror"
            return None

    def _format_reset_time(self, iso_timestamp: str) -> str:
        """Format reset time as relative human-readable string."""
        try:
            reset_dt = datetime.fromisoformat(iso_timestamp)
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
            return (183, 28, 28)      # Red
        elif utilization >= 75:
            return (230, 81, 0)       # Orange
        elif utilization >= 50:
            return (245, 127, 23)     # Yellow
        elif utilization >= 25:
            return (27, 94, 32)       # Green
        else:
            return (21, 101, 192)     # Blue

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """Load a font, falling back to default."""
        font_paths = [
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
        ]
        for path in font_paths:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _draw_bar(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, pct: float) -> None:
        """Draw a progress bar with border and fill."""
        # Bar background (dark)
        draw.rectangle([x, y, x + w, y + h], fill=(30, 30, 30), outline=(80, 80, 80))
        # Bar fill
        fill_w = int(w * min(pct, 100) / 100)
        if fill_w > 0:
            color = self._get_bar_color(pct)
            draw.rectangle([x + 1, y + 1, x + fill_w, y + h - 1], fill=color)

    def _update_display(self) -> None:
        """Update the button display with usage info."""
        try:
            if self.error_message and not self.usage_data:
                self.update_image_render(
                    text=f"Claude\n{self.error_message}",
                    background_color="#B71C1C",
                    font_color="#FFFFFF",
                    font_size=11,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return

            if not self.usage_data:
                self.update_image_render(
                    text="Claude\n...",
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
        """Render compact view showing 5h and 7d with progress bars."""
        data = self.usage_data

        five_h = data.get('five_hour') or {}
        seven_d = data.get('seven_day') or {}

        five_h_pct = five_h.get('utilization', 0) or 0
        seven_d_pct = seven_d.get('utilization', 0) or 0

        five_h_reset = self._format_reset_time(five_h['resets_at']) if five_h.get('resets_at') else "?"
        seven_d_reset = self._format_reset_time(seven_d['resets_at']) if seven_d.get('resets_at') else "?"

        img = Image.new('RGB', (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_label = self._load_font(11)
        font_small = self._load_font(9)

        # 5h section: label + bar + reset
        draw.text((4, 2), f"5h {five_h_pct:.0f}%", fill=(255, 255, 255), font=font_label)
        self._draw_bar(draw, 4, 16, 64, 8, five_h_pct)
        draw.text((4, 26), five_h_reset, fill=(180, 180, 180), font=font_small)

        # 7d section: label + bar + reset
        draw.text((4, 38), f"7d {seven_d_pct:.0f}%", fill=(255, 255, 255), font=font_label)
        self._draw_bar(draw, 4, 52, 64, 8, seven_d_pct)
        draw.text((4, 62), seven_d_reset, fill=(180, 180, 180), font=font_small)

        self.update_image_raw(img)

    def _render_rotate_view(self) -> None:
        """Render rotating views cycling through usage windows."""
        data = self.usage_data
        views = []

        for key, label in [('five_hour', '5 Hour'), ('seven_day', '7 Day'),
                           ('seven_day_sonnet', 'Sonnet'), ('seven_day_opus', 'Opus')]:
            bucket = data.get(key) or {}
            if bucket.get('utilization') is not None:
                pct = bucket['utilization']
                reset = self._format_reset_time(bucket['resets_at']) if bucket.get('resets_at') else "?"
                views.append((label, pct, reset))

        if not views:
            self.update_image_render(
                text="Claude\nNo data",
                background_color="#37474F",
                font_color="#FFFFFF",
                font_size=12,
                text_vertical_align="middle",
                text_horizontal_align="center",
            )
            return

        idx = self.current_view % len(views)
        label, pct, reset = views[idx]

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
        """Called when plugin starts."""
        self.log(LogLevel.INFO, "Claude Usage plugin started")
        self.log(LogLevel.INFO, f"Poll interval: {self.poll_interval}s, Display: {self.display_mode}")
        self._update_display()

    def on_button_pressed(self) -> None:
        """Force refresh on button press."""
        self.log(LogLevel.INFO, "Button pressed, forcing usage refresh")
        self.usage_data = self._fetch_usage()
        if self.usage_data:
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
        self.poll_interval = max(int(config.get('poll_interval', 300)), 60)
        self.credentials_path = config.get('credentials_path', '') or DEFAULT_CREDENTIALS_PATH
        self.display_mode = config.get('display_mode', 'compact')
        self.rotate_interval = int(config.get('rotate_interval', 5))
        self.quota_sentinel_url = config.get('quota_sentinel_url', '').split('/v1')[0].rstrip('/')
        self.account = config.get('account', '').strip()
        self._sentinel_api_key = ''
        self._sentinel_instance_id = ''
        self.log(LogLevel.INFO, "Configuration updated")
        # Force refresh
        self.usage_data = self._fetch_usage()
        if self.usage_data:
            self.error_message = None
        self.last_poll_time = time.time()
        self._update_display()

    def update(self) -> None:
        """Called periodically in the main loop."""
        current_time = time.time()

        # Poll at configured interval
        if current_time - self.last_poll_time >= self.poll_interval:
            self.usage_data = self._fetch_usage()
            if self.usage_data:
                self.error_message = None
            self.last_poll_time = current_time
            self._update_display()

        # Handle view rotation
        if self.display_mode == 'rotate' and self.usage_data:
            if current_time - self.last_rotate_time >= self.rotate_interval:
                self.current_view += 1
                self.last_rotate_time = current_time
                self._update_display()


def main():
    """Main entry point for plugin."""
    if len(sys.argv) < 3:
        print("Usage: claude_usage_plugin.py <socket_path> <config_json>")
        sys.exit(1)

    socket_path = sys.argv[1]
    config_json = sys.argv[2]

    try:
        config = json.loads(config_json)
    except json.JSONDecodeError as e:
        print(f"Invalid config JSON: {e}")
        sys.exit(1)

    plugin = ClaudeUsagePlugin(socket_path, config)
    plugin.run()


if __name__ == '__main__':
    main()
