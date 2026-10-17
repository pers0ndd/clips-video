import glob
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
IRL_WM_WIDTH = 150          # ширина знака в IRL
IRL_WM_BOTTOM = 40          # отступ знака от нижнего края
IRL_TEXT_SIZE = 50          # размер текста в IRL
IRL_TEXT_GAP = 16           # зазор между текстом и знаком

# --- водяной знак (режим наложения Overlay) ---
WATERMARK = "watermark.png"
WM_WIDTH = 250        # ширина знака в пикселях (всего видео 1080)
WM_OPACITY = 0.50     # сила наложения: 0.0 - не видно, 1.0 - на полную
WM_BOTTOM = 120       # отступ от нижнего края (только для раскладки split)

# --- автоопределение камеры (раскладка "auto") ---
# Эталоны камер лежат в корне репозитория: ref_<ник>__<профиль>.png
# (их делает страничка-калибровка). Бот ищет эталон по всему кадру клипа:
# камера может стоять в другом месте или быть другого размера.
GAME_W = 972           # ширина куска игры в кадре 1920x1080 (пропорция нижней части видео)
MIN_SCORE = 0.60       # минимальное совпадение с эталоном
MIN_BLOCKS = 2         # сколько из 6 частей камеры должно совпасть (защита от ложных срабатываний)
SCALES = [0.6 + 0.05 * i for i in range(21)]   # размеры камеры 60%...160% от эталона

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



def grab_frames(clip, count=7):
    """Несколько кадров клипа (серые, 1920x1080)."""
    import numpy as np
    dur = duration(clip)
    frames = []
    for i in range(count):
        t = dur * (i + 0.5) / count
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", clip, "-frames:v", "1",
             "-vf", "scale=1920:1080", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True).stdout
        if len(raw) == 1920 * 1080:
            frames.append(np.frombuffer(raw, np.uint8).reshape(1080, 1920))
    return frames


def find_camera(frames, ref_path):
    """Ищет эталон по всему кадру. Возвращает (совпадение, x, y, w, h, частей_совпало)."""
    import cv2
    ref = cv2.imread(ref_path, cv2.IMREAD_GRAYSCALE)
    if ref is None:
        return None
    th, tw = ref.shape
    half = [cv2.resize(f, (960, 540), interpolation=cv2.INTER_AREA) for f in frames]

    best = (-1.0, 0, 0, 1.0)
    for s in SCALES:
        w, h = int(round(tw * s / 2)), int(round(th * s / 2))
        if w < 24 or h < 16 or w >= 960 or h >= 540:
            continue
        t = cv2.resize(ref, (w, h), interpolation=cv2.INTER_AREA)
        for f in half:
            _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(f, t, cv2.TM_CCOEFF_NORMED))
            if mx > best[0]:
                best = (mx, loc[0] * 2, loc[1] * 2, s)
    score, cx, cy, s = best
    if score < 0:
        return None

    # уточнение в полном разрешении
    fine = (score, cx, cy, s)
    for ds in (-0.04, -0.02, 0.0, 0.02, 0.04):
        s2 = s + ds
        w, h = int(round(tw * s2)), int(round(th * s2))
        if w >= 1920 or h >= 1080:
            continue
        t = cv2.resize(ref, (w, h), interpolation=cv2.INTER_AREA)
        for f in frames:
            x0, y0 = max(cx - 8, 0), max(cy - 8, 0)
            roi = f[y0:min(cy + h + 8, 1080), x0:min(cx + w + 8, 1920)]
            if roi.shape[0] < h or roi.shape[1] < w:
                continue
            _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED))
            if mx > fine[0] - 1e-9:
                fine = (mx, x0 + loc[0], y0 + loc[1], s2)
    score, x, y, s = fine
    w, h = int(round(tw * s)), int(round(th * s))

    # проверка по частям: камера должна совпасть хотя бы в нескольких из 6 кусков
    t = cv2.resize(ref, (w, h), interpolation=cv2.INTER_AREA)
    blocks = 0
    for j in range(2):
        for i in range(3):
            x0, x1, y0, y1 = i * w // 3, (i + 1) * w // 3, j * h // 2, (j + 1) * h // 2
            tb = t[y0:y1, x0:x1]
            top = 0.0
            for f in frames:
                fb = f[y + y0:y + y1, x + x0:x + x1]
                if fb.shape == tb.shape and tb.std() >= 3 and fb.std() >= 3:
                    top = max(top, float(cv2.matchTemplate(fb, tb, cv2.TM_CCOEFF_NORMED)[0, 0]))
            blocks += top > 0.5
    return score, x, y, w, h, blocks


def game_x_for(x, w):
    if x + w / 2 < 960:
        return int(min(x + w + 10, 1920 - GAME_W))
    return int(max(x - 10 - GAME_W, 0))


