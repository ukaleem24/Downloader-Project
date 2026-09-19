"""
Batch downloader for file pages that require clicking a "Download" button
(e.g. fuckingfast.co links) before the real file is served.

The site sits behind a Cloudflare bot-check (plain HTTP requests get a 403)
and opens ad tabs before the real download link becomes available. This
script drives your real installed Chrome via Playwright (falls back to
bundled Chromium if Chrome isn't found) just far enough to click through to
the real signed file URL, then downloads that URL directly with `requests`
-- Chrome's own download manager turned out to be unreliable for this site
(it tends to tear down the tab/browser right as the download starts), so we
sidestep it entirely once we have the link.

Usage:
    pip install -r requirements.txt
    playwright install chromium
    python download.py --links links.txt --out "C:\\path\\to\\folder"

By default the browser runs minimized and off-screen so it never pops up in
front of you -- it's still a real, fully-rendered Chrome (unlike --headless),
so Cloudflare's check still passes.

Optional flags:
    --show              Show the browser window normally (useful the first
                         time, or if you need to solve a CAPTCHA by hand).
    --headless          Run fully headless instead. Not recommended: more
                         likely to get blocked by Cloudflare's bot check.
    --timeout 90        Seconds to wait for the real download link to appear.
    --delay 3           Seconds to wait between links.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

URL_RE = re.compile(r"https?://[^\s()\[\]<>\"']+")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Selectors tried in order to find the actual download trigger on the page.
# Broad on purpose since the exact markup isn't known ahead of time.
DOWNLOAD_SELECTORS = [
    "#download",
    "a#download",
    "button#download",
    "a.download-btn",
    "button.download-btn",
    "a:has-text('Download')",
    "button:has-text('Download')",
    "text=/^\\s*Download\\s*$/i",
]


def parse_links(path: Path) -> list[str]:
    links = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = URL_RE.search(line)
        if match:
            links.append(match.group(0))
    # de-dupe, keep order
    seen = set()
    unique = []
    for link in links:
        if link not in seen:
            seen.add(link)
            unique.append(link)
    return unique


def find_download_trigger(page):
    for selector in DOWNLOAD_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def wait_until_clickable(locator, timeout_s: float) -> bool:
    """Some sites disable the button behind a countdown timer."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if locator.is_enabled() and locator.is_visible():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def stream_download(url: str, referer: str, dest: Path) -> int:
    """Fetch the signed file URL directly, independent of the browser."""
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Referer": referer}
    tmp_dest = dest.with_name(dest.name + ".part")
    total = 0
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp_dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
    tmp_dest.replace(dest)
    return total


def download_one(page, context, url: str, out_dir: Path, timeout_s: float) -> tuple[bool, str]:
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeoutError:
        pass  # some pages keep long-polling; not fatal

    try:
        content = page.content().lower()
    except Exception:
        content = ""
    if "cloudflare" in content and "verification failed" in content:
        return False, "blocked by Cloudflare bot check"

    trigger = find_download_trigger(page)
    if trigger is None:
        return False, "no download button found on page"

    if not wait_until_clickable(trigger, timeout_s=min(timeout_s, 30)):
        return False, "download button never became clickable"

    # Chrome's own download manager tends to tear the tab/browser down right
    # as the transfer starts on this site, so we only use the browser to get
    # as far as the signed file URL: capture it from the "download" event,
    # cancel Chrome's copy immediately, and fetch the bytes ourselves below.
    captured = {}

    def on_download(d):
        if "url" not in captured:
            captured["url"] = d.url
            captured["filename"] = d.suggested_filename
        try:
            d.cancel()
        except Exception:
            pass

    page.on("download", on_download)
    try:
        # The site opens an ad tab on the first click and the real link can
        # take 10-15s to become live, so keep clicking (closing ad tabs as
        # they appear) until the download event fires.
        deadline = time.monotonic() + timeout_s
        last_click = 0.0
        while time.monotonic() < deadline and "url" not in captured:
            if time.monotonic() - last_click > 8:
                try:
                    trigger.click(timeout=5_000, no_wait_after=True)
                except Exception:
                    pass
                last_click = time.monotonic()

            try:
                for pg in list(context.pages):
                    if pg is not page and not pg.is_closed() and pg.url != "about:blank":
                        pg.close()
            except Exception:
                pass  # page/context may already be gone; the listener above still works

            time.sleep(0.5)
    finally:
        try:
            page.remove_listener("download", on_download)
        except Exception:
            pass

    if "url" not in captured:
        return False, "clicking download never produced a file link"

    suggested = captured["filename"] or url.rstrip("/").rsplit("/", 1)[-1]
    dest = out_dir / suggested
    if dest.exists():
        return True, f"already exists, skipped: {dest.name}"

    try:
        size = stream_download(captured["url"], referer=url, dest=dest)
    except Exception as exc:
        return False, f"direct download failed: {exc}"

    return True, f"saved {dest.name} ({size / 1_048_576:.1f} MB)"


