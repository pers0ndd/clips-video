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

# --- раскладка "irl": само видео 4:3 (центр кадра, края обрезаются), 1440x1080 ---
IRL_W, IRL_H = 1440, 1080
IRL_WM_WIDTH = 150          # ширина знака в IRL (кадр шире, поэтому больше, чем WM_WIDTH)
IRL_WM_BOTTOM = 40          # отступ знака от нижнего края
IRL_TEXT_SIZE = 50          # размер текста в IRL (небольшой)
IRL_TEXT_GAP = 16           # зазор между текстом и знаком

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


def layout_text(text, sizes=(64, 56, 48, 40), max_w=980, max_lines=3):
    for size in sizes:
        width = max(8, int(max_w / (size * 0.66)))
        lines = textwrap.wrap(text, width=width)
        if len(lines) <= max_lines:
            return size, int(size * 1.25), lines
    size = sizes[-1]
    width = max(8, int(max_w / (size * 0.66)))
    return size, int(size * 1.25), textwrap.wrap(text, width=width)[:max_lines + 1]


def image_size(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path,
    ]).decode().strip()
    w, h = out.split("x")
    return int(w), int(h)


def base_chain():
    """Возвращает (части фильтра, имя последней метки, ширина и высота видео)."""
    if LAYOUT == "irl":
        parts = [
            # центр кадра в пропорции 4:3, края обрезаются
            f"[0:v]crop=ih*4/3:ih,scale={IRL_W}:{IRL_H}[t2]",
        ]
        return parts, "t2", IRL_W, IRL_H

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
    return parts, "t2", OUT_W, OUT_H


def build_video():
    parts, last, ow, oh = base_chain()
    irl = LAYOUT == "irl"
    use_wm = os.path.exists(WATERMARK)

    # --- размеры и позиция знака ---
    wm_w = IRL_WM_WIDTH if irl else WM_WIDTH
    if use_wm:
        iw, ih = image_size(WATERMARK)
        wm_h = round(wm_w * ih / iw)
    else:
        wm_h = 0
    if irl:
        wm_y = oh - IRL_WM_BOTTOM - wm_h
        text_bottom = (wm_y - IRL_TEXT_GAP) if use_wm else (oh - 60)
    else:
        wm_y = oh - WM_BOTTOM - wm_h

    # --- текст ---
    if TEXT:
        if irl:
            size, lh, lines = layout_text(
                TEXT, sizes=(IRL_TEXT_SIZE, IRL_TEXT_SIZE - 4, IRL_TEXT_SIZE - 8),
                max_w=ow - 160, max_lines=2,
            )
            top = text_bottom - len(lines) * lh
            border = max(3, size // 12)
        else:
            size, lh, lines = layout_text(TEXT)
            top = SPLIT_TEXT_Y - (len(lines) * lh) // 2
            border = 5
        for i, line in enumerate(lines):
            name = f"line{i}.txt"
            with open(name, "w", encoding="utf-8") as f:
                f.write(line)
            nxt = f"d{i}"
            parts.append(
                f"[{last}]drawtext=fontfile={FONT}:textfile={name}:expansion=none:"
                f"fontsize={size}:fontcolor=white:borderw={border}:bordercolor=black:"
                f"x=(w-text_w)/2:y={top + i * lh}[{nxt}]"
            )
            last = nxt

    # --- водяной знак (Overlay) ---
    if use_wm:
        parts += [
            f"[1:v]format=rgba,scale={wm_w}:-1[wm]",
            f"color=c=0x808080:s={ow}x{oh}:r=30[g]",
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
    return ow, oh


def send_video(ow, oh):
    cap = f"🎬 {STREAMER}" + (" · IRL" if LAYOUT == "irl" else "")
    subprocess.run([
        "curl", "-sS", "--fail-with-body",
        "-F", f"chat_id={CHAT_ID}",
        "-F", "video=@out.mp4",
        "-F", "supports_streaming=true",
        "-F", f"width={ow}",
        "-F", f"height={oh}",
        "-F", f"caption={cap}",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
    ], check=True)


def main():
    print(f"make_video.py v3 | раскладка={LAYOUT} | стример={STREAMER} | профиль={PROFILE}", flush=True)
    print("1/4 получаю ссылку на клип", flush=True)
    url = get_download_url(SLUG)
    print("2/4 скачиваю клип", flush=True)
    download(url, "clip.mp4")
    print("3/4 собираю видео", flush=True)
    ow, oh = build_video()
    print(f"4/4 отправляю в Telegram ({ow}x{oh})", flush=True)
    send_video(ow, oh)
    print("\nГотово: Telegram принял видео", flush=True)


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
