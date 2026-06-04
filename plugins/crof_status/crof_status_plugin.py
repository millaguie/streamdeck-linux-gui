#!/usr/bin/env python3
"""CrofAI usage / status monitoring plugin for StreamDeck UI.

CrofAI (https://crof.ai) does not expose a public balance/usage API
authenticated with a Bearer/API key. The dashboard endpoints
(/user-api/credits, /user-api/usage, /u_v2/get_usable_requests) only
accept the Flask session cookie issued after browser login.

If the user pastes their session cookie into config, this plugin
fetches credits, usable requests, and top-model usage. Without it,
falls back to /v1/models for basic service status + model count.

To extract the cookie: browser DevTools → Application → Cookies →
crof.ai → copy value of `session` cookie.
"""

import json
import re
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

MODELS_URL = "https://crof.ai/v1/models"
CREDITS_URL = "https://crof.ai/user-api/credits"
USAGE_URL = "https://crof.ai/user-api/usage"
USABLE_REQUESTS_URL = "https://crof.ai/u_v2/get_usable_requests"
DASHBOARD_URL = "https://crof.ai/dashboard"
COOKIE_DOMAIN = "crof.ai"
DEFAULT_OPENCODE_AUTH = str(Path.home() / ".local" / "share" / "opencode" / "auth.json")

# crof.ai stopped returning the plan ceiling from /u_v2/get_usable_requests
# (it now sends a bare fractional number, e.g. ``2872.5``). The ceiling only
# survives in the dashboard's server-rendered string:
#
#     <pretty id="usable_requests">2872.5/6250</pretty>
#
# Scrape it once and cache it; it's static per plan. Numerator may be
# fractional, so allow a decimal there.
_PLAN_RE = re.compile(
    r'<pretty[^>]*id="usable_requests"[^>]*>\s*\d+(?:\.\d+)?\s*/\s*(\d+)\s*</pretty>',
    re.IGNORECASE,
)