def kill_stale_profile_locks(profile_dir: Path):
    """A crashed Chrome instance can leave a zombie process that still holds
    our persistent profile's singleton lock, which then makes the next
    launch fail with 'Opening in existing browser session'. Clear it first."""
    if sys.platform != "win32":
        return
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile_dir.name}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            timeout=10,
            capture_output=True,
        )
    except Exception:
        pass


def launch_browser(p, headless: bool, hide_window: bool, profile_dir: Path):
    kill_stale_profile_locks(profile_dir)
    args = [
        "--disable-blink-features=AutomationControlled",
        "--safebrowsing-disable-download-protection",
        "--disable-client-side-phishing-detection",
    ]
    if hide_window and not headless:
        # Still a real, fully-rendered Chrome (so Cloudflare's check still
        # passes) -- just started off-screen and minimized so it never
        # pops up in front of you.
        args += ["--start-minimized", "--window-position=-32000,-32000"]

    launch_kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        accept_downloads=True,
        args=args,
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1366, "height": 850},
    )
    try:
        # Real installed Chrome is far less likely to get flagged by
        # Cloudflare's bot check than the bundled Chromium build.
        context = p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
    except Exception:
        context = p.chromium.launch_persistent_context(**launch_kwargs)

    page = context.new_page()

    # Ad tabs the site opens can themselves try to spawn further popups;
    # close anything extra (i.e. not our tracked main page) the instant it
    # appears, to limit the blast radius.
    def close_if_not_main(new_page):
        if new_page is page:
            return
        try:
            if not new_page.is_closed():
                new_page.close()
        except Exception:
            pass

    context.on("page", close_if_not_main)
    return context, page


def browser_died(exc: Exception) -> bool:
    text = str(exc).lower()
    return "closed" in text or "crashed" in text or "disconnected" in text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--links", default="links.txt", help="Text file with one link per line")
    parser.add_argument("--out", required=True, help="Folder to save downloaded files into")
    parser.add_argument("--headless", action="store_true", help="Run browser fully headless (more likely to get blocked by Cloudflare)")
    parser.add_argument("--show", action="store_true", help="Show the browser window normally instead of running it minimized/off-screen")
    parser.add_argument("--timeout", type=float, default=90, help="Seconds to wait for the real download link to appear")
    parser.add_argument("--delay", type=float, default=3, help="Seconds to wait between links")
    args = parser.parse_args()

    links_path = Path(args.links)
    if not links_path.exists():
        sys.exit(f"Links file not found: {links_path}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    links = parse_links(links_path)
    if not links:
        sys.exit(f"No links found in {links_path}")

    print(f"Found {len(links)} link(s). Saving to: {out_dir}\n")

    profile_dir = Path(__file__).parent / ".browser-profile"
    log_path = out_dir / "download_log.txt"

    results = []
    with sync_playwright() as p:
        context, page = launch_browser(p, args.headless, not args.show, profile_dir)

        for i, url in enumerate(links, 1):
            print(f"[{i}/{len(links)}] {url}")

            ok, message = False, ""
            for browser_attempt in (1, 2):
                try:
                    ok, message = download_one(page, context, url, out_dir, args.timeout)
                    break
                except Exception as exc:
                    message = f"error: {exc}"
                    if browser_attempt == 1 and browser_died(exc):
                        print("    (browser crashed, likely from a malicious ad - relaunching)")
                        try:
                            context.close()
                        except Exception:
                            pass
                        context, page = launch_browser(p, args.headless, not args.show, profile_dir)
                        continue
                    break

            status = "OK" if ok else "FAIL"
            print(f"    -> {status}: {message}")
            results.append((url, status, message))

            if i < len(links):
                time.sleep(args.delay)

        try:
            context.close()
        except Exception:
            pass

    with log_path.open("w", encoding="utf-8") as f:
        for url, status, message in results:
            f.write(f"{status}\t{url}\t{message}\n")

    succeeded = sum(1 for _, status, _ in results if status == "OK")
    print(f"\nDone: {succeeded}/{len(links)} succeeded. Log written to {log_path}")


if __name__ == "__main__":
    main()
