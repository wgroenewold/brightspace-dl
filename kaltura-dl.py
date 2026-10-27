#!/usr/bin/env python3
"""
kaltura-dl — download the highest-quality version of a RUG Kaltura video and turn it into a clean mp4.

Accepts:
  - MediaSpace URLs     https://video.rug.nl/media/<title>/0_xxxxxxxx  or  /media/t/0_xxxxxxxx
  - KAF/Brightspace     https://kaf.video.rug.nl/.../entryid/0_xxxxxxxx/...
  - a bare entry ID     0_xxxxxxxx

Requires: python3 and `pip install -r requirements.txt` (yt-dlp, static-ffmpeg).
A system ffmpeg/ffprobe in PATH is used if present; otherwise static-ffmpeg provides them.
You must be logged in to video.rug.nl in your browser.

Examples:
  kaltura-dl.py "https://video.rug.nl/media/t/0_p263lb0e"
  kaltura-dl.py 0_ltc6sztb -o lecture3.mp4 --browser chrome
  kaltura-dl.py <url> --max-height 1080 --preset fast
"""
import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import MozillaCookieJar
from pathlib import Path

MEDIASPACE = "https://video.rug.nl"
DEFAULT_API = "https://api.eu.kaltura.com"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
EID = r"[0-9]_[0-9a-z]{8}"


YTDLP = [sys.executable, "-m", "yt_dlp"]


def log(msg):
    print(f"» {msg}", file=sys.stderr)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- input / auth

def parse_entry_id(s: str) -> str:
    s = urllib.parse.unquote(s.strip())
    m = re.search(rf"entry_?id[/=]({EID})", s, re.I)
    if m:
        return m.group(1)
    last = s.rstrip("/").rsplit("/", 1)[-1]
    if re.fullmatch(EID, last, re.I):
        return last
    hits = re.findall(rf"(?<![0-9a-z_])({EID})(?![0-9a-z])", s, re.I)
    if hits:
        return hits[-1]
    die(f"no entry ID found in: {s}")


