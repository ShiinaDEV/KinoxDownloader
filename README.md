# VOE Converter

Standalone tool for converting VOE page/embed links to direct media links.
You can also pass a webpage/player URL; the tool will try to scrape a VOE link
from that page first.
It does not import or modify the installed `aniworld` package.

## Examples

Interactive starter:

```powershell
.\start-converter.bat
```

The starter asks for a link, stores settings in `voe_converter_settings.json`,
and lets you configure the download folder, minimum duration, capture logs,
DNS mode, output container, adblock/popup protection, and other browser options.

Convert one link:

```powershell
python .\voe_converter.py "https://voe.sx/e/DEIN_CODE"
```

Or use the Windows wrapper:

```powershell
.\voe-converter.bat "https://voe.sx/e/DEIN_CODE"
```

Scrape a normal page/player URL and convert the VOE link found there:

```powershell
.\voe-converter.bat "https://example.com/page-with-video"
```

Convert many links from a text file:

```powershell
python .\voe_converter.py --file .\links.txt --output .\direct-links.txt
```

Download after converting:

```powershell
python .\voe_converter.py --download "https://voe.sx/e/DEIN_CODE"
```

Downloads are saved to `downloads\` by default.

Choose the output container:

```powershell
.\voe-converter.bat --browser --download --container mp4 "https://deine-testseite/clip"
```

`mkv` is the default and usually the most robust container for copied streams.
`mp4` is useful for better compatibility with phones, TVs, and web players when
the stream uses MP4-compatible video/audio codecs.

Use the visible browser mode for JavaScript pages, player pages, or captcha:

```powershell
.\voe-converter.bat --browser "https://seite-wo-video-laeuft/..."
```

When Chromium opens, use the page normally. If a captcha appears, solve it in
that window. The converter watches the page, frames, and network responses and
continues once it finds a VOE link or direct `.m3u8` media link.
It also scans HTML, JavaScript, and JSON response bodies for hidden VOE/media
links, so the link does not need to be visible in the page source.
It does not click provider buttons automatically unless `--auto-click-voe` is used.

Browser mode with download:

```powershell
.\voe-converter.bat --browser --download "https://seite-wo-video-laeuft/..."
```

In browser download mode, Chromium is used to find the media link and capture
session cookies. After that Chromium closes and `ffmpeg` continues the download
in the terminal with progress.
The terminal output stays quiet while the browser is waiting. Once media is
found, it prints `Download gestartet` once and then prints compact progress
updates, normally in 5 percent steps.
Downloads are named from the page title when possible. The converter checks
`og:title`, `twitter:title`, the first `h1`, and then the browser tab title.
Common page suffixes such as `Stream online anschauen`, `downloaden auf ...`,
and domain names are removed. If no useful title remains, it falls back to the
URL-based filename.

Browser mode ignores broken HTTPS certificates by default, because some player
pages use mismatched certificates. To enforce strict certificate checks:

```powershell
.\voe-converter.bat --browser --strict-https "https://seite-wo-video-laeuft/..."
```

Browser mode uses Cloudflare DNS-over-HTTPS by default. Disable it with:

```powershell
.\voe-converter.bat --browser --no-cloudflare-dns "https://seite-wo-video-laeuft/..."
```

Browser mode also enables popup/ad-domain blocking by default. It blocks common
ad/redirect domains, closes popup tabs, and prevents the main page from
navigating away to VOE when the input is a normal webpage. If a page still
forces that navigation, the browser returns to the original page automatically.
Disable it if your own test page needs a blocked popup or script:

```powershell
.\voe-converter.bat --browser --no-adblock "https://deine-testseite/clip"
```

Force a fresh Chromium profile and test whether Cloudflare DNS-over-HTTPS is active:

```powershell
.\voe-converter.bat --dns-check --reset-browser-profile
```

Enable automatic VOE clicks only for controlled pages, such as your own test site:

```powershell
.\voe-converter.bat --browser --auto-click-voe "https://seite-wo-video-laeuft/..."
```

Write a capture log showing requests and discovered hidden links:

```powershell
.\voe-converter.bat --browser --capture-log capture.txt "https://seite-wo-video-laeuft/..."
```

If only a short preview is downloaded, collect candidates longer and require a
minimum HLS duration:

```powershell
.\voe-converter.bat --browser --download --media-settle-seconds 15 --min-duration 300 --capture-log capture.txt "https://deine-testseite/clip"
```

The converter will inspect all captured `.m3u8` candidates, follow HLS master
playlists to their variants, estimate duration from `#EXTINF`, and choose the
longest usable stream.
For HLS master playlists, higher bandwidth/resolution variants are preferred
among valid candidates.

Disable the progress line:

```powershell
.\voe-converter.bat --browser --download --no-progress "https://deine-testseite/clip"
```

Only scan the page HTML without following possible redirect links:

```powershell
python .\voe_converter.py --no-follow-candidates "https://example.com/page-with-video"
```

Treat inputs as direct VOE links only:

```powershell
python .\voe_converter.py --no-scrape-page "https://voe.sx/e/DEIN_CODE"
```

## Notes

- If VOE or the source page returns a captcha/challenge page, use `--browser`.
- If the website builds its player link only with JavaScript after the page is
  loaded, use `--browser`.
- Browser mode uses `patchright` and opens a real Chromium window.
- Browser mode starts Chromium with Cloudflare DNS-over-HTTPS unless
  `--no-cloudflare-dns` is used.
- Browser mode blocks common ad/redirect domains, neutralizes `window.open`,
  closes popup tabs, and keeps the main page from navigating away to VOE unless
  `--no-adblock` is used.
- Browser mode ignores HTTPS certificate errors unless `--strict-https` is used.
- Browser mode scans request URLs and text-like response bodies.
- Browser mode ranks captured media candidates and prefers the longest HLS
  stream.
- Browser downloads pass captured cookies/Referer to `ffmpeg` and show terminal
  progress after Chromium closes.
- Browser downloads prefer the page title as the filename when available.
- Downloads use `mkv` by default. Use `--container mp4` or menu setting 10 for
  MP4 output.
- Browser mode does not click provider buttons unless `--auto-click-voe` is used.
- `--download` requires `ffmpeg` on PATH.