def detect_camera(clip, streamer):
    """Ищет камеру стримера по эталонам. Возвращает словарь с результатом."""
    refs = sorted(glob.glob(f"ref_{streamer}__*.png"))
    if not refs:
        return {"found": False, "reason": f"нет эталонов камеры для {streamer}"}
    try:
        import cv2  # noqa: F401
    except Exception:
        return {"found": False, "reason": "OpenCV не установлен"}
    frames = grab_frames(clip)
    if not frames:
        return {"found": False, "reason": "не удалось прочитать кадры клипа"}

    best = None
    for ref in refs:
        res = find_camera(frames, ref)
        if res and (best is None or res[0] > best["score"]):
            name = os.path.basename(ref)[len(f"ref_{streamer}__"):-4]
            best = {"score": res[0], "x": res[1], "y": res[2], "w": res[3], "h": res[4],
                    "blocks": res[5], "profile": name}
    if not best:
        return {"found": False, "reason": "не удалось прочитать эталоны"}
    print(f"лучшее совпадение: {best}", flush=True)
    if best["score"] < MIN_SCORE or best["blocks"] < MIN_BLOCKS:
        return {"found": False, "reason": f"камера не найдена (совпадение {best['score']:.2f})"}
    best["found"] = True
    best["game_x"] = game_x_for(best["x"], best["w"])
    return best


def resolve_plan():
    """Решает, какую раскладку делать. Возвращает (раскладка, камера, game_x, примечание)."""
    if LAYOUT == "irl":
        return "irl", None, None, ""

    if LAYOUT == "auto":
        res = detect_camera("clip.mp4", STREAMER)
        if res["found"]:
            face = (res["x"], res["y"], res["w"], res["h"])
            note = f"авто: камера найдена ({res['profile']}, {res['score']:.2f})"
            print(note, "камера:", face, "game_x:", res["game_x"], flush=True)
            return "split", face, res["game_x"], note
        note = f"авто: {res['reason']}, сделан IRL 4:3"
        print(note, flush=True)
        return "irl", None, None, note

    if LAYOUT != "split":
        raise ValueError(f"неизвестная раскладка: {LAYOUT} (допустимо: split, irl, auto)")

    profiles = STREAMERS.get(STREAMER)
    if not profiles:
        raise ValueError(f"для стримера {STREAMER} ещё не настроена камера")
    cfg = profiles.get(PROFILE)
    if not cfg:
        raise ValueError(
            f"у стримера {STREAMER} нет профиля «{PROFILE}» (есть: {', '.join(profiles)})"
        )
    return "split", cfg["face"], cfg["game_x"], ""


def base_chain(layout, face, gx):
    """Возвращает (части фильтра, имя последней метки, ширина и высота видео)."""
    if layout == "irl":
        parts = [
            # центр кадра в пропорции 4:3, края обрезаются (подходит и для нестандартных клипов)
            f"[0:v]crop='min(iw,ih*4/3)':'min(ih,iw*3/4)',scale={IRL_W}:{IRL_H}[t2]",
        ]
        return parts, "t2", IRL_W, IRL_H

    x, y, w, h = face
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
    layout, face, gx, note = resolve_plan()
    parts, last, ow, oh = base_chain(layout, face, gx)
    irl = layout == "irl"
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
    return ow, oh, note, layout


def send_video(ow, oh, note, layout):
    # Подпись содержит служебные строки «Клип», «Режим», «Текст»: по ним бот
    # понимает, что пересобирать, когда нажимают кнопки под видео. Не меняйте их.
    mode = LAYOUT if LAYOUT in ("auto", "split", "irl") else "split"
    if mode == "split":
        mode += f" · {PROFILE}"
    clean_text = " ".join(TEXT.split())
    cap = f"🎬 {STREAMER}" + (" · IRL" if layout == "irl" else "")
    if note:
        cap += f"\n{note}"
    cap += f"\nКлип: {SLUG}\nРежим: {mode}"
    if clean_text:
        cap += f"\nТекст: {clean_text}"
    cap = cap[:1000]

    # Кнопки под видео: «Другой текст» и «Раскладка» (и «Без текста», если текст был).
    row = [
        {"text": "✏️ Другой текст", "callback_data": "v:t"},
        {"text": "🔁 Раскладка", "callback_data": "v:l"},
    ]
    keyboard = [row]
    if clean_text:
        keyboard.append([{"text": "🚫 Без текста", "callback_data": "v:n"}])

    subprocess.run([
        "curl", "-sS", "--fail-with-body",
        "-F", f"chat_id={CHAT_ID}",
        "-F", "video=@out.mp4",
        "-F", "supports_streaming=true",
        "-F", f"width={ow}",
        "-F", f"height={oh}",
        "--form-string", f"caption={cap}",
        "--form-string", "reply_markup=" + json.dumps({"inline_keyboard": keyboard}),
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
    ], check=True)


def main():
    print(f"make_video.py v5 | раскладка={LAYOUT} | стример={STREAMER} | профиль={PROFILE}", flush=True)
    print("1/4 получаю ссылку на клип", flush=True)
    url = get_download_url(SLUG)
    print("2/4 скачиваю клип", flush=True)
    download(url, "clip.mp4")
    print("3/4 собираю видео", flush=True)
    ow, oh, note, layout = build_video()
    print(f"4/4 отправляю в Telegram ({ow}x{oh})", flush=True)
    send_video(ow, oh, note, layout)
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