def load_cookies(browser: str, url: str, tmpdir: Path) -> MozillaCookieJar:
    """Have yt-dlp dump the browser cookies (it knows how to decrypt Firefox/Chrome keyrings)."""
    path = tmpdir / "cookies.txt"
    subprocess.run(
        [*YTDLP, "--cookies-from-browser", browser, "--cookies", str(path),
         "--skip-download", "--quiet", "--no-warnings", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fails with 'Unsupported URL', but still writes the cookie file
    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        die(f"could not load cookies from {browser} ({e})")
    return jar


def make_opener(jar):
    o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    o.addheaders = [("User-Agent", UA)]
    return o


# ---------------------------------------------------------------- kaltura

def ks_partner(ks: str):
    """A v2 KS is urlsafe base64 of 'v2|<partnerId>|<encrypted blob>'."""
    try:
        raw = base64.urlsafe_b64decode(ks + "=" * (-len(ks) % 4))
        ver, pid, _ = raw.split(b"|", 2)
        if ver == b"v2":
            return int(pid)
    except Exception:
        pass
    return None


def scrape_page(html: str):
    kss = list(dict.fromkeys(re.findall(r'"ks"\s*:\s*"([A-Za-z0-9_\-=]{40,})"', html)))
    m = re.search(r'"serviceUrl"\s*:\s*"(https?:\\?/\\?/[a-z0-9.\-]+)', html, re.I)
    api = m.group(1).replace("\\/", "/") if m else DEFAULT_API
    m = re.search(r'"partnerId"\s*:\s*"?(\d+)', html)
    pid = int(m.group(1)) if m else None
    return kss, api, pid


def api_call(opener, api, service, action, **params):
    params["format"] = 1
    url = f"{api}/api_v3/service/{service}/action/{action}?{urllib.parse.urlencode(params)}"
    with opener.open(url, timeout=30) as r:
        data = json.load(r)
    if isinstance(data, dict) and data.get("objectType") == "KalturaAPIException":
        raise RuntimeError(data.get("message") or data.get("code"))
    return data


def flavor_order(objs):
    ready = [f for f in objs if f.get("status") == 2]  # 2 = READY
    return sorted(
        ready,
        key=lambda f: (bool(f.get("isOriginal")),
                       (f.get("width") or 0) * (f.get("height") or 0),
                       f.get("bitrate") or 0),
        reverse=True,
    )


def manifest_url(api, pid, eid, ks, flavor_id=None):
    base = f"{api}/p/{pid}/sp/{pid}00/playManifest/entryId/{eid}"
    if flavor_id:
        return f"{base}/flavorId/{flavor_id}/format/url/protocol/https/ks/{ks}/a.mp4"
    return f"{base}/format/applehttp/protocol/https/ks/{ks}/a.m3u8"


def download(opener, url, dest: Path):
    with opener.open(url, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 2**20:7.1f} / {total / 2**20:.1f} MiB ({100 * done / total:5.1f}%)",
                      end="", file=sys.stderr)
        print(file=sys.stderr)
    if done < 100_000:
        raise RuntimeError(f"only {done} bytes, probably an error page")


# ---------------------------------------------------------------- ffmpeg

def probe(path: Path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name,pix_fmt,height", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout).get("streams", [])


def to_mp4(src: Path, dst: Path, crf: int, preset: str, max_height):
    streams = probe(src)
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    if not v and not a:
        die("source file contains no audio or video")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y", "-i", str(src)]
    if v:
        cmd += ["-map", "0:v:0"]
    if a:
        cmd += ["-map", "0:a?"]

    desc = []
    if v:
        too_big = max_height and (v.get("height") or 0) > max_height
        copyable = (v["codec_name"] in ("h264", "hevc")
                    and v.get("pix_fmt") in ("yuv420p", "yuvj420p")
                    and not too_big)
        if copyable:
            cmd += ["-c:v", "copy"]
            if v["codec_name"] == "hevc":
                cmd += ["-tag:v", "hvc1"]
            desc.append(f"video {v['codec_name']} copy")
        else:
            cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p"]
            if too_big:
                cmd += ["-vf", f"scale=-2:{max_height}"]
            desc.append(f"video {v['codec_name']} → h264 (crf {crf}, {preset})")
    if a:
        if a["codec_name"] == "aac":
            cmd += ["-c:a", "copy"]
            desc.append("audio aac copy")
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
            desc.append(f"audio {a['codec_name']} → aac")

    cmd += ["-sn", "-dn", "-map_metadata", "-1", "-movflags", "+faststart", str(dst)]
    log(", ".join(desc))
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------- deps

def check_deps():
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        die("yt-dlp not installed — run: pip install -r requirements.txt")
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    try:
        import static_ffmpeg
    except ImportError:
        die("ffmpeg/ffprobe not found — install them, or run: pip install -r requirements.txt")
    log("no system ffmpeg, using static-ffmpeg (downloads binaries on first run)")
    static_ffmpeg.add_paths()
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        die("static-ffmpeg did not provide ffmpeg/ffprobe")


# ---------------------------------------------------------------- main

def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" ._")
    return name[:150] or "video"


def main():
    ap = argparse.ArgumentParser(description="Download a RUG Kaltura video in the highest quality as mp4.")
    ap.add_argument("url", help="MediaSpace/KAF URL or entry ID")
    ap.add_argument("-o", "--output", help="output file (default: <title>.mp4)")
    ap.add_argument("--browser", default="firefox", help="browser to take cookies from (default: firefox)")
    ap.add_argument("--mediaspace", default=MEDIASPACE, help=f"MediaSpace base URL (default: {MEDIASPACE})")
    ap.add_argument("--crf", type=int, default=18, help="x264 CRF when re-encoding (default: 18)")
    ap.add_argument("--preset", default="medium", help="x264 preset when re-encoding (default: medium)")
    ap.add_argument("--max-height", type=int, help="downscale if taller than this (e.g. 1080)")
    ap.add_argument("--keep-source", action="store_true", help="keep the original source file")
    args = ap.parse_args()

    check_deps()

    eid = parse_entry_id(args.url)
    page_url = f"{args.mediaspace.rstrip('/')}/media/t/{eid}"
    log(f"entry {eid}")

    with tempfile.TemporaryDirectory(prefix="kaltura-dl-") as td:
        tmp = Path(td)
        jar = load_cookies(args.browser, page_url, tmp)
        opener = make_opener(jar)

        with opener.open(page_url, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
        kss, api, pid = scrape_page(html)
        if not kss:
            die(f"no KS found in the page — are you logged in to {args.mediaspace} in {args.browser}, "
                "and do you have access to this video?")
        pid = pid or next((p for p in map(ks_partner, kss) if p), None)
        if not pid:
            die("could not determine partner ID")
        log(f"partner {pid}, api {api}, {len(kss)} KS(s)")

        # fetch flavors + title with the first KS that is allowed to
        flavors, title, ks_ok = [], None, None
        for ks in kss:
            try:
                res = api_call(opener, api, "flavorasset", "list", ks=ks, **{"filter:entryIdEqual": eid})
                objs = flavor_order(res.get("objects") or [])
                if objs:
                    flavors, ks_ok = objs, ks
                    break
            except Exception:
                continue
        for ks in ([ks_ok] if ks_ok else []) + kss:
            try:
                title = api_call(opener, api, "baseentry", "get", ks=ks, entryId=eid).get("name")
                if title:
                    break
            except Exception:
                continue

        out = Path(args.output) if args.output else Path(safe_name(title or eid) + ".mp4")
        if title:
            log(f"title: {title}")

        src = None
        # 1) direct download of the best flavor (usually the original upload)
        for f in flavors:
            label = f"{f.get('width')}x{f.get('height')} {f.get('bitrate')}kb/s" + (" [source]" if f.get("isOriginal") else "")
            ext = (f.get("fileExt") or "bin").lower()
            dest = tmp / f"src.{ext}"
            for ks in [ks_ok] + [k for k in kss if k != ks_ok]:
                log(f"downloading flavor {f['id']} ({label})")
                try:
                    download(opener, manifest_url(api, pid, eid, ks, f["id"]), dest)
                    src = dest
                    break
                except (urllib.error.HTTPError, RuntimeError) as e:
                    log(f"  failed: {e}")
            if src:
                break

        # 2) fallback: HLS via yt-dlp (best variant in the ladder)
        if not src:
            log("no direct flavor available, falling back to HLS")
            for ks in kss:
                r = subprocess.run(
                    [*YTDLP, "--quiet", "--progress", "--no-warnings", "-f", "bv*+ba/b",
                     "-o", str(tmp / "src.%(ext)s"), manifest_url(api, pid, eid, ks)])
                found = [p for p in tmp.glob("src.*") if p.suffix not in (".part", ".ytdl")]
                if r.returncode == 0 and found:
                    src = found[0]
                    break
        if not src:
            die("download failed with every KS")

        if args.keep_source:
            kept = out.with_name(out.stem + ".source" + src.suffix)
            shutil.copy2(src, kept)
            log(f"source kept: {kept}")

        to_mp4(src, out, args.crf, args.preset, args.max_height)

    log(f"done: {out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except subprocess.CalledProcessError as e:
        die(f"{e.cmd[0]} failed (exit {e.returncode})")
