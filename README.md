# Downloader Project

Automates downloading files from link pages (e.g. fuckingfast.co) that require
clicking a "Download" button before the file is served. The site blocks plain
HTTP requests (403), so this drives a real browser with Playwright instead.

## Setup (one-time)

```
pip install -r requirements.txt
playwright install chromium
```

## Usage

1. Put your links in `links.txt` (one per line — plain URLs or `-(url)` both work).
2. Run:

```
python download.py --links links.txt --out "C:\path\to\your\folder"
```

A minimized, off-screen Chrome window visits each link, clicks Download, and
the file is streamed straight into the output folder. A `download_log.txt`
is written there listing what succeeded/failed and why.

### Options

- `--show` — show the browser window normally instead of running it
  minimized/off-screen (useful the first run, or to solve a CAPTCHA by hand).
- `--headless` — run fully headless. Not recommended: more likely to get
  blocked by Cloudflare's bot check than the default minimized/off-screen mode.
- `--timeout 90` — seconds to wait for the real download link to appear.
- `--delay 3` — seconds to pause between links.

## Notes

- If a link fails with "no download button found on page", the site's markup
  may differ from what's expected — send an example page's button HTML and
  the selector list in `download.py` (`DOWNLOAD_SELECTORS`) can be extended.
- Files already present in the output folder are skipped, so it's safe to
  re-run after fixing failures.
