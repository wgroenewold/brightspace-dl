# kaltura-dl

Download a RUG Kaltura video (video.rug.nl / Brightspace) in the highest available quality and turn it into a clean mp4.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

- **yt-dlp** reads your browser cookies and is the HLS fallback.
- **static-ffmpeg** provides `ffmpeg`/`ffprobe`, but only when they aren't already in `PATH`. A system ffmpeg is always preferred. On first use static-ffmpeg downloads its binaries.

## Usage

Log in to https://video.rug.nl in your browser first, then:

```bash
./kaltura-dl.py "https://video.rug.nl/media/t/0_p263lb0e"
./kaltura-dl.py "https://kaf.video.rug.nl/.../entryid/0_ltc6sztb/..." -o lecture3.mp4
./kaltura-dl.py 0_p263lb0e --max-height 1080 --preset fast
```

| Option | Default | |
|---|---|---|
| `-o, --output` | `<title>.mp4` | Output file |
| `--browser` | `firefox` | Browser to take cookies from (`chrome`, `safari`, …) |
| `--mediaspace` | `https://video.rug.nl` | MediaSpace base URL |
| `--crf` / `--preset` | `18` / `medium` | x264 settings, only used when re-encoding |
| `--max-height` | none | Downscale if taller (forces a re-encode) |
| `--keep-source` | off | Keep the original download next to the mp4 |

## How it works

1. Extracts the entry ID (`0_xxxxxxxx`) from a MediaSpace URL, a KAF URL or a bare ID.
2. Dumps browser cookies via yt-dlp and fetches `<mediaspace>/media/t/<entryId>`.
3. Scrapes the page for Kaltura session tokens (KS), the API host (`serviceUrl`, `api.eu.kaltura.com` for RUG) and the partner ID. The partner ID is decoded from the KS if the page doesn't contain it: a v2 KS is base64 of `v2|<pid>|…`.
4. Calls `flavorasset.list`, picks the original upload (or the highest-resolution READY flavor) and downloads it directly via `playManifest/.../flavorId/<id>/format/url`. Each KS is tried until one works.
5. If no flavor can be downloaded directly, falls back to the HLS manifest via yt-dlp.
6. Remuxes or re-encodes to mp4 with `+faststart` and metadata stripped:
   - H.264/HEVC + AAC → stream copy
   - anything else (AV1, VP9, Opus, …) → H.264 (CRF) + AAC 192k

## Limitations

- Only works for videos you can open at video.rug.nl. Content entitled only through a Brightspace course (KAF) may not expose a usable KS on the MediaSpace page.
- KSs expire, so run the script right after logging in if you get 403s.
- DRM-protected entries (Widevine/PlayReady) are not supported.
- Partner ID `239` and the EU API host are specific to RUG; for other institutions use `--mediaspace`. The API host is scraped from the page.
