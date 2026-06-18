#!/usr/bin/env python3
"""
Standalone VOE converter.

Converts VOE embed/page links into direct media links and can optionally save
the media through ffmpeg. This file intentionally does not import aniworld.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import html as html_lib
import json
import re
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

JUNK_PARTS = ("@$", "^^", "~@", "%?", "*~", "!!", "#&")

REDIRECT_PATTERN = re.compile(r"""['"](\s*https?://[^'"<>\s]+/e/[^'"<>\s]+)['"]""")
B64_PATTERN = re.compile(r"""var\s+a168c\s*=\s*['"]([^'"]+)['"]""")
HLS_PATTERN = re.compile(r"""['"]hls['"]\s*:\s*['"](?P<hls>[^'"]+)['"]""")
SOURCE_PATTERN = re.compile(r"""['"]source['"]\s*:\s*['"](?P<source>[^'"]+)['"]""")
MEDIA_PATTERN = re.compile(
    r"""https?://[^"'<>\s\\]+(?:\.m3u8|\.mp4|\.webm|\.mkv)(?:\?[^"'<>\s\\]*)?""",
    re.IGNORECASE,
)
M3U8_PATTERN = re.compile(r"""https?://[^"'<>\s\\]+\.m3u8[^"'<>\s\\]*""", re.IGNORECASE)
RAW_URL_PATTERN = re.compile(r"""https?(?::|%3A)(?://|\\/\\/|%2F%2F)[^"'<>),\s]+""", re.IGNORECASE)
ATTR_URL_PATTERN = re.compile(
    r"""(?:href|src|data-url|data-link|data-href)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
VOE_HINT_PATTERN = re.compile(r"""voe|/e/|embed""", re.IGNORECASE)
CLOUDFLARE_DOH_TEMPLATE = "https://chrome.cloudflare-dns.com/dns-query"
PROFILE_DIR = Path(__file__).resolve().parent / ".chromium_converter_profile"
DEFAULT_PAGE_MEDIA_MIN_DURATION = 60.0
AD_BLOCK_HOST_PARTS = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "adservice.google",
    "adnxs.com",
    "adform.net",
    "adsystem.com",
    "pubmatic.com",
    "rubiconproject.com",
    "openx.net",
    "criteo.com",
    "taboola.com",
    "outbrain.com",
    "mgid.com",
    "exoclick.com",
    "propellerads.com",
    "propeller-tracking.com",
    "popads.net",
    "popcash.net",
    "onclickads.net",
    "clickadu.com",
    "adsterra.com",
    "ad-maven.com",
    "juicyads.com",
    "trafficjunky.net",
    "hilltopads.net",
    "adcash.com",
    "yllix.com",
    "realsrv.com",
    "exdynsrv.com",
    "zeroredirect.com",
    "redirectnative.com",
    "pushads",
    "popunder",
)


class VoeConvertError(RuntimeError):
    """Raised when a VOE link cannot be converted."""


def shift_letters(value: str) -> str:
    result: list[str] = []
    for char in value:
        code = ord(char)
        if 65 <= code <= 90:
            code = (code - 65 + 13) % 26 + 65
        elif 97 <= code <= 122:
            code = (code - 97 + 13) % 26 + 97
        result.append(chr(code))
    return "".join(result)


def remove_junk(value: str) -> str:
    for part in JUNK_PARTS:
        value = value.replace(part, "_")
    return value.replace("_", "")


def shift_back(value: str, amount: int) -> str:
    return "".join(chr(ord(char) - amount) for char in value)


def decode_voe_payload(encoded: str) -> dict:
    try:
        step1 = shift_letters(encoded)
        step2 = remove_junk(step1)
        step3 = base64.b64decode(step2).decode("utf-8")
        step4 = shift_back(step3, 3)
        step5 = base64.b64decode(step4[::-1]).decode("utf-8")
        decoded = json.loads(step5)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoeConvertError(f"Could not decode VOE payload: {exc}") from exc

    if not isinstance(decoded, dict):
        raise VoeConvertError("Decoded VOE payload is not an object.")
    return decoded


def clean_media_url(value: str) -> str:
    value = value.strip().strip("'\"")
    value = value.replace("\\/", "/")
    value = value.replace("\\u0026", "&")
    value = value.replace("%3A", ":").replace("%2F", "/").replace("%2f", "/")
    return html_lib.unescape(value)


def decode_script_string(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, str):
                return loaded
        except json.JSONDecodeError:
            return raw[1:-1].encode("utf-8").decode("unicode_escape")
    return raw


def fetch_page(url: str, timeout: int) -> tuple[str, str]:
    try:
        request = Request(url, headers=DEFAULT_HEADERS)
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            encoding = response.headers.get_content_charset() or "utf-8"
            return raw.decode(encoding, errors="replace"), response.geturl()
    except ValueError as exc:
        raise VoeConvertError(f"Invalid URL: {url!r}") from exc
    except HTTPError as exc:
        raise VoeConvertError(f"HTTP {exc.code} while loading {url}") from exc
    except URLError as exc:
        raise VoeConvertError(f"Network error while loading {url}: {exc.reason}") from exc


def fetch_text(url: str, timeout: int) -> str:
    return fetch_page(url, timeout)[0]


def is_voe_url(url: str) -> bool:
    try:
        parsed = urlparse(clean_media_url(url))
    except ValueError:
        return False

    path = parsed.path.lower().strip("/")
    if "/hosters/voe" in parsed.path.lower():
        return True
    if "voe" not in parsed.netloc.lower():
        return False

    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"e", "embed", "v"}:
        return len(parts[1]) >= 4
    return False


def text_variants(text: str) -> list[str]:
    variants = [
        text,
        html_lib.unescape(text),
        unquote(html_lib.unescape(text)),
        text.replace("\\/", "/"),
    ]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            variants.append(text.encode("utf-8").decode("unicode_escape"))
    except UnicodeError:
        pass
    return variants


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def extract_voe_links_from_text(text: str, base_url: str | None = None) -> list[str]:
    links: list[str] = []
    for variant in text_variants(text):
        for match in RAW_URL_PATTERN.finditer(variant):
            candidate = clean_media_url(unquote(match.group(0)))
            if is_voe_url(candidate):
                links.append(candidate)

        for match in ATTR_URL_PATTERN.finditer(variant):
            candidate = clean_media_url(unquote(match.group(1)))
            if base_url:
                candidate = urljoin(base_url, candidate)
            if is_voe_url(candidate):
                links.append(candidate)

    return unique(links)


def extract_media_links_from_text(text: str) -> list[str]:
    links: list[str] = []
    for variant in text_variants(text):
        for match in MEDIA_PATTERN.finditer(variant):
            links.append(clean_media_url(unquote(match.group(0))))
    return unique(links)


def parse_extinf_duration(playlist_text: str) -> tuple[float, int]:
    durations = []
    for match in re.finditer(r"#EXTINF:([0-9.]+)", playlist_text, re.IGNORECASE):
        try:
            durations.append(float(match.group(1)))
        except ValueError:
            continue
    return sum(durations), len(durations)


def parse_hls_variants(playlist_text: str, playlist_url: str) -> list[tuple[int, int, str]]:
    variants: list[tuple[int, int, str]] = []
    pending_bandwidth = -1
    pending_pixels = 0
    for raw_line in playlist_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXT-X-STREAM-INF"):
            match = re.search(r"BANDWIDTH=(\d+)", line, re.IGNORECASE)
            pending_bandwidth = int(match.group(1)) if match else 0
            res_match = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.IGNORECASE)
            pending_pixels = (
                int(res_match.group(1)) * int(res_match.group(2))
                if res_match
                else 0
            )
            continue
        if pending_bandwidth >= 0 and not line.startswith("#"):
            variants.append((pending_bandwidth, pending_pixels, urljoin(playlist_url, line)))
            pending_bandwidth = -1
            pending_pixels = 0
    return variants


def estimate_hls_duration(url: str, timeout: int = 15, depth: int = 0) -> tuple[float, int, str]:
    if depth > 2:
        return 0.0, 0, url

    try:
        playlist_text = fetch_text(url, timeout=timeout)
    except VoeConvertError:
        return 0.0, 0, url

    duration, segments = parse_extinf_duration(playlist_text)
    if duration > 0 or segments > 0:
        return duration, segments, url

    variants = parse_hls_variants(playlist_text, url)
    best = (0.0, 0, url)
    for _bandwidth, _pixels, variant_url in sorted(variants, reverse=True):
        candidate = estimate_hls_duration(variant_url, timeout=timeout, depth=depth + 1)
        if (candidate[0], candidate[1]) > (best[0], best[1]):
            best = candidate
    return best


def score_hls_candidate(
    url: str,
    timeout: int = 15,
    depth: int = 0,
) -> tuple[float, int, int, int, str]:
    if depth > 2:
        return 0.0, 0, 0, 0, url

    try:
        playlist_text = fetch_text(url, timeout=timeout)
    except VoeConvertError:
        return 0.0, 0, 0, 0, url

    duration, segments = parse_extinf_duration(playlist_text)
    if duration > 0 or segments > 0:
        return duration, segments, 0, 0, url

    best = (0.0, 0, 0, 0, url)
    for bandwidth, pixels, variant_url in parse_hls_variants(playlist_text, url):
        duration, segments, _child_bandwidth, _child_pixels, resolved_url = (
            score_hls_candidate(variant_url, timeout=timeout, depth=depth + 1)
        )
        candidate = (duration, segments, bandwidth, pixels, resolved_url)
        if (bandwidth, pixels, duration, segments) > (
            best[2],
            best[3],
            best[0],
            best[1],
        ):
            best = candidate
    return best


def choose_best_media_url(
    urls: list[str],
    min_duration: float = 0.0,
    capture_log: list[str] | None = None,
) -> str | None:
    best_url = None
    best_score = (-1.0, -1, -1)
    best_duration = 0.0

    for index, url in enumerate(unique(urls)):
        cleaned = clean_media_url(url)
        parsed_path = urlparse(cleaned).path.lower()

        if ".m3u8" in parsed_path:
            duration, segments, bandwidth, pixels, resolved_url = score_hls_candidate(cleaned)
            duration_ok = 1 if not min_duration or duration >= min_duration else 0
            score = (duration_ok, bandwidth, pixels, duration, segments, 1000 - index)
            if capture_log is not None:
                capture_log.append(
                    "Media candidate HLS: "
                    f"duration={duration:.3f}s segments={segments} "
                    f"bandwidth={bandwidth} pixels={pixels} url={resolved_url}"
                )
            if duration >= min_duration and score > best_score:
                best_url = resolved_url
                best_score = score
                best_duration = duration
            elif not best_url and score > best_score:
                best_url = resolved_url
                best_score = score
                best_duration = duration
            continue

        score = (0, 0, 0, 0.0, 0, 1000 - index)
        if capture_log is not None:
            capture_log.append(f"Media candidate file: url={cleaned}")
        if not best_url or score > best_score:
            best_url = cleaned
            best_score = score
            best_duration = 0.0

    if min_duration and best_duration and best_duration < min_duration:
        raise VoeConvertError(
            f"Best media candidate is only {best_duration:.1f}s; "
            f"minimum requested is {min_duration:.1f}s."
        )

    return best_url


def page_media_min_duration(min_duration: float) -> float:
    return min_duration if min_duration > 0 else DEFAULT_PAGE_MEDIA_MIN_DURATION


def try_choose_best_media_url(
    urls: list[str],
    min_duration: float = 0.0,
    capture_log: list[str] | None = None,
) -> str | None:
    try:
        return choose_best_media_url(
            urls,
            min_duration=min_duration,
            capture_log=capture_log,
        )
    except VoeConvertError as exc:
        if capture_log is not None:
            capture_log.append(f"Ignored media candidates: {exc}")
        return None


def extract_link_candidates(html: str, base_url: str) -> list[str]:
    candidates: list[str] = []
    for match in ATTR_URL_PATTERN.finditer(html):
        raw = clean_media_url(unquote(match.group(1)))
        if not raw or raw.startswith(("#", "javascript:", "mailto:")):
            continue

        start = max(0, match.start() - 160)
        end = min(len(html), match.end() + 160)
        nearby_html = html[start:end]

        absolute = urljoin(base_url, raw)
        parsed_base = urlparse(base_url)
        parsed_candidate = urlparse(absolute)

        same_host = parsed_candidate.netloc == parsed_base.netloc
        useful_path = any(part in parsed_candidate.path.lower() for part in ("redirect", "link", "watch", "embed", "stream", "r"))
        has_hint = bool(VOE_HINT_PATTERN.search(nearby_html))

        if is_voe_url(absolute) or has_hint or (same_host and useful_path):
            candidates.append(absolute)

    return unique(candidates)


def scrape_voe_links_from_page(
    page_url: str,
    timeout: int = 30,
    follow_candidates: bool = True,
    max_candidates: int = 25,
) -> list[str]:
    html, final_url = fetch_page(page_url, timeout=timeout)
    if looks_like_captcha(html):
        raise VoeConvertError(
            "The page returned a captcha/challenge page. Open it in a browser once, then retry."
        )

    found = extract_voe_links_from_text(html, base_url=final_url)
    if found or not follow_candidates:
        return found

    for candidate in extract_link_candidates(html, final_url)[:max_candidates]:
        try:
            candidate_html, candidate_final_url = fetch_page(candidate, timeout=timeout)
        except VoeConvertError:
            continue

        if is_voe_url(candidate_final_url):
            found.append(candidate_final_url)
            continue

        found.extend(extract_voe_links_from_text(candidate_html, base_url=candidate_final_url))

    return unique(found)


def require_patchright():
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        raise VoeConvertError(
            "Browser mode needs patchright. Install it with: "
            "pip install patchright && python -m patchright install chromium"
        ) from exc
    return sync_playwright


def chromium_launch_args(use_cloudflare_dns: bool = True) -> list[str]:
    args = [
        "--disable-logging",
        "--log-level=3",
        "--v=0",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if not use_cloudflare_dns:
        return args
    args.extend(
        [
            "--allow-config-for-managed-device",
            "--enable-features=DnsOverHttps",
            "--dns-over-https-mode=secure",
            f"--dns-over-https-templates={CLOUDFLARE_DOH_TEMPLATE}",
            "--dns-over-https.secure-dns-mode=secure",
            f"--dns-over-https.templates={CLOUDFLARE_DOH_TEMPLATE}",
        ]
    )
    return args


def configure_chromium_profile(
    use_cloudflare_dns: bool = True,
    reset_profile: bool = False,
) -> Path:
    if reset_profile and PROFILE_DIR.exists():
        shutil.rmtree(PROFILE_DIR)

    default_dir = PROFILE_DIR / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"

    prefs = {}
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prefs = {}

    prefs["built_in_dns_client_enabled"] = True
    prefs["dns_over_https"] = {
        "mode": "secure" if use_cloudflare_dns else "automatic",
        "templates": CLOUDFLARE_DOH_TEMPLATE if use_cloudflare_dns else "",
    }
    prefs["session"] = {
        "restore_on_startup": 5,
    }

    prefs_path.write_text(
        json.dumps(prefs, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    local_state_path = PROFILE_DIR / "Local State"
    local_state = {}
    if local_state_path.exists():
        try:
            local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            local_state = {}

    local_state["built_in_dns_client_enabled"] = True
    local_state["dns_over_https"] = {
        "mode": "secure" if use_cloudflare_dns else "automatic",
        "templates": CLOUDFLARE_DOH_TEMPLATE if use_cloudflare_dns else "",
    }
    local_state_path.write_text(
        json.dumps(local_state, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return PROFILE_DIR


def launch_browser_context(
    playwright,
    ignore_https_errors: bool = True,
    use_cloudflare_dns: bool = True,
    reset_profile: bool = False,
):
    profile_dir = configure_chromium_profile(
        use_cloudflare_dns,
        reset_profile=reset_profile,
    )
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=False,
        args=chromium_launch_args(use_cloudflare_dns),
        ignore_https_errors=ignore_https_errors,
    )


def browser_primary_page(context):
    pages = list(context.pages)
    if not pages:
        return context.new_page()

    primary = pages[0]
    for page in pages[1:]:
        try:
            page.close()
        except Exception:
            pass
    return primary


def adblock_reason(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None

    if not host:
        return None

    for part in AD_BLOCK_HOST_PARTS:
        if part in host:
            return part
    return None


def install_browser_adblock(context, capture_log: list[str] | None = None) -> None:
    context.add_init_script(
        """
        (() => {
            const blockOpen = () => {
                try {
                    Object.defineProperty(window, 'open', {
                        value: () => null,
                        configurable: true
                    });
                } catch (_) {
                    window.open = () => null;
                }
            };
            blockOpen();
        })();
        """
    )

    def route_handler(route, request) -> None:
        reason = adblock_reason(request.url)
        if reason:
            if capture_log is not None:
                capture_log.append(f"Adblock blocked ({reason}): {request.url}")
            route.abort()
            return
        route.continue_()

    context.route("**/*", route_handler)


def install_popup_blocker(page, capture_log: list[str] | None = None) -> None:
    def close_popup(popup) -> None:
        try:
            if capture_log is not None:
                capture_log.append(f"Popup closed: {popup.url}")
            popup.close()
        except Exception:
            pass

    page.on("popup", close_popup)


def install_main_navigation_guard(
    page,
    original_url: str,
    capture_log: list[str] | None = None,
) -> None:
    original_host = urlparse(original_url).netloc.lower()
    returning_to_original = {"active": False}

    def should_block_main_target(target_url: str) -> bool:
        target_host = urlparse(target_url).netloc.lower()
        return bool(
            target_host
            and target_url != original_url
            and target_host != original_host
            and (is_voe_url(target_url) or adblock_reason(target_url))
        )

    def route_handler(route, request) -> None:
        try:
            frame = request.frame
            is_main_frame = frame == page.main_frame or getattr(frame, "parent_frame", None) is None
            is_main_document = request.is_navigation_request() and is_main_frame
        except Exception:
            is_main_document = False

        if not is_main_document:
            route.continue_()
            return

        target_url = request.url
        if not returning_to_original["active"] and should_block_main_target(target_url):
            if capture_log is not None:
                capture_log.append(f"Main navigation blocked: {target_url}")
            route.abort()
            return

        route.continue_()

    def handle_frame_navigated(frame) -> None:
        try:
            is_main_frame = frame == page.main_frame or getattr(frame, "parent_frame", None) is None
            current_url = frame.url
        except Exception:
            return

        if (
            not is_main_frame
            or returning_to_original["active"]
            or not should_block_main_target(current_url)
        ):
            return

        if capture_log is not None:
            capture_log.append(f"Main navigation reverted: {current_url}")
        try:
            returning_to_original["active"] = True
            page.goto(original_url, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            if capture_log is not None:
                capture_log.append(f"Main navigation revert failed: {exc}")
        finally:
            returning_to_original["active"] = False

    page.route("**/*", route_handler)
    page.on("framenavigated", handle_frame_navigated)


def simplify_media_title(title: str) -> str:
    cleaned = html_lib.unescape(title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    site_suffix_patterns = (
        r"\s+[-|]\s+[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}\s*$",
        r"\s+[-|]\s+[^-|]*(?:kino|stream|movie|film|watch|download)[^-|]*$",
    )
    for pattern in site_suffix_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

    noise_patterns = (
        r"\s+(?:stream\s+)?online\s+anschauen(?:\s+und\s+downloaden)?(?:\s+auf\s+.+)?$",
        r"\s+anschauen\s+und\s+downloaden(?:\s+auf\s+.+)?$",
        r"\s+downloaden(?:\s+auf\s+.+)?$",
        r"\s+(?:ganzer\s+film|kompletter\s+film)\s+.*$",
        r"\s+(?:deutsch|german)\s+stream\s+.*$",
        r"\s+(?:stream|streams)\s+(?:deutsch|german|hd|online).*$",
    )
    for pattern in noise_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

    cleaned = re.sub(r"\s+[-|]\s*$", "", cleaned).strip()
    return cleaned


def clean_title_for_filename(title: str | None) -> str | None:
    if not title:
        return None

    cleaned = simplify_media_title(title)
    cleaned = re.sub(r"""[<>:"/\\|?*\x00-\x1f]+""", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if not cleaned:
        return None

    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
    if cleaned.upper() in reserved:
        cleaned = f"{cleaned}_video"
    return cleaned[:180].strip(" ._") or None


def extract_browser_page_title(page) -> str | None:
    try:
        value = page.evaluate(
            """
            () => {
                const candidates = [
                    document.querySelector('meta[property="og:title"]')?.content,
                    document.querySelector('meta[name="twitter:title"]')?.content,
                    document.querySelector('h1')?.innerText,
                    document.title
                ];
                return candidates.find((item) => item && item.trim()) || '';
            }
            """
        )
    except Exception:
        return None
    return clean_title_for_filename(str(value))


def run_dns_check(
    timeout: int,
    use_cloudflare_dns: bool = True,
    reset_profile: bool = False,
) -> None:
    sync_playwright = require_patchright()
    with sync_playwright() as playwright:
        context = launch_browser_context(
            playwright,
            ignore_https_errors=False,
            use_cloudflare_dns=use_cloudflare_dns,
            reset_profile=reset_profile,
        )
        try:
            page = context.new_page()
            page.goto("https://1.1.1.1/help", wait_until="networkidle", timeout=timeout * 1000)
            text = page.locator("body").inner_text(timeout=5000)
            sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
            sys.stdout.buffer.write(b"\n")
        finally:
            context.close()


def collect_voe_links_from_browser(context) -> list[str]:
    links: list[str] = []
    for page in context.pages:
        try:
            if is_voe_url(page.url):
                links.append(page.url)
        except Exception:
            pass

        for frame in page.frames:
            try:
                if is_voe_url(frame.url):
                    links.append(frame.url)
            except Exception:
                pass

            try:
                links.extend(extract_voe_links_from_text(frame.content(), frame.url))
            except Exception:
                pass

        try:
            links.extend(extract_voe_links_from_text(page.content(), page.url))
        except Exception:
            pass

    return unique(links)


def remember_browser_url(
    url: str,
    captured_voe: list[str],
    captured_media: list[str],
) -> None:
    try:
        cleaned = clean_media_url(url)
        if is_voe_url(cleaned) and cleaned not in captured_voe:
            captured_voe.append(cleaned)
        if MEDIA_PATTERN.search(cleaned) and cleaned not in captured_media:
            captured_media.append(cleaned)

        for voe_url in extract_voe_links_from_text(cleaned):
            if voe_url not in captured_voe:
                captured_voe.append(voe_url)
    except Exception:
        pass


def inspect_browser_response(
    response,
    captured_voe: list[str],
    captured_media: list[str],
    capture_log: list[str] | None = None,
) -> None:
    remember_browser_url(response.url, captured_voe, captured_media)

    try:
        content_type = (response.header_value("content-type") or "").lower()
    except Exception:
        content_type = ""

    text_like = (
        "text/" in content_type
        or "json" in content_type
        or "javascript" in content_type
        or "xml" in content_type
        or "html" in content_type
    )
    if not text_like:
        return

    try:
        body_text = response.text()
    except Exception:
        return

    for voe_url in extract_voe_links_from_text(body_text, response.url):
        if voe_url not in captured_voe:
            captured_voe.append(voe_url)
            if capture_log is not None:
                capture_log.append(f"VOE from response body: {voe_url}")

    for media_url in extract_media_links_from_text(body_text):
        if media_url not in captured_media:
            captured_media.append(media_url)
            if capture_log is not None:
                capture_log.append(f"Media from response body: {media_url}")


def wait_for_browser_voe_links(
    context,
    timeout: int,
    captured_voe: list[str] | None = None,
    captured_media: list[str] | None = None,
    auto_click_voe: bool = False,
    prefer_media: bool = False,
    voe_fallback_seconds: int = 60,
) -> list[str]:
    deadline = time.time() + timeout
    last_click_attempt = 0.0
    first_voe_at: float | None = None
    remembered_links: list[str] = []
    while time.time() < deadline:
        links = list(captured_voe or [])
        links.extend(collect_voe_links_from_browser(context))
        if links:
            remembered_links = unique(links)
            if not prefer_media:
                return remembered_links
            if first_voe_at is None:
                first_voe_at = time.time()
            if time.time() - first_voe_at >= voe_fallback_seconds:
                return remembered_links
        if captured_media:
            return []

        if auto_click_voe and time.time() - last_click_attempt >= 3:
            click_voe_provider_candidates(context)
            last_click_attempt = time.time()

        time.sleep(1)
    return remembered_links if remembered_links and not captured_media else []


def wait_for_media_candidates(
    timeout: int,
    captured_media: list[str],
    settle_seconds: int,
) -> None:
    deadline = time.time() + timeout
    last_count = len(captured_media)
    stable_since = time.time()

    while time.time() < deadline:
        current_count = len(captured_media)
        if current_count != last_count:
            last_count = current_count
            stable_since = time.time()
        if captured_media and time.time() - stable_since >= settle_seconds:
            return
        time.sleep(1)


def click_voe_provider_candidates(context) -> bool:
    selectors = (
        "a:has-text('VOE')",
        "button:has-text('VOE')",
        "[role='button']:has-text('VOE')",
        "[data-provider='VOE']",
        "[data-provider='voe']",
    )
    clicked = False
    for page in context.pages:
        targets = [page, *page.frames]
        for target in targets:
            for selector in selectors:
                try:
                    locator = target.locator(selector).first
                    locator.wait_for(state="visible", timeout=300)
                    locator.click(timeout=500)
                    clicked = True
                    break
                except Exception:
                    continue
            if clicked:
                try:
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                return True
    return False


def extract_media_from_browser_pages(context, captured_media: list[str]) -> str | None:
    if captured_media:
        return captured_media[0]

    for page in context.pages:
        for frame in page.frames:
            try:
                source = extract_source_from_html(frame.content())
                if source:
                    return source
            except Exception:
                pass

        try:
            source = extract_source_from_html(page.content())
            if source:
                return source
        except Exception:
            pass

    return None


def browser_convert_voe_url(
    context,
    voe_url: str,
    timeout: int,
    settle_seconds: int,
    min_duration: float,
    capture_log: list[str] | None = None,
    adblock: bool = True,
) -> str:
    captured_media: list[str] = []

    def remember_media(response) -> None:
        try:
            media_url = clean_media_url(response.url)
            if MEDIA_PATTERN.search(media_url) and media_url not in captured_media:
                captured_media.append(media_url)
        except Exception:
            pass

    page = context.new_page()
    if adblock:
        install_popup_blocker(page, capture_log)
    page.on("response", remember_media)
    try:
        page.goto(voe_url, wait_until="domcontentloaded", timeout=timeout * 1000)
    except Exception as exc:
        raise VoeConvertError(f"Browser could not open VOE URL: {exc}") from exc

    deadline = time.time() + timeout
    while time.time() < deadline:
        source = extract_media_from_browser_pages(context, captured_media)
        if source and source not in captured_media:
            captured_media.append(source)
        if captured_media:
            wait_for_media_candidates(
                timeout=max(1, min(settle_seconds, int(deadline - time.time()))),
                captured_media=captured_media,
                settle_seconds=settle_seconds,
            )
            best = try_choose_best_media_url(
                captured_media,
                min_duration=min_duration,
                capture_log=capture_log,
            )
            if best:
                return best
        time.sleep(1)

    raise VoeConvertError("No direct media link found in browser mode.")


def browser_convert_input_url(
    url: str,
    timeout: int,
    ignore_https_errors: bool = True,
    auto_click_voe: bool = False,
    use_cloudflare_dns: bool = True,
    reset_profile: bool = False,
    capture_log_path: str | None = None,
    media_settle_seconds: int = 6,
    min_duration: float = 0.0,
    adblock: bool = True,
) -> list[dict[str, str]]:
    sync_playwright = require_patchright()
    with sync_playwright() as playwright:
        context = launch_browser_context(
            playwright,
            ignore_https_errors=ignore_https_errors,
            use_cloudflare_dns=use_cloudflare_dns,
            reset_profile=reset_profile,
        )
        capture_log: list[str] = []
        try:
            if adblock:
                install_browser_adblock(context, capture_log)
            captured_voe: list[str] = []
            captured_media: list[str] = []

            def handle_browser_request(request) -> None:
                try:
                    capture_log.append(f"Request: {request.url}")
                    remember_browser_url(
                        request.url,
                        captured_voe,
                        captured_media,
                    )
                except Exception as exc:
                    capture_log.append(f"Ignored request inspection error: {exc}")

            def handle_browser_response(response) -> None:
                try:
                    inspect_browser_response(
                        response,
                        captured_voe,
                        captured_media,
                        capture_log,
                    )
                except Exception as exc:
                    capture_log.append(f"Ignored response inspection error: {exc}")

            context.on("request", handle_browser_request)
            context.on("response", handle_browser_response)

            page = browser_primary_page(context)
            if adblock:
                install_popup_blocker(page, capture_log)
                if not is_voe_url(url):
                    install_main_navigation_guard(page, url, capture_log)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page_title = extract_browser_page_title(page)

            if is_voe_url(url):
                voe_urls = [url]
            else:
                voe_urls = wait_for_browser_voe_links(
                    context,
                    timeout,
                    captured_voe=captured_voe,
                    captured_media=captured_media,
                    auto_click_voe=auto_click_voe,
                    prefer_media=True,
                    voe_fallback_seconds=min(20, max(10, timeout // 10)),
                )

            if not voe_urls and captured_media:
                wait_for_media_candidates(
                    timeout=media_settle_seconds,
                    captured_media=captured_media,
                    settle_seconds=media_settle_seconds,
                )
                best_media = try_choose_best_media_url(
                    captured_media,
                    min_duration=page_media_min_duration(min_duration),
                    capture_log=capture_log,
                )
                page_title = extract_browser_page_title(page) or page_title
                if not best_media and captured_voe:
                    voe_urls = unique(captured_voe)
                elif not best_media:
                    raise VoeConvertError("No usable media candidate found.")
                else:
                    cookie_header = cookie_header_for_url(context, best_media)
                    results = [
                        {
                            "url": url,
                            "voe": url,
                            "direct": best_media,
                            "title": page_title or "",
                            "referer": url,
                            "cookie_header": cookie_header,
                            "status": "ok",
                        }
                    ]
                    return results

            if not voe_urls:
                raise VoeConvertError("No VOE link found in browser mode.")

            results: list[dict[str, str]] = []
            for voe_url in voe_urls:
                direct = browser_convert_voe_url(
                    context,
                    voe_url,
                    timeout,
                    settle_seconds=media_settle_seconds,
                    min_duration=min_duration,
                    capture_log=capture_log,
                    adblock=adblock,
                )
                page_title = extract_browser_page_title(page) or page_title
                results.append(
                    {
                        "url": url,
                        "voe": voe_url,
                        "direct": direct,
                        "title": page_title or "",
                        "referer": voe_url,
                        "cookie_header": cookie_header_for_url(context, direct),
                        "status": "ok",
                    }
                )

            return results
        except VoeConvertError:
            raise
        except Exception as exc:
            raise VoeConvertError(f"Browser mode failed: {exc}") from exc
        finally:
            if capture_log_path:
                Path(capture_log_path).write_text(
                    "\n".join(capture_log) + "\n",
                    encoding="utf-8",
                )
            context.close()


def looks_like_captcha(html: str) -> bool:
    lowered = html.lower()
    indicators = (
        "captcha",
        "cloudflare",
        "checking your browser",
        "verify you are human",
        "cf-challenge",
    )
    return any(indicator in lowered for indicator in indicators)


def extract_source_from_html(html: str) -> str | None:
    script_blocks = re.findall(
        r"""<script\s+type=["']application/json["']>(.*?)</script>""",
        html,
        re.DOTALL | re.IGNORECASE,
    )
    for block in script_blocks:
        try:
            decoded = decode_voe_payload(decode_script_string(block))
        except VoeConvertError:
            continue
        source = decoded.get("source") or decoded.get("hls")
        if source:
            return clean_media_url(str(source))

    match = B64_PATTERN.search(html)
    if match:
        try:
            decoded = decode_voe_payload(match.group(1))
            source = decoded.get("source") or decoded.get("hls")
            if source:
                return clean_media_url(str(source))
        except VoeConvertError:
            pass

    for pattern in (HLS_PATTERN, SOURCE_PATTERN, M3U8_PATTERN):
        match = pattern.search(html)
        if match:
            value = match.groupdict().get("hls") or match.groupdict().get("source")
            return clean_media_url(value or match.group(0))

    return None


def convert_voe_url(url: str, retries: int = 3, timeout: int = 30) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise VoeConvertError(f"Invalid URL: {url!r}")

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            html = fetch_text(url, timeout=timeout)
            if looks_like_captcha(html):
                raise VoeConvertError(
                    "VOE returned a captcha/challenge page. Open the link in a "
                    "browser once, then retry."
                )

            source = extract_source_from_html(html)
            if source:
                return source

            redirect = REDIRECT_PATTERN.search(html)
            if redirect:
                redirect_url = redirect.group(1).strip()
                html = fetch_text(redirect_url, timeout=timeout)
                if looks_like_captcha(html):
                    raise VoeConvertError(
                        "VOE returned a captcha/challenge page after redirect."
                    )
                source = extract_source_from_html(html)
                if source:
                    return source

            raise VoeConvertError("No direct media link found on the VOE page.")
        except VoeConvertError as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))

    raise VoeConvertError(str(last_error) if last_error else "Unknown conversion error.")


def resolve_input_to_voe_urls(
    url: str,
    timeout: int,
    scrape_pages: bool,
    follow_candidates: bool,
) -> list[str]:
    if is_voe_url(url):
        return [url]
    if not scrape_pages:
        return [url]
    return scrape_voe_links_from_page(
        url,
        timeout=timeout,
        follow_candidates=follow_candidates,
    )


def read_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.urls)
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    if not urls and not sys.stdin.isatty():
        for line in sys.stdin.read().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def filename_from_url(url: str, index: int) -> str:
    path = urlparse(url).path.strip("/")
    code = path.split("/")[-1] if path else f"video_{index}"
    if code.lower().endswith((".html", ".htm")):
        code = code.rsplit(".", 1)[0]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", code).strip("._")
    return safe or f"video_{index}"


def filename_from_item(item: dict[str, str], index: int) -> str:
    title_name = clean_title_for_filename(item.get("title"))
    if title_name:
        return title_name

    name_source = item.get("url") or item.get("voe") or item.get("direct") or ""
    return filename_from_url(name_source, index)


def cookie_header_for_url(context, url: str) -> str:
    try:
        cookies = context.cookies([url])
    except Exception:
        try:
            cookies = context.cookies()
        except Exception:
            cookies = []

    parts = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def estimate_media_duration(url: str) -> float:
    parsed_path = urlparse(url).path.lower()
    if ".m3u8" in parsed_path:
        duration, _segments, _resolved = estimate_hls_duration(url)
        return duration
    return 0.0


def parse_ffmpeg_time(value: str) -> float:
    value = value.strip()
    if not value or value == "N/A":
        return 0.0
    if value.isdigit():
        return int(value) / 1_000_000

    match = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def progress_percent(done_seconds: float, total_seconds: float) -> float:
    if total_seconds > 0:
        return min(100.0, max(0.0, done_seconds / total_seconds * 100))
    return 0.0


def print_download_progress(done_seconds: float, total_seconds: float) -> None:
    if total_seconds > 0:
        percent = progress_percent(done_seconds, total_seconds)
        filled = int(percent // 4)
        bar = "#" * filled + "-" * (25 - filled)
        message = f"Fortschritt: [{bar}] {percent:5.1f}%"
    else:
        message = f"Fortschritt: {done_seconds:0.0f}s geladen"

    width = max(40, shutil.get_terminal_size((100, 20)).columns - 1)
    print("\r" + message[:width].ljust(width), end="", flush=True)


def progress_bucket(done_seconds: float, total_seconds: float) -> int:
    if total_seconds > 0:
        return int(progress_percent(done_seconds, total_seconds) // 5) * 5
    return int(done_seconds // 30)


def is_ffmpeg_progress_line(line: str) -> bool:
    progress_keys = (
        "bitrate",
        "drop_frames",
        "dup_frames",
        "fps",
        "frame",
        "out_time",
        "out_time_ms",
        "out_time_us",
        "progress",
        "speed",
        "stream_",
        "total_size",
    )
    return any(line.startswith(f"{key}=") for key in progress_keys)


def redact_ffmpeg_diagnostic(line: str) -> str:
    line = re.sub(r"Cookie:\s*[^\r\n]+", "Cookie: <redacted>", line, flags=re.IGNORECASE)
    line = re.sub(r"(https?://[^\s]+)", "<url>", line)
    return line


def format_ffmpeg_return_code(return_code: int) -> str:
    if return_code >= 0:
        unsigned_code = return_code
        signed_code = return_code - (1 << 32) if return_code > 0x7FFFFFFF else return_code
        if signed_code != return_code:
            return f"{return_code} (0x{unsigned_code:08x}, signed {signed_code})"
        return f"{return_code} (0x{unsigned_code:08x})"
    return str(return_code)


def temporary_download_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")


def find_ffmpeg_executable() -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path

    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise VoeConvertError(
            "ffmpeg was not found. Run install.bat first, or install ffmpeg manually."
        ) from exc

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    if ffmpeg_path and Path(ffmpeg_path).exists():
        return ffmpeg_path

    raise VoeConvertError(
        "ffmpeg was not found. Run install.bat first, or install ffmpeg manually."
    )


def download_with_ffmpeg(
    media_url: str,
    output_path: Path,
    referer: str | None = None,
    cookie_header: str | None = None,
    show_progress: bool = True,
    container: str = "mkv",
    download_retries: int = 3,
) -> None:
    ffmpeg_executable = find_ffmpeg_executable()

    container = (container or "mkv").lower()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = temporary_download_path(output_path)
    headers_map = dict(DEFAULT_HEADERS)
    if referer:
        headers_map["Referer"] = referer
    if cookie_header:
        headers_map["Cookie"] = cookie_header

    headers = "".join(f"{key}: {value}\r\n" for key, value in headers_map.items())
    command_prefix = [
        ffmpeg_executable,
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-rw_timeout",
        "15000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "408,429,500,502,503,504",
        "-reconnect_delay_max",
        "5",
        "-reconnect_max_retries",
        "3",
        "-reconnect_delay_total_max",
        "30",
        "-headers",
        headers,
        "-i",
        media_url,
        "-c",
        "copy",
    ]
    if container == "mp4":
        command_prefix.extend(["-movflags", "+faststart"])

    total_seconds = estimate_media_duration(media_url)
    if show_progress:
        print(f"Download gestartet: {output_path.name}", flush=True)

    attempts = max(1, download_retries)
    last_message = ""
    for attempt in range(1, attempts + 1):
        if temporary_path.exists():
            temporary_path.unlink()
        command = [*command_prefix, "-y", str(temporary_path)]
        if show_progress and attempt > 1:
            print(f"Download erneut versucht ({attempt}/{attempts})...", flush=True)

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        last_done = 0.0
        last_bucket = -1
        last_progress_time = 0.0
        diagnostic_lines: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                last_done = parse_ffmpeg_time(line.split("=", 1)[1])
                if show_progress:
                    bucket = progress_bucket(last_done, total_seconds)
                    now = time.time()
                    if bucket != last_bucket or now - last_progress_time >= 30:
                        print_download_progress(last_done, total_seconds)
                        last_bucket = bucket
                        last_progress_time = now
            elif line.startswith("out_time="):
                last_done = parse_ffmpeg_time(line.split("=", 1)[1])
                if show_progress:
                    bucket = progress_bucket(last_done, total_seconds)
                    now = time.time()
                    if bucket != last_bucket or now - last_progress_time >= 30:
                        print_download_progress(last_done, total_seconds)
                        last_bucket = bucket
                        last_progress_time = now
            elif line.startswith("progress=end"):
                if show_progress:
                    print_download_progress(total_seconds or last_done, total_seconds)
            elif line and not is_ffmpeg_progress_line(line):
                diagnostic_lines.append(redact_ffmpeg_diagnostic(line))
                diagnostic_lines = diagnostic_lines[-8:]

        return_code = process.wait()
        if show_progress:
            print()
        if return_code == 0:
            temporary_path.replace(output_path)
            return

        details = "\n".join(diagnostic_lines)
        last_message = f"ffmpeg failed with exit code {format_ffmpeg_return_code(return_code)}."
        if details:
            last_message = f"{last_message}\nLast ffmpeg output:\n{details}"
        else:
            last_message = f"{last_message}\nNo ffmpeg diagnostic output was captured."

    if temporary_path.exists():
        temporary_path.unlink()
    raise VoeConvertError(last_message)


def download_items_with_browser_context(
    items: list[dict[str, str]],
    context,
    download_dir: str,
    base_index: int,
    show_progress: bool = True,
    container: str = "mkv",
    download_retries: int = 3,
) -> None:
    container = (container or "mkv").lower()
    for item_index, item in enumerate(items, start=1):
        name_index = base_index if len(items) == 1 else int(f"{base_index}{item_index}")
        target = Path(download_dir) / f"{filename_from_item(item, name_index)}.{container}"
        cookie_header = cookie_header_for_url(context, item["direct"])
        download_with_ffmpeg(
            item["direct"],
            target,
            referer=item.get("voe") or item.get("url"),
            cookie_header=cookie_header,
            show_progress=show_progress,
            container=container,
            download_retries=download_retries,
        )
        item["file"] = str(target)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert VOE links, or pages containing VOE links, to direct media links.",
    )
    parser.add_argument("urls", nargs="*", help="VOE URLs or player/page URLs to convert.")
    parser.add_argument("-f", "--file", help="Text file with one URL per line.")
    parser.add_argument(
        "-o",
        "--output",
        help="Write converted direct links to this text file.",
    )
    parser.add_argument(
        "-d",
        "--download",
        action="store_true",
        help="Download converted links with ffmpeg.",
    )
    parser.add_argument(
        "--download-dir",
        default="downloads",
        help="Folder for downloads when --download is used.",
    )
    parser.add_argument(
        "--container",
        choices=["mkv", "mp4"],
        default="mkv",
        help="Output container for downloads.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Number of conversion attempts per URL.",
    )
    parser.add_argument(
        "--download-retries",
        type=int,
        default=3,
        help="Number of ffmpeg download attempts after network or HTTP failures.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print results as JSON.",
    )
    parser.add_argument(
        "--no-scrape-page",
        action="store_true",
        help="Treat inputs as VOE links only; do not scrape non-VOE pages.",
    )
    parser.add_argument(
        "--no-follow-candidates",
        action="store_true",
        help="Only scan the given page HTML; do not request possible redirect links.",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Use a visible Chromium browser for JS/captcha/player pages.",
    )
    parser.add_argument(
        "--browser-timeout",
        type=int,
        default=300,
        help="Seconds to wait in browser mode.",
    )
    parser.add_argument(
        "--strict-https",
        action="store_true",
        help="Do not ignore HTTPS certificate errors in browser mode.",
    )
    parser.add_argument(
        "--auto-click-voe",
        action="store_true",
        help="Automatically click very visible VOE provider candidates in browser mode.",
    )
    parser.add_argument(
        "--no-auto-click-voe",
        action="store_true",
        help="Deprecated compatibility option; auto-click is off unless --auto-click-voe is used.",
    )
    parser.add_argument(
        "--no-cloudflare-dns",
        action="store_true",
        help="Do not force Cloudflare DNS-over-HTTPS for the browser.",
    )
    parser.add_argument(
        "--adblock",
        dest="adblock",
        action="store_true",
        default=True,
        help="Enable browser popup and ad-domain blocking.",
    )
    parser.add_argument(
        "--no-adblock",
        dest="adblock",
        action="store_false",
        help="Disable browser popup and ad-domain blocking.",
    )
    parser.add_argument(
        "--reset-browser-profile",
        action="store_true",
        help="Delete and recreate the converter Chromium profile before launch.",
    )
    parser.add_argument(
        "--dns-check",
        action="store_true",
        help="Open Cloudflare's DNS help page with the same browser settings and print the result.",
    )
    parser.add_argument(
        "--capture-log",
        help="Write browser request/response capture details to this text file.",
    )
    parser.add_argument(
        "--media-settle-seconds",
        type=int,
        default=6,
        help="Seconds to keep collecting media candidates after the first one appears.",
    )
    parser.add_argument(
        "--min-duration",
        type=float,
        default=0.0,
        help="Reject HLS media candidates shorter than this many seconds.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable ffmpeg download progress display.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dns_check:
        try:
            run_dns_check(
                timeout=args.browser_timeout,
                use_cloudflare_dns=not args.no_cloudflare_dns,
                reset_profile=args.reset_browser_profile,
            )
            return 0
        except VoeConvertError as exc:
            print(f"ERROR DNS check: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"ERROR DNS check failed: {exc}", file=sys.stderr)
            return 1

    urls = read_urls(args)
    if not urls:
        parser.error("provide at least one URL, --file, or piped input")

    results: list[dict[str, str]] = []
    exit_code = 0

    for index, url in enumerate(urls, start=1):
        try:
            if args.browser:
                converted_items = browser_convert_input_url(
                    url,
                    timeout=args.browser_timeout,
                    ignore_https_errors=not args.strict_https,
                    auto_click_voe=args.auto_click_voe and not args.no_auto_click_voe,
                    use_cloudflare_dns=not args.no_cloudflare_dns,
                    reset_profile=args.reset_browser_profile,
                    capture_log_path=args.capture_log,
                    media_settle_seconds=max(1, args.media_settle_seconds),
                    min_duration=max(0.0, args.min_duration),
                    adblock=args.adblock,
                )
            else:
                voe_urls = resolve_input_to_voe_urls(
                    url,
                    timeout=args.timeout,
                    scrape_pages=not args.no_scrape_page,
                    follow_candidates=not args.no_follow_candidates,
                )
                if not voe_urls:
                    raise VoeConvertError("No VOE link found on this page.")

                converted_items = []
                for voe_url in voe_urls:
                    direct = convert_voe_url(
                        voe_url,
                        retries=args.retries,
                        timeout=args.timeout,
                    )
                    converted_items.append(
                        {
                            "url": url,
                            "voe": voe_url,
                            "direct": direct,
                            "status": "ok",
                        }
                    )

            for voe_index, item in enumerate(converted_items, start=1):
                item = {
                    **item,
                    "status": "ok",
                }
                results.append(item)

                if args.download and not item.get("file"):
                    name_index = (
                        index
                        if len(converted_items) == 1
                        else int(f"{index}{voe_index}")
                    )
                    target = (
                        Path(args.download_dir)
                        / f"{filename_from_item(item, name_index)}.{args.container}"
                    )
                    download_with_ffmpeg(
                        item["direct"],
                        target,
                        referer=item.get("referer") or item.get("voe") or item.get("url"),
                        cookie_header=item.get("cookie_header"),
                        show_progress=not args.no_progress,
                        container=args.container,
                        download_retries=args.download_retries,
                    )
                    item["file"] = str(target)

                if not args.json and not args.download:
                    print(item["direct"])
        except (VoeConvertError, subprocess.CalledProcessError) as exc:
            exit_code = 1
            results.append({"url": url, "error": str(exc), "status": "error"})
            if not args.json:
                print(f"ERROR {url}: {exc}", file=sys.stderr)

    if args.output:
        converted = [item["direct"] for item in results if item.get("status") == "ok"]
        Path(args.output).write_text("\n".join(converted) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(results, indent=2))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
