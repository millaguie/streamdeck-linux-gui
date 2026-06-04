#!/usr/bin/env python3
"""Synthetic.new subscription quota monitor for StreamDeck UI.

Synthetic exposes three concurrent budgets that all bind real usage:

- ``subscription``    daily request count   (renews ~24h)
- ``rolling_5h``      rolling 5-hour requests (regenerates 5%/12 min)
- ``weekly_credits``  weekly $ credits remaining

The plugin tracks all three.  The headline display always shows the
*tightest* (highest-utilisation) window so the badge reflects the
actual binding constraint — heavy bursts hit the 5h window long before
the daily counter does.
"""

from __future__ import annotations

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

QUOTAS_URL = "https://api.synthetic.new/v2/quotas"
DEFAULT_OPENCODE_AUTH = str(Path.home() / ".local" / "share" / "opencode" / "auth.json")

# Order matters for "worst window" tie-breaks: when two windows are at
# the same utilisation the rolling 5h is the more actionable one (the
# user can wait 12 min for a regen tick), so it comes first.
_WINDOW_ORDER = ("rolling_5h", "subscription", "weekly_credits")
_WINDOW_LABELS = {
    "rolling_5h": "R5h",
    "subscription": "Day",
    "weekly_credits": "Wk$",
}


class SyntheticUsagePlugin(BasePlugin):
    """Plugin for monitoring Synthetic.new request + credit quotas."""

    def __init__(self, socket_path: str, config: dict[str, Any]):
        super().__init__(socket_path, config)

        self.api_key = config.get("api_key", "")
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
        # ``_compute_windows_from_quota``; ``headline`` is the worst one.
        self.last_poll_time = 0.0
        self.windows: dict[str, dict[str, Any]] = {}
        self.headline: dict[str, Any] | None = None
        self.error_message: str | None = None
        self.current_view = 0
        self.last_rotate_time = 0.0

    # ------------------------------------------------------------------ key

    def _resolve_key(self) -> str:
        """Get API key from config or opencode auth.json."""
        if self.api_key:
            return self.api_key
        try:
            with open(self.opencode_auth_path) as f:
                auth = json.load(f)
            entry = auth.get("synthetic") or {}
            if entry.get("key"):
                self.log(LogLevel.INFO, "Using API key from opencode auth (synthetic)")
                return str(entry["key"])
        except FileNotFoundError:
            self.log(
                LogLevel.WARNING,
                f"opencode auth not found: {self.opencode_auth_path}",
            )
        except (json.JSONDecodeError, KeyError) as e:
            self.log(LogLevel.ERROR, f"Failed to read opencode auth: {e}")
        return ""

    # ----------------------------------------------------------- window math

    @staticmethod
    def _compute_windows_from_quota(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Build ``{window_id: {utilization, used, limit, remaining, renews_at, unit}}``
        from the raw ``/v2/quotas`` payload.
        """
        out: dict[str, dict[str, Any]] = {}

        # Skip ``subscription`` when the user hasn't touched it — on
        # pay-per-use plans this window is always 0/500 and only adds
        # noise next to the real binding constraints (5h + weekly $).
        # Reappears as soon as the first subscription request lands.
        sub = data.get("subscription") or {}
        sub_limit = float(sub.get("limit") or 0)
        sub_requests = float(sub.get("requests") or 0)
        if sub_limit > 0 and sub_requests > 0:
            out["subscription"] = {
                "utilization": min(100.0, (sub_requests / sub_limit) * 100.0),
                "used": int(sub_requests),
                "limit": int(sub_limit),
                "remaining": max(0, int(sub_limit - sub_requests)),
                "renews_at": sub.get("renewsAt") or sub.get("renews_at"),
                "unit": "req",
            }

        rh = data.get("rollingFiveHourLimit") or {}
        rh_remaining = float(rh.get("remaining") or 0)
        rh_max = float(rh.get("max") or 0)
        if rh_max > 0:
            used = max(0.0, rh_max - rh_remaining)
            out["rolling_5h"] = {
                "utilization": min(100.0, (used / rh_max) * 100.0),
                "used": int(used),
                "limit": int(rh_max),
                "remaining": max(0, int(rh_remaining)),
                "renews_at": rh.get("nextTickAt"),
                "unit": "req",
            }

        wk = data.get("weeklyTokenLimit") or {}
        wk_pct = wk.get("percentRemaining")
        if isinstance(wk_pct, (int, float)):
            wk_pct_f = max(0.0, min(100.0, float(wk_pct)))
            out["weekly_credits"] = {
                "utilization": 100.0 - wk_pct_f,
                "used": str(wk.get("maxCredits") or "").lstrip("$"),
                "limit": str(wk.get("maxCredits") or "").lstrip("$"),
                "remaining": str(wk.get("remainingCredits") or "").lstrip("$"),
                "renews_at": wk.get("nextRegenAt"),
                "unit": "$",
            }
        return out

    @staticmethod
    def _windows_from_sentinel(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Reverse ``_compute_windows_from_quota`` — but read sentinel's
        ``provider_detail`` shape instead of the raw API.

        Keeps backwards compat: if sentinel only exposes the legacy
        ``subscription`` window (pre-fix), we still surface it.
        """
        out: dict[str, dict[str, Any]] = {}
        windows = payload.get("windows", {}) or {}
        for wid, w in windows.items():
            if not isinstance(w, dict):
                continue
            meta = w.get("metadata", {}) or {}
            unit = "$" if "max_credits" in meta else "req"
            entry: dict[str, Any] = {
                "utilization": float(w.get("utilization") or 0.0),
                "renews_at": w.get("resets_at"),
                "unit": unit,
            }
            if unit == "$":
                entry["used"] = meta.get("max_credits") or ""
                entry["limit"] = meta.get("max_credits") or ""
                entry["remaining"] = meta.get("remaining_credits") or ""
            else:
                entry["used"] = int(meta.get("used_requests") or 0)
                entry["limit"] = int(meta.get("limit_requests") or 0)
                entry["remaining"] = int(meta.get("remaining_requests") or 0)
            out[wid] = entry
        return out

    @staticmethod
    def _pick_headline(
        windows: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any]] | None:
        """Choose the window with the highest utilisation as the headline."""
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
        key = self._resolve_key()
        if not key:
            return False
        try:
            payload = {
                "project_name": "streamdeck-synthetic",
                "framework": "opencode",
                "auth": {"opencode_auth": {"synthetic": {"key": key}}},
            }
            resp = requests.post(
                f"{self.quota_sentinel_url}/v1/instances", json=payload, timeout=10
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
            if not self._provider_exists_in_sentinel("synthetic"):
                self.log(
                    LogLevel.INFO, "Provider 'synthetic' not registered in Sentinel"
                )
                return False
            response = requests.get(
                f"{self.quota_sentinel_url}/v1/providers/synthetic",
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
        key = self._resolve_key()
        if not key:
            self.error_message = "No\nAPI key"
            return False
        try:
            response = requests.get(
                QUOTAS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=10
            )
            if response.status_code == 401:
                self.error_message = "Auth\nfailed"
                return False
            if response.status_code == 429:
                self.error_message = "Rate\nlimited"
                return False
            response.raise_for_status()
            data = response.json()
            windows = self._compute_windows_from_quota(data)
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
            self.log(LogLevel.ERROR, f"Failed to fetch Synthetic quota: {e}")
            self.error_message = "API\nerror"
            return False

    def _fetch_usage(self) -> bool:
        # Sentinel-first when configured, with a fall-through to direct
        # mode so a misconfigured / restarting / outdated sentinel never
        # blacks out the badge as long as the API key still works.
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
    def _format_renewal(iso: str | None) -> str:
        if not iso:
            return "—"
        try:
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError:
            return "—"
        now = datetime.now(timezone.utc)
        delta = dt - now
        secs = int(delta.total_seconds())
        if secs <= 0:
            return "now"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"

    @staticmethod
    def _format_used(window: dict[str, Any]) -> str:
        unit = window.get("unit", "req")
        if unit == "$":
            rem = window.get("remaining") or "0"
            lim = window.get("limit") or "0"
            return f"${rem}/${lim}"
        return f"{window.get('used', 0)}/{window.get('limit', 0)}"

    def _update_display(self) -> None:
        try:
            if self.error_message and not self.headline:
                self.update_image_render(
                    text=f"Synthetic\n{self.error_message}",
                    background_color="#B71C1C",
                    font_color="#FFFFFF",
                    font_size=10,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return
            if not self.headline:
                self.update_image_render(
                    text="Synthetic\n…",
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
        """Show up to 2 windows side-by-side, OpenAI-plugin style."""
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
        draw.text((12, 2), "Synth", fill=(255, 255, 255), font=font_title)

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
            draw.rectangle([4, bar_y, 68, bar_y + 6], fill=(30, 30, 30), outline=(80, 80, 80))
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
        renew_label = self._format_renewal(w.get("renews_at"))
        wlabel = _WINDOW_LABELS.get(wid, wid[:3])

        img = Image.new("RGB", (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_title = self._load_font(10)
        font_value = self._load_font(13)
        font_small = self._load_font(9)

        dot = (76, 175, 80) if pct < 80 else (245, 127, 23) if pct < 95 else (183, 28, 28)
        draw.ellipse([2, 2, 8, 8], fill=dot)
        draw.text((12, 2), f"Synth·{wlabel}", fill=(255, 255, 255), font=font_title)

        draw.text(
            (36, 18), f"{pct:.0f}%", fill=self._bar_color(pct), font=font_value, anchor="mt"
        )
        draw.text((4, 38), self._format_used(w), fill=(180, 180, 180), font=font_small)
        draw.text(
            (4, 50), f"renews {renew_label}", fill=(180, 180, 180), font=font_small
        )

        # Utilization bar
        draw.rectangle([4, 63, 68, 69], fill=(30, 30, 30), outline=(80, 80, 80))
        fill_w = int(64 * min(pct, 100) / 100)
        if fill_w > 0:
            draw.rectangle([5, 64, 4 + fill_w, 68], fill=self._bar_color(pct))

        self.update_image_raw(img)

    # ----------------------------------------------------------- lifecycle

    def on_start(self) -> None:
        self.log(LogLevel.INFO, "Synthetic Usage plugin started")
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
        self.api_key = config.get("api_key", "")
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
        print("Usage: synthetic_usage_plugin.py <socket_path> <config_json>")
        sys.exit(1)
    socket_path = sys.argv[1]
    try:
        config = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid config JSON: {e}")
        sys.exit(1)
    plugin = SyntheticUsagePlugin(socket_path, config)
    plugin.run()


if __name__ == "__main__":
    main()
