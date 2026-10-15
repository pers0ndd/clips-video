import json
import os
import subprocess
import sys
import textwrap
import urllib.parse
import urllib.request

GQL_HASH = "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

# ---------------------------------------------------------------------------
# ПРОФИЛИ КАМЕР (нужны для раскладки "split": камера сверху, игра снизу).
# У каждого стримера может быть несколько профилей: "default" и любые другие
# (например "corner"). Профиль выбирается при запуске.
# Новые строки профилей выдаёт страничка-калибровка: просто вставьте строку
# внутрь нужного стримера.
#   face   = (x, y, ширина, высота) камеры в кадре 1920x1080
#   game_x = левый край куска игры (рядом с камерой, камеру не захватывает)
# ---------------------------------------------------------------------------
STREAMERS = {
    "dangerlyoha": {
        "default": {"face": (26, 450, 516, 282), "game_x": 556},
    },
    "morphe_ya": {
        "default": {"face": (2, 449, 483, 327), "game_x": 492},
    },
    "kopsteep": {
        "default": {"face": (26, 392, 522, 293), "game_x": 556},
    },
}

OUT_W, OUT_H = 1080, 1920
FONT = "font.ttf"

# --- раскладка "split": камера сверху, игра снизу ---
FACE_H = 720                # высота камеры сверху (960 = ровно пополам)
GAME_H = OUT_H - FACE_H     # высота игры снизу
SPLIT_TEXT_Y = FACE_H       # центр текста (на стыке камеры и игры)

# --- раскладка "irl": центр кадра 4:3 на размытом фоне ---
IRL_H = OUT_W * 3 // 4                # высота видео 4:3 при ширине 1080 = 810
IRL_Y = (OUT_H - IRL_H) // 2          # отступ сверху (555)
IRL_TEXT_Y = IRL_Y // 2               # текст: по центру верхней размытой полосы
IRL_WM_Y = IRL_Y + IRL_H + 40         # знак: сразу под видео (расстояние от верха)

# --- водяной знак (режим наложения Overlay) ---
WATERMARK = "watermark.png"
WM_WIDTH = 250        # ширина знака в пикселях (всего видео 1080)
WM_OPACITY = 0.50     # сила наложения: 0.0 - не видно, 1.0 - на полную
WM_BOTTOM = 120       # отступ от нижнего края (только для раскладки split)

SLUG = os.environ["SLUG"].strip()
STREAMER = os.environ["STREAMER"].strip().lower()
CHAT_ID = os.environ["CHAT_ID"].strip()
TEXT = os.environ.get("TEXT", "").strip()
LAYOUT = (os.environ.get("LAYOUT", "") or "split").strip().lower()
PROFILE = (os.environ.get("PROFILE", "") or "default").strip().lower()
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
        resp = json.load(r)[0]
    clip = (resp.get("data") or {}).get("clip")
    if not clip:
        errors = resp.get("errors")
        if errors:
            raise ValueError("Twitch изменил внутренний запрос, нужно обновить код: " + str(errors)[:150])
        raise ValueError("клип с таким slug не найден, проверь slug")
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
    for size in (64, 56, 48, 40):
        width = max(8, int(980 / (size * 0.66)))
        lines = textwrap.wrap(text, width=width)
        if len(lines) <= 3:
            return size, int(size * 1.25), lines
    width = max(8, int(980 / (40 * 0.66)))
    return 40, 50, textwrap.wrap(text, width=width)[:4]


def base_chain():
    """Возвращает (части фильтра, имя последней метки, y центра текста, y знака)."""
    if LAYOUT == "irl":
        parts = [
            "[0:v]split=2[a][b]",
            # размытый фон: центральная вертикальная полоса кадра, увеличенная и размытая
            f"[a]crop=ih*0.5625:ih,scale=270:480,gblur=sigma=8,"
            f"scale={OUT_W}:{OUT_H},eq=brightness=-0.1[bg]",
            # передний план: центр кадра в пропорции 4:3 (края обрезаются)
            f"[b]crop=ih*4/3:ih,scale={OUT_W}:{IRL_H}[fg]",
            f"[bg][fg]overlay=0:{IRL_Y}[t2]",
        ]
        return parts, "t2", IRL_TEXT_Y, str(IRL_WM_Y)

    if LAYOUT != "split":
        raise ValueError(f"неизвестная раскладка: {LAYOUT} (допустимо: split, irl)")

    profiles = STREAMERS.get(STREAMER)
    if not profiles:
        raise ValueError(f"для стримера {STREAMER} ещё не настроена камера")
    cfg = profiles.get(PROFILE)
    if not cfg:
        raise ValueError(
            f"у стримера {STREAMER} нет профиля «{PROFILE}» (есть: {', '.join(profiles)})"
        )

    x, y, w, h = cfg["face"]
    gx = cfg["game_x"]
    face_crop = (
        f"crop=iw*{w / 1920:.5f}:ih*{h / 1080:.5f}"
        f":iw*{x / 1920:.5f}:ih*{y / 1080:.5f}"
    )
    game_crop = f"crop=ih*{OUT_W / GAME_H:.5f}:ih:iw*{gx / 1920:.5f}:0"
    parts = [
        "[0:v]split=2[a][b]",
        f"[a]{face_crop},scale={OUT_W}:{FACE_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{FACE_H}[face]",
        f"[b]{game_crop},scale={OUT_W}:{GAME_H}[game]",
        "[face][game]vstack[t2]",
    ]
    return parts, "t2", SPLIT_TEXT_Y, f"main_h-overlay_h-{WM_BOTTOM}"


def build_video():
    parts, last, text_y, wm_y = base_chain()

    if TEXT:
        size, lh, lines = layout_text(TEXT)
        top = text_y - (len(lines) * lh) // 2
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

    use_wm = os.path.exists(WATERMARK)
    if use_wm:
        parts += [
            f"[1:v]format=rgba,scale={WM_WIDTH}:-1[wm]",
            f"color=c=0x808080:s={OUT_W}x{OUT_H}:r=30[g]",
            f"[g][wm]overlay=(main_w-overlay_w)/2:{wm_y}:format=auto[wmc]",
            f"[{last}][wmc]blend=all_mode=overlay:all_opacity={WM_OPACITY}:shortest=1[wmo]",
        ]
        last = "wmo"

    parts.append(f"[{last}]format=yuv420p[out]")

    dur = duration("clip.mp4")
    vb = int(min(6000, 47 * 8 * 1000 / max(dur, 1) - 160))

    cmd = ["ffmpeg", "-y", "-i", "clip.mp4"]
    if use_wm:
        cmd += ["-loop", "1", "-framerate", "30", "-i", WATERMARK]
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[out]", "-map", "0:a?",
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{vb}k", "-maxrate", f"{vb}k", "-bufsize", f"{2 * vb}k",
        "-c:a", "aac", "-b:a", "128k",
        "-r", "30", "-movflags", "+faststart",
        "out.mp4",
    ]
    subprocess.run(cmd, check=True)


def send_video():
    cap = f"🎬 {STREAMER}" + (" · IRL" if LAYOUT == "irl" else "")
    subprocess.run([
        "curl", "-sS", "--fail-with-body",
        "-F", f"chat_id={CHAT_ID}",
        "-F", "video=@out.mp4",
        "-F", "supports_streaming=true",
        "-F", f"width={OUT_W}",
        "-F", f"height={OUT_H}",
        "-F", f"caption={cap}",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
    ], check=True)


def main():
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