class CrofStatusPlugin(BasePlugin):
    """Plugin for monitoring CrofAI service status and model catalog."""

    def __init__(self, socket_path: str, config: dict[str, Any]):
        super().__init__(socket_path, config)

        self.api_key = config.get('api_key', '')
        self.session_cookie = config.get('session_cookie', '').strip()
        self.browser = (config.get('browser') or '').strip().lower()
        self.cookie_domain = (config.get('cookie_domain') or COOKIE_DOMAIN).strip()
        self.opencode_auth_path = config.get('opencode_auth_path', '') or DEFAULT_OPENCODE_AUTH
        self.poll_interval = max(int(config.get('poll_interval', 600)), 60)
        self.display_mode = config.get('display_mode', 'compact')
        self.rotate_interval = int(config.get('rotate_interval', 5))
        # ``quota_sentinel_url`` — when set, credits/usable_requests come
        # from the sentinel's ``/v1/providers/crofai`` instead of being
        # scraped directly. /v1/models keeps using crof.ai directly since
        # that endpoint is public.
        self.quota_sentinel_url = (
            (config.get('quota_sentinel_url') or '').split('/v1')[0].rstrip('/')
        )
        self._sentinel_api_key = ''
        self._sentinel_instance_id = ''

        self.last_poll_time = 0
        self.error_message: str | None = None
        self.has_data = False
        self.current_view = 0
        self.last_rotate_time = 0

        # State
        self.status_ok = False
        self.model_count = 0
        self.cheapest_model: str | None = None
        self.cheapest_price: float | None = None  # $/M tokens (output), as reported by crof.ai
        self.fastest_model: str | None = None
        self.fastest_speed: int = 0
        self.auth_ok: bool | None = None  # None if no key configured

        # Session-cookie-only fields (None when no cookie configured)
        self.credits: float | None = None
        self.usable_requests: float | None = None  # fractional, e.g. 2872.5
        self.requests_plan: int | None = None
        self.top_model: str | None = None
        self.top_model_tokens: int = 0
        self.session_ok: bool | None = None  # True if cookie is valid
        # Plan ceiling scraped from the dashboard HTML (static per plan).
        self._scraped_plan: int | None = None

    def _resolve_key(self) -> str:
        if self.api_key:
            return self.api_key
        try:
            with open(self.opencode_auth_path) as f:
                auth = json.load(f)
            for key_name in ('crof-coding-plan', 'crof', 'crofai'):
                entry = auth.get(key_name, {})
                if entry.get('key'):
                    return entry['key']
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, KeyError) as e:
            self.log(LogLevel.ERROR, f"Failed to read opencode auth: {e}")
        return ""

    def _fetch_models(self) -> bool:
        """Fetch /v1/models — public endpoint. Always called."""
        try:
            headers = {"Accept": "application/json"}
            key = self._resolve_key()
            if key:
                headers["Authorization"] = f"Bearer {key}"

            response = requests.get(MODELS_URL, headers=headers, timeout=10)

            if response.status_code == 401:
                self.auth_ok = False
                self.error_message = "Auth\nfailed"
                return False
            if response.status_code == 429:
                self.error_message = "Rate\nlimited"
                return False

            response.raise_for_status()
            data = response.json()
            models = data.get('data', data) if isinstance(data, dict) else data
            if not isinstance(models, list):
                self.error_message = "Bad\nresponse"
                return False

            self.status_ok = True
            self.auth_ok = bool(key) if key else None
            self.model_count = len(models)

            cheapest = None
            cheapest_price = None
            fastest = None
            fastest_speed = 0
            for m in models:
                pricing = m.get('pricing', {})
                try:
                    price = float(pricing.get('completion', 0))
                except (TypeError, ValueError):
                    price = 0
                if price > 0 and (cheapest_price is None or price < cheapest_price):
                    cheapest_price = price
                    cheapest = m.get('id')
                speed = int(m.get('speed', 0) or 0)
                if speed > fastest_speed:
                    fastest_speed = speed
                    fastest = m.get('id')

            self.cheapest_model = cheapest
            # crof.ai's /v1/models reports pricing already in $/M tokens
            # (e.g. completion "0.85"), so use it as-is. (It used to be
            # per-token, which is why this multiplied by 1e6.)
            self.cheapest_price = cheapest_price if cheapest_price else None
            self.fastest_model = fastest
            self.fastest_speed = fastest_speed
            return True

        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch CrofAI models: {e}")
            self.status_ok = False
            self.error_message = "Down"
            return False

    def _resolve_session_cookie(self) -> str:
        """Return the raw Flask ``session`` cookie value.

        When ``browser`` is configured the value comes straight from the
        browser store and bypasses ``_normalize_cookie``'s paste-cleanup
        (the on-disk value is already canonical).
        """
        if not self.browser:
            return self.session_cookie
        try:
            cookies = list_cookies(self.cookie_domain, browser=self.browser)
        except CookieError as e:
            self.log(LogLevel.WARNING, f"browser cookie lookup failed: {e}")
            return self.session_cookie
        value = cookies.get("session")
        if not value:
            self.log(
                LogLevel.WARNING,
                f"no 'session' cookie for {self.cookie_domain} in {self.browser}",
            )
            return self.session_cookie
        return value

    def _normalize_cookie(self) -> str:
        """Strip common copy-paste artifacts from the session cookie value.

        Returns empty string and logs a warning if the value is obviously not
        a Flask session cookie (URL pasted by mistake, too short, etc.).
        """
        c = self._resolve_session_cookie().strip().strip('"').strip("'")
        # If user pasted "session=VALUE; Path=/; ..." strip everything but value
        if c.lower().startswith("session="):
            c = c.split("=", 1)[1].split(";", 1)[0].strip()
        # Sanity checks
        if c.lower().startswith(("http://", "https://")):
            self.log(LogLevel.ERROR, "session_cookie looks like a URL — paste the cookie VALUE, not the URL")
            return ""
        if len(c) < 40:
            self.log(LogLevel.WARNING, f"session_cookie suspiciously short ({len(c)} chars) — typical Flask cookies are 100+")
        return c

    # -------------------------------------------------------------- sentinel

    def _sentinel_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._sentinel_api_key:
            headers['X-API-Key'] = self._sentinel_api_key
        return headers

    def _register_with_sentinel(self) -> bool:
        if self._sentinel_api_key:
            return True
        cookie = self._normalize_cookie()
        if not cookie:
            self.log(
                LogLevel.WARNING,
                "no session_cookie set — sentinel registration needs it for the "
                "crof.ai dashboard (the /v1/models key is rejected by the quota endpoint)",
            )
            return False
        try:
            payload = {
                'project_name': 'streamdeck-crofai',
                'framework': 'opencode',
                'auth': {'opencode_auth': {'crofai': {'key': self._resolve_key() or ''}}},
                'provider_config': {'crofai': {'session_cookie': cookie}},
            }
            resp = requests.post(
                f"{self.quota_sentinel_url}/v1/instances", json=payload, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            self._sentinel_api_key = data.get('api_key', '')
            self._sentinel_instance_id = data.get('instance_id', '')
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
                self._sentinel_api_key = ''
                return False
            response.raise_for_status()
            providers = response.json()
            if isinstance(providers, dict):
                return provider in providers
            return provider in [
                p.get('name', p) if isinstance(p, dict) else p for p in providers
            ]
        except requests.exceptions.RequestException:
            return False

    def _fetch_from_sentinel(self) -> bool:
        """Populate ``credits``/``usable_requests``/``requests_plan`` from
        the sentinel's ``crofai`` provider.  Falls back to direct mode on
        any error so a misconfigured sentinel doesn't black out the badge.
        """
        try:
            if not self._register_with_sentinel():
                return False
            if not self._provider_exists_in_sentinel('crofai'):
                self.log(LogLevel.INFO, "Provider 'crofai' not registered in Sentinel")
                return False
            response = requests.get(
                f"{self.quota_sentinel_url}/v1/providers/crofai",
                headers=self._sentinel_headers(),
                timeout=10,
            )
            if response.status_code == 404:
                return False
            response.raise_for_status()
            data = response.json()
            if data.get('error'):
                self.log(LogLevel.WARNING, f"Sentinel crofai error: {data['error']}")
                return False
            window = (data.get('windows') or {}).get('requests') or {}
            meta = window.get('metadata') or {}
            usable = meta.get('usable_requests')
            plan = meta.get('requests_plan')
            credits = meta.get('credits')
            if usable is None and credits is None:
                return False
            if usable is not None:
                self.usable_requests = float(usable)
            if plan is not None:
                self.requests_plan = int(plan)
            if credits is not None:
                self.credits = float(credits)
            # /user-api/usage is not proxied by the sentinel — leave
            # ``top_model`` / ``top_model_tokens`` as whatever the last
            # direct fetch left behind (usually None).
            self.session_ok = True
            return True
        except requests.exceptions.RequestException as e:
            self.log(LogLevel.ERROR, f"Failed to fetch from Sentinel: {e}")
            return False

    def _fetch_session_endpoints(self) -> None:
        """Fetch the session-cookie-only endpoints (credits, usage, usable requests).

        Sets self.session_ok. Failures are non-fatal (we still show models data).
        """
        # Sentinel mode short-circuits the direct dashboard scrape.  We
        # still fall through to direct mode if the sentinel can't deliver
        # so the user keeps a working badge.
        if self.quota_sentinel_url and self._fetch_from_sentinel():
            return
        if not self.session_cookie and not self.browser:
            self.session_ok = None
            return

        cookie_val = self._normalize_cookie()
        if not cookie_val:
            self.session_ok = False
            return
        cookies = {"session": cookie_val}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://crof.ai/dashboard",
            "User-Agent": "Mozilla/5.0 streamdeck-crof-plugin",
        }
        self.log(LogLevel.INFO, f"Session cookie configured (len={len(cookie_val)}, starts={cookie_val[:8]!r})")
        any_ok = False

        endpoints = [
            ("credits", CREDITS_URL),
            ("usage", USAGE_URL),
            ("usable_requests", USABLE_REQUESTS_URL),
        ]
        responses: dict[str, tuple[int, str, str]] = {}
        for name, url in endpoints:
            try:
                r = requests.get(url, cookies=cookies, headers=headers, timeout=10, allow_redirects=False)
                ct = r.headers.get("content-type", "")
                snippet = r.text[:120].replace("\n", " ")
                responses[name] = (r.status_code, ct, snippet)
                self.log(LogLevel.INFO, f"{name}: HTTP {r.status_code} ct={ct} body={snippet!r}")
            except requests.exceptions.RequestException as e:
                self.log(LogLevel.WARNING, f"{name} fetch failed: {e}")
                responses[name] = (0, "", str(e))

        # Parse credits
        sc, ct, body = responses.get("credits", (0, "", ""))
        if sc == 200 and body.strip().lower() != "no":
            txt = body.strip().strip('"').lstrip('$').rstrip()
            try:
                self.credits = float(txt)
                any_ok = True
            except ValueError:
                self.log(LogLevel.WARNING, f"Unparseable credits: {body[:60]!r}")

        # Parse usage
        sc, ct, _ = responses.get("usage", (0, "", ""))
        if sc == 200:
            # Re-fetch full body for parsing (snippet was truncated)
            try:
                r = requests.get(USAGE_URL, cookies=cookies, headers=headers, timeout=10, allow_redirects=False)
                if r.text.strip().lower() != "no":
                    usage = r.json()
                    if isinstance(usage, dict) and usage:
                        ranked = sorted(
                            usage.items(),
                            key=lambda kv: int((kv[1] or {}).get('total_tokens', 0) or 0),
                            reverse=True,
                        )
                        top_id, top_data = ranked[0]
                        self.top_model = top_id
                        self.top_model_tokens = int((top_data or {}).get('total_tokens', 0) or 0)
                        any_ok = True
            except (ValueError, requests.exceptions.RequestException) as e:
                self.log(LogLevel.WARNING, f"Unparseable usage: {e}")

        # Parse usable_requests (need full body too)
        sc, ct, _ = responses.get("usable_requests", (0, "", ""))
        if sc == 200:
            try:
                r = requests.get(USABLE_REQUESTS_URL, cookies=cookies, headers=headers, timeout=10, allow_redirects=False)
                try:
                    j = r.json()
                except ValueError:
                    j = None
                if isinstance(j, dict):
                    if 'usable_requests' in j:
                        self.usable_requests = float(j.get('usable_requests') or 0)
                    if 'requests_plan' in j:
                        self.requests_plan = int(j.get('requests_plan') or 0)
                    if self.usable_requests is not None:
                        any_ok = True
                elif isinstance(j, (int, float)):
                    # crof.ai now returns a bare fractional number here.
                    self.usable_requests = float(j)
                    any_ok = True
            except requests.exceptions.RequestException as e:
                self.log(LogLevel.WARNING, f"usable_requests reparse failed: {e}")

        # The plan ceiling is no longer in the JSON response — scrape it from
        # the dashboard once and cache it (static per plan).
        if self.usable_requests is not None and not self.requests_plan:
            if self._scraped_plan is None:
                self._scraped_plan = self._fetch_plan_from_dashboard(cookies, headers)
            if self._scraped_plan:
                self.requests_plan = self._scraped_plan

        self.session_ok = any_ok
        if not any_ok:
            self.log(LogLevel.WARNING, "Session cookie did not authenticate any endpoint")

    def _fetch_plan_from_dashboard(self, cookies: dict[str, str], headers: dict[str, str]) -> int | None:
        """Scrape the daily request plan ceiling from the dashboard HTML.

        crof.ai only renders the ceiling as ``<pretty id="usable_requests">
        2872.5/6250</pretty>`` — there's no JSON endpoint for it. Returns the
        integer ceiling, or None if the page couldn't be fetched/parsed.
        """
        try:
            r = requests.get(DASHBOARD_URL, cookies=cookies, headers=headers, timeout=10, allow_redirects=False)
            if r.status_code != 200:
                return None
            m = _PLAN_RE.search(r.text)
            if not m:
                self.log(LogLevel.WARNING, "plan ceiling not found in dashboard HTML")
                return None
            return int(m.group(1))
        except (requests.exceptions.RequestException, ValueError) as e:
            self.log(LogLevel.WARNING, f"plan scrape failed: {e}")
            return None

    def _fetch_status(self) -> bool:
        ok = self._fetch_models()
        if ok:
            self._fetch_session_endpoints()
            self.has_data = True
        return ok

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

    def _update_display(self) -> None:
        try:
            if self.error_message and not self.has_data:
                self.update_image_render(
                    text=f"Crof\n{self.error_message}",
                    background_color="#B71C1C",
                    font_color="#FFFFFF",
                    font_size=11,
                    text_vertical_align="middle",
                    text_horizontal_align="center",
                )
                return

            if not self.has_data:
                self.update_image_render(
                    text="Crof\n...",
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
        img = Image.new('RGB', (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_title = self._load_font(10)
        font_value = self._load_font(14)
        font_small = self._load_font(8)
        font_med = self._load_font(11)

        status_color = (76, 175, 80) if self.status_ok else (183, 28, 28)
        draw.ellipse([2, 2, 8, 8], fill=status_color)
        draw.text((12, 2), "CrofAI", fill=(255, 255, 255), font=font_title)

        if self.session_ok and self.credits is not None:
            # Session-aware view: credits + usable requests
            draw.text((36, 16), f"${self.credits:.2f}", fill=(76, 175, 80), font=font_value, anchor="mt")
            draw.text((36, 33), "credits", fill=(180, 180, 180), font=font_small, anchor="mt")

            if self.usable_requests is not None:
                if self.requests_plan:
                    pct = self.usable_requests / self.requests_plan * 100
                    bar_color = (76, 175, 80) if pct > 50 else (245, 127, 23) if pct > 20 else (183, 28, 28)
                    draw.text((36, 44), f"{self.usable_requests}/{self.requests_plan}", fill=(255, 255, 255), font=font_small, anchor="mt")
                    draw.rectangle([4, 56, 68, 64], fill=(30, 30, 30), outline=(80, 80, 80))
                    fill_w = int(64 * min(pct, 100) / 100)
                    if fill_w > 0:
                        draw.rectangle([5, 57, 4 + fill_w, 63], fill=bar_color)
                else:
                    draw.text((36, 48), f"{self.usable_requests} req", fill=(33, 150, 243), font=font_med, anchor="mt")
        else:
            # Public-only view: model count + cheapest pricing
            draw.text((36, 18), str(self.model_count), fill=(255, 255, 255), font=font_value, anchor="mt")
            draw.text((36, 35), "models", fill=(180, 180, 180), font=font_small, anchor="mt")
            if self.cheapest_price is not None:
                draw.text((36, 47), "from", fill=(150, 150, 150), font=font_small, anchor="mt")
                draw.text((36, 56), f"${self.cheapest_price:.2f}/M", fill=(76, 175, 80), font=font_small, anchor="mt")

        # Auth indicators (bottom-right): k=API key OK, s=session OK
        marks = []
        if self.session_ok is True:
            marks.append(("s", (76, 175, 80)))
        elif self.session_ok is False:
            marks.append(("s", (183, 28, 28)))
        if self.auth_ok is True:
            marks.append(("k", (76, 175, 80)))
        x = 68
        for label, color in marks:
            draw.text((x, 64), label, fill=color, font=font_small, anchor="rb")
            x -= 8

        self.update_image_raw(img)

    def _render_rotate_view(self) -> None:
        views: list[tuple[str, str, tuple[int, int, int]]] = [
            ("Status", "OK" if self.status_ok else "DOWN",
             (76, 175, 80) if self.status_ok else (183, 28, 28)),
        ]
        if self.session_ok and self.credits is not None:
            views.append(("Credits", f"${self.credits:.2f}", (76, 175, 80)))
        if self.session_ok and self.usable_requests is not None:
            label = f"{self.usable_requests}"
            if self.requests_plan:
                label = f"{self.usable_requests}/{self.requests_plan}"
            views.append(("Usable", label, (33, 150, 243)))
        if self.session_ok and self.top_model:
            views.append(("Top model", self.top_model[:11], (255, 255, 255)))
        views.append(("Models", str(self.model_count), (255, 255, 255)))
        if self.cheapest_price is not None:
            views.append(("Cheapest", f"${self.cheapest_price:.2f}/M", (76, 175, 80)))
        if self.fastest_model:
            views.append(("Fastest", f"{self.fastest_speed} t/s", (33, 150, 243)))

        idx = self.current_view % len(views)
        label, value, color = views[idx]

        img = Image.new('RGB', (72, 72), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        font_title = self._load_font(10)
        font_label = self._load_font(11)
        font_value = self._load_font(15)

        status_color = (76, 175, 80) if self.status_ok else (183, 28, 28)
        draw.ellipse([2, 2, 8, 8], fill=status_color)

        draw.text((12, 2), "CrofAI", fill=(255, 255, 255), font=font_title)
        draw.text((36, 22), label, fill=(180, 180, 180), font=font_label, anchor="mt")
        draw.text((36, 40), value, fill=color, font=font_value, anchor="mt")

        self.update_image_raw(img)

    def on_start(self) -> None:
        self.log(LogLevel.INFO, "CrofAI Status plugin started")
        self.log(LogLevel.INFO, f"Poll interval: {self.poll_interval}s, Display: {self.display_mode}")
        self._update_display()

    def on_button_pressed(self) -> None:
        self.log(LogLevel.INFO, "Button pressed, forcing refresh")
        if self._fetch_status():
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
        self.session_cookie = config.get('session_cookie', '').strip()
        self.browser = (config.get('browser') or '').strip().lower()
        self.cookie_domain = (config.get('cookie_domain') or COOKIE_DOMAIN).strip()
        self.quota_sentinel_url = (
            (config.get('quota_sentinel_url') or '').split('/v1')[0].rstrip('/')
        )
        self._sentinel_api_key = ''
        self._sentinel_instance_id = ''
        self.opencode_auth_path = config.get('opencode_auth_path', '') or DEFAULT_OPENCODE_AUTH
        self.poll_interval = max(int(config.get('poll_interval', 600)), 60)
        self.display_mode = config.get('display_mode', 'compact')
        self.rotate_interval = int(config.get('rotate_interval', 5))
        self.log(LogLevel.INFO, "Configuration updated")
        if self._fetch_status():
            self.error_message = None
        self.last_poll_time = time.time()
        self._update_display()

    def update(self) -> None:
        current_time = time.time()
        if current_time - self.last_poll_time >= self.poll_interval:
            if self._fetch_status():
                self.error_message = None
            self.last_poll_time = current_time
            self._update_display()

        if self.display_mode == 'rotate' and self.has_data:
            if current_time - self.last_rotate_time >= self.rotate_interval:
                self.current_view += 1
                self.last_rotate_time = current_time
                self._update_display()


def main():
    if len(sys.argv) < 3:
        print("Usage: crof_status_plugin.py <socket_path> <config_json>")
        sys.exit(1)

    socket_path = sys.argv[1]
    try:
        config = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid config JSON: {e}")
        sys.exit(1)

    plugin = CrofStatusPlugin(socket_path, config)
    plugin.run()


if __name__ == '__main__':
    main()
