import json
import os
import subprocess
import sys
import textwrap
import urllib.parse
import urllib.request

GQL_HASH = "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

# Область камеры в кадре 1920x1080: x, y, ширина, высота
FACECAMS = {
    "dangerlyoha": (26, 450, 516, 282),
    "morphe_ya": (2, 449, 483, 327),
    "kopsteep": (26, 392, 522, 293),
}

OUT_W, OUT_H = 1080, 1920
FACE_H = 720      # высота блока с камерой сверху
GAME_Y = 1000     # где начинается игра
GAME_H = 608      # высота игры (16:9 при ширине 1080)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

SLUG = os.environ["SLUG"].strip()
STREAMER = os.environ["STREAMER"].strip().lower()
CHAT_ID = os.environ["CHAT_ID"].strip()
TEXT = os.environ.get("TEXT", "").strip()
BOT_TOKEN = os.environ["BOT_TOKEN"]


def tg_message(text):
    data = json.dumps({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30)


def get_download_url(slug):
    body = json.dumps([{
        "operationName": "VideoAccessToken_Clip",
        "variables": {"slug": slug},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": GQL_HASH}},
    }]).encode()
    req = urllib.request.Request(
        "https://gql.twitch.tv/gql",
        data=body,
        headers={"Client-Id": GQL_CLIENT_ID, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        clip = json.load(r)[0]["data"]["clip"]
    best = max(
        clip["videoQualities"],
        key=lambda q: (float(q["quality"]), q.get("frameRate") or 0),
    )
    tok = clip["playbackAccessToken"]
    value = urllib.parse.quote(tok["value"], safe="")
    return f'{best["sourceURL"]}?sig={tok["signature"]}&token={value}'


def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
    ])
    return float(out)


def layout_text(text):
    room = GAME_Y - FACE_H - 20
    for size in (64, 56, 48, 40):
        width = max(8, int(980 / (size * 0.66)))
        lines = textwrap.wrap(text, width=width)
        lh = int(size * 1.25)
        if len(lines) * lh <= room:
            return size, lh, lines
    width = max(8, int(980 / (40 * 0.66)))
    return 40, 50, textwrap.wrap(text, width=width)[:5]


def build_video():
    x, y, w, h = FACECAMS[STREAMER]
    face_crop = (
        f"crop=iw*{w / 1920:.5f}:ih*{h / 1080:.5f}"
        f":iw*{x / 1920:.5f}:ih*{y / 1080:.5f}"
    )
    parts = [
        "[0:v]split=3[a][b][c]",
        f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        f"gblur=sigma=8,scale={OUT_W}:{OUT_H},eq=brightness=-0.1[bg]",
        f"[b]{face_crop},scale={OUT_W}:{FACE_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{FACE_H}[face]",
        f"[c]scale={OUT_W}:{GAME_H}[game]",
        "[bg][face]overlay=0:0[t1]",
        f"[t1][game]overlay=0:{GAME_Y}[t2]",
    ]
    last = "t2"

    if TEXT:
        size, lh, lines = layout_text(TEXT)
        gap = GAME_Y - FACE_H
        top = FACE_H + (gap - len(lines) * lh) // 2
        for i, line in enumerate(lines):
            name = f"line{i}.txt"
            with open(name, "w", encoding="utf-8") as f:
                f.write(line)
            nxt = f"d{i}"
            parts.append(
                f"[{last}]drawtext=fontfile={FONT}:textfile={name}:expansion=none:"
                f"fontsize={size}:fontcolor=white:borderw=5:bordercolor=black:"
                f"x=(w-text_w)/2:y={top + i * lh}[{nxt}]"
            )
            last = nxt

    parts.append(f"[{last}]format=yuv420p[out]")

    dur = duration("clip.mp4")
    vb = int(min(6000, 47 * 8 * 1000 / max(dur, 1) - 160))
    cmd = [
        "ffmpeg", "-y", "-i", "clip.mp4",
        "-filter_complex", ";".join(parts),
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{vb}k", "-maxrate", f"{vb}k", "-bufsize", f"{2 * vb}k",
        "-c:a", "aac", "-b:a", "128k",
        "-r", "30", "-movflags", "+faststart",
        "out.mp4",
    ]
    subprocess.run(cmd, check=True)


def send_video():
    subprocess.run([
        "curl", "-sS", "--fail-with-body",
        "-F", f"chat_id={CHAT_ID}",
        "-F", "video=@out.mp4",
        "-F", "supports_streaming=true",
        "-F", f"width={OUT_W}",
        "-F", f"height={OUT_H}",
        "-F", f"caption=🎬 {STREAMER}",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
    ], check=True)


def main():
    if STREAMER not in FACECAMS:
        raise ValueError(f"для стримера {STREAMER} ещё не настроена камера")
    download(get_download_url(SLUG), "clip.mp4")
    build_video()
    send_video()


try:
    main()
except subprocess.CalledProcessError as e:
    print("Ошибка программы, код:", e.returncode)
    try:
        tg_message("⚠️ Не получилось собрать или отправить видео (подробности в логах GitHub Actions).")
    except Exception:
        pass
    sys.exit(1)
except Exception as e:
    print("Ошибка:", repr(e))
    try:
        tg_message("⚠️ Не получилось сделать видео: " + str(e)[:300])
    except Exception:
        pass
    sys.exit(1)
