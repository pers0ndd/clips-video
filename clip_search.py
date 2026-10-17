"""Поиск клипов Twitch: стример, категория, слова из названия, период по датам.

Запуск: двойной щелчок по clip_search.exe (или: python clip_search.py).
Ключи Twitch (Client ID и Client Secret) программа спросит при первом запуске
и сохранит только на этом компьютере. Окно рисует Edge/Chrome в режиме приложения.
"""
import csv
import json
import math
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

VERSION = "2.1"

# Адреса можно подменить переменными среды (так программа проверяется без интернета).
API_BASE = os.environ.get("CLIPS_API_BASE", "https://api.twitch.tv/helix")
TOKEN_URL = os.environ.get("CLIPS_TOKEN_URL", "https://id.twitch.tv/oauth2/token")
GQL_URL = os.environ.get("CLIPS_GQL_URL", "https://gql.twitch.tv/gql")
GQL_HASH = "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

MAX_RESULTS = 2000          # больше этого в таблицу не складываем
TWITCH_CLIPS_START = datetime(2016, 5, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- настройки

def config_path():
    override = os.environ.get("CLIPS_CONFIG")
    if override:
        return override
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ClipSearch", "config.json")


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(data):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- период

def plural(n, one, few, many):
    a = abs(n) % 100
    b = a % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    if b == 1:
        return one
    return many


def period_label(minutes):
    """«за 24 часа», «за 2 недели», «за всё время»."""
    if minutes == 0:
        return "за всё время"
    table = [
        (43200, "месяц", "месяца", "месяцев"),
        (10080, "неделю", "недели", "недель"),
        (1440, "день", "дня", "дней"),
        (60, "час", "часа", "часов"),
        (1, "минуту", "минуты", "минут"),
    ]
    if minutes % 43200 == 0:
        row = table[0]
    elif minutes % 10080 == 0:
        row = table[1]
    elif minutes % 1440 == 0 and minutes > 2880:
        row = table[2]
    elif minutes % 60 == 0:
        row = table[3]
    else:
        row = table[4]
    n = minutes // row[0]
    return f"за {n} {plural(n, row[1], row[2], row[3])}"


def parse_period(raw):
    """«2 часа», «3 дня», «2 недели», «45 минут», «1,5 часа», «всё время» -> минуты.
    0 означает «всё время». Если не понял, вернёт None."""
    t = str(raw).lower().replace(",", ".").strip()
    if not t:
        return None
    if re.fullmatch(r"(всё|все|all)(\s*время)?", t):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-zа-яё]+)", t)
    if not m:
        return None
    n = float(m.group(1))
    u = m.group(2)
    if re.match(r"(мес|mo)", u):
        mult = 43200
    elif re.match(r"(мин|min|м$|m$)", u):
        mult = 1
    elif re.match(r"(ч|h)", u):
        mult = 60
    elif re.match(r"(д|d)", u):
        mult = 1440
    elif re.match(r"(н|w)", u):
        mult = 10080
    else:
        return None
    minutes = round(n * mult)
    if minutes < 1 or minutes > 3650 * 1440:
        return None
    return minutes


# ---------------------------------------------------------------- Twitch

class Cancelled(Exception):
    pass


class TwitchError(Exception):
    pass


def rfc3339(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Twitch:
    def __init__(self, client_id, client_secret):
        self.cid = client_id.strip()
        self.secret = client_secret.strip()
        self.token = None

    def _login(self):
        data = urllib.parse.urlencode(
            {"client_id": self.cid, "client_secret": self.secret, "grant_type": "client_credentials"}
        ).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as r:
                self.token = json.load(r)["access_token"]
        except urllib.error.HTTPError:
            raise TwitchError("Не удалось войти в Twitch: проверьте Client ID и Client Secret (кнопка «Ключи Twitch»).")
        except urllib.error.URLError as e:
            raise TwitchError(f"Нет связи с Twitch: {e.reason}")
        except Exception:
            raise TwitchError("Не удалось войти в Twitch: неожиданный ответ.")

    def get(self, path, params, cancel=None):
        url = API_BASE + path + "?" + urllib.parse.urlencode(params, doseq=True)
        last = None
        for attempt in range(4):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if not self.token:
                self._login()
            req = urllib.request.Request(url, headers={"Client-Id": self.cid, "Authorization": "Bearer " + self.token})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self.token = None
                    continue
                if e.code == 429:
                    time.sleep(2 + attempt * 2)
                    continue
                try:
                    msg = json.load(e).get("message", "")
                except Exception:
                    msg = ""
                raise TwitchError(f"Twitch ответил {e.code}. {msg}".strip())
            except urllib.error.URLError as e:
                last = e
                time.sleep(1 + attempt)
        raise TwitchError(f"Нет связи с Twitch: {getattr(last, 'reason', last)}")

    def user(self, login):
        d = self.get("/users", {"login": login}).get("data") or []
        if not d:
            raise TwitchError(f"Стример «{login}» не найден.")
        return d[0]

    def categories(self, query, cancel=None):
        d = self.get("/search/categories", {"query": query, "first": 25}, cancel).get("data") or []
        return [{"id": c["id"], "name": c["name"],
                 "art": str(c.get("box_art_url", "")).replace("{width}", "52").replace("{height}", "72")} for c in d]

    def games(self, ids, cancel=None):
        ids = sorted({i for i in ids if i})
        out = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            for g in self.get("/games", {"id": chunk}, cancel).get("data") or []:
                out[g["id"]] = g["name"]
        return out


def split_words(text):
    include, exclude = [], []
    for w in text.lower().split():
        if w.startswith("-") and len(w) > 1:
            exclude.append(w[1:])
        else:
            include.append(w)
    return include, exclude


def make_windows(start, end, deep):
    """Список окон (start, end), новые сверху. Без глубокого поиска окно одно."""
    if not deep:
        return [(start, end)]
    span_days = max(1, math.ceil((end - start).total_seconds() / 86400))
    step = max(7, math.ceil(span_days / 600))
    windows = []
    cur = end
    while cur > start:
        lo = max(start, cur - timedelta(days=step))
        windows.append((lo, cur))
        cur = lo
    return windows


def search_clips(tw, login="", game_id="", words="", start=None, end=None, creator="", min_views=0,
                 max_pages=10, deep=False, progress=None, cancel=None):
    """Ищет клипы за период start..end (aware datetime; None = без границы).
    Возвращает (список клипов, сведения)."""
    if not login and not game_id:
        raise ValueError("Укажите стримера или категорию (можно и то и другое).")
    include, exclude = split_words(words)
    creator = creator.strip().lower()
    base = {"first": 100}
    created = None
    if login:
        u = tw.user(login)
        base["broadcaster_id"] = u["id"]
        try:
            created = parse_time(u["created_at"][:19] + "Z")
        except Exception:
            created = None
    # Twitch не принимает broadcaster_id и game_id в одном запросе (ошибка 400),
    # поэтому при стримере ищем по нему, а категорию отбираем сами.
    if game_id and not login:
        base["game_id"] = game_id

    now = datetime.now(timezone.utc)
    if end is not None and end > now:
        end = now
    if start is not None and end is not None and start >= end:
        raise ValueError("Дата «с» должна быть раньше даты «по».")
    dated = start is not None or end is not None or deep
    if dated:
        if end is None:
            end = now
        if start is None:
            start = max(created, TWITCH_CLIPS_START) if created else TWITCH_CLIPS_START
        windows = make_windows(start, end, deep)
    else:
        windows = [(None, None)]

    found, seen = [], set()
    scanned = 0
    truncated = False
    for wi, (lo, hi) in enumerate(windows):
        cursor = None
        for page in range(max_pages):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            params = dict(base)
            if dated:
                params["started_at"] = rfc3339(lo)
                params["ended_at"] = rfc3339(hi)
            if cursor:
                params["after"] = cursor
            r = tw.get("/clips", params, cancel)
            data = r.get("data") or []
            scanned += len(data)
            for c in data:
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                title = str(c.get("title", "")).lower()
                if include and not all(w in title for w in include):
                    continue
                if exclude and any(w in title for w in exclude):
                    continue
                if creator and creator not in str(c.get("creator_name", "")).lower():
                    continue
                if c.get("view_count", 0) < min_views:
                    continue
                if login and game_id and c.get("game_id") != game_id:
                    continue
                found.append(c)
            cursor = (r.get("pagination") or {}).get("cursor")
            if progress:
                tail = f" (период {wi + 1} из {len(windows)})" if deep and len(windows) > 1 else ""
                progress(f"Просмотрено клипов: {scanned}, найдено: {len(found)}{tail}")
            if len(found) >= MAX_RESULTS or not cursor:
                break
        else:
            if cursor and not deep:
                truncated = True
        if len(found) >= MAX_RESULTS:
            truncated = True
            break

    found.sort(key=lambda c: c.get("view_count", 0), reverse=True)
    games = tw.games([c.get("game_id") for c in found], cancel)
    rows = []
    for c in found:
        try:
            created_at = parse_time(c["created_at"])
        except Exception:
            created_at = now
        rows.append({
            "id": c["id"],
            "url": c.get("url") or f"https://clips.twitch.tv/{c['id']}",
            "title": str(c.get("title", "")).strip() or "без названия",
            "creator": c.get("creator_name", ""),
            "broadcaster": c.get("broadcaster_name", ""),
            "game": games.get(c.get("game_id"), ""),
            "created": created_at,
            "views": int(c.get("view_count", 0)),
            "duration": float(c.get("duration", 0)),
            "thumb": c.get("thumbnail_url", ""),
        })
    return rows, {"scanned": scanned, "truncated": truncated}


# ---------------------------------------------------------------- скачивание

def get_download_url(slug):
    body = json.dumps([{
        "operationName": "VideoAccessToken_Clip",
        "variables": {"slug": slug},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": GQL_HASH}},
    }]).encode()
    req = urllib.request.Request(GQL_URL, data=body, headers={"Client-Id": GQL_CLIENT_ID, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            clip = json.load(r)[0]["data"]["clip"]
        best = sorted(clip["videoQualities"], key=lambda q: (-float(q["quality"]), -(q.get("frameRate") or 0)))[0]
        tok = clip["playbackAccessToken"]
        url = f'{best["sourceURL"]}?sig={tok["signature"]}&token={urllib.parse.quote(tok["value"], safe="")}'
        return url, best["quality"]
    except Exception:
        raise TwitchError(f"Не удалось получить ссылку на скачивание клипа {slug}.")


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:80] or "clip"


def download_clip(row, folder, progress=None, cancel=None):
    url, quality = get_download_url(row["id"])
    name = f'{safe_name(row["broadcaster"] or "clip")} - {safe_name(row["title"])} - {row["id"]}.mp4'
    path = os.path.join(folder, name)
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done * 100 // total)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------- вывод

def ago(dt):
    mins = max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 60))
    if mins < 60:
        return f"{mins} мин назад"
    h = round(mins / 60)
    if h < 48:
        return f"{h} ч назад"
    d = round(h / 24)
    if d < 60:
        return f"{d} дн назад"
    return f"{round(d / 30)} мес назад"


def local_text(dt):
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def export_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Название", "Автор клипа", "Стример", "Игра", "Дата", "Просмотры", "Секунд", "Ссылка"])
        for r in rows:
            w.writerow([r["title"], r["creator"], r["broadcaster"], r["game"], local_text(r["created"]),
                        r["views"], round(r["duration"]), r["url"]])


PRESETS = ["1 час", "24 часа", "7 дней", "30 дней", "Всё время"]
COLUMNS = [
    ("n", "№", 45, "e"),
    ("title", "Название", 360, "w"),
    ("creator", "Автор клипа", 130, "w"),
    ("game", "Игра", 150, "w"),
    ("date", "Дата", 190, "w"),
    ("views", "Просмотры", 90, "e"),
    ("dur", "Секунд", 70, "e"),
]




# ---------------------------------------------------------------- мини-сервер и окно

import base64
import secrets
import socketserver
import subprocess
import http.server

UI_HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Поиск клипов</title>
<link rel="icon" type="image/png" href="/icon.png">
<style>
:root {
  --sky-0: #070b1f;
  --sky-1: #0c1538;
  --sky-2: #16245a;
  --panel: rgba(17, 26, 62, 0.58);
  --panel-solid: #121b44;
  --panel-hi: rgba(30, 44, 98, 0.7);
  --line: rgba(150, 172, 255, 0.15);
  --line-strong: rgba(150, 172, 255, 0.28);
  --text: #e9edff;
  --muted: #95a0d0;
  --faint: #626ea3;
  --accent: #5b7cff;
  --accent-2: #7a63ff;
  --glow: 0 0 26px rgba(80, 110, 255, 0.5);
  --ok: #3ddc97;
  --warn: #ffb454;
  --bad: #ff6b7e;
  --radius-lg: 24px;
  --radius-md: 16px;
  --radius-sm: 11px;
  --font: "Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { height: 100%; margin: 0; }
body {
  font-family: var(--font);
  color: var(--text);
  font-size: 14px;
  line-height: 1.45;
  background:
    radial-gradient(900px 420px at 12% 105%, rgba(255, 150, 90, 0.26), transparent 62%),
    radial-gradient(1000px 520px at 85% -5%, rgba(90, 120, 255, 0.3), transparent 62%),
    linear-gradient(180deg, var(--sky-0) 0%, var(--sky-1) 55%, #121e4e 100%);
  overflow: hidden;
}
button, input, select { font: inherit; color: inherit; }
button { cursor: pointer; }
:focus-visible { outline: 2px solid #9db0ff; outline-offset: 2px; }
svg.i { width: 1em; height: 1em; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; flex: none; }

/* ---------- фон: звёзды и горы ---------- */
#stars { position: fixed; inset: 0; pointer-events: none; }
#hills { position: fixed; left: 0; right: 0; bottom: 0; width: 100%; height: 24vh; pointer-events: none; }

/* ---------- каркас ---------- */
.shell {
  position: relative;
  display: grid;
  grid-template-columns: 224px minmax(0, 1fr);
  height: 100vh;
  background: rgba(10, 16, 46, 0.55);
  box-shadow: inset 0 0 0 1px rgba(150, 172, 255, 0.22);
  overflow: hidden;
}
.side {
  display: flex; flex-direction: column;
  padding: 26px 18px 20px;
  background: rgba(6, 10, 30, 0.5);
  border-right: 1px solid var(--line);
}
.brand { display: flex; align-items: center; gap: 11px; padding: 0 6px 30px; }
.brand img { width: 34px; height: 34px; }
.brand b { font-size: 19px; font-weight: 600; letter-spacing: 0.01em; }
.brand span { font-weight: 300; color: var(--muted); }
.nav { display: grid; gap: 10px; }
.nav button {
  display: flex; align-items: center; gap: 12px;
  background: none; border: 0; padding: 6px 8px 6px 6px; border-radius: 40px;
  color: var(--muted); text-align: left; font-size: 14px;
  transition: color .15s, background .15s;
}
.nav button .ic {
  width: 38px; height: 38px; border-radius: 50%;
  display: grid; place-items: center; font-size: 17px;
  background: rgba(16, 24, 60, 0.9); border: 1px solid var(--line);
  transition: background .15s, box-shadow .15s;
}
.nav button:hover { color: var(--text); }
.nav button.on { color: var(--text); }
.nav button.on .ic {
  background: radial-gradient(circle at 35% 30%, #8aa2ff, #4262f0 70%);
  border-color: transparent; box-shadow: var(--glow);
}
.nav .count { margin-left: auto; font-size: 12px; background: var(--accent); color: #fff; padding: 1px 8px; border-radius: 10px; }
.side .spacer { flex: 1; }
.side .foot { font-size: 12px; color: var(--faint); padding: 14px 8px 0; }

.main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.top {
  display: flex; align-items: center; gap: 16px;
  padding: 22px 30px 0;
}
.top { -webkit-app-region: drag; user-select: none; }
.top button, .top .pill, .wc, .wc button { -webkit-app-region: no-drag; }
.brand { -webkit-app-region: drag; user-select: none; }
.wc { position: fixed; top: 0; right: 0; z-index: 70; display: flex; }
.wc button { width: 48px; height: 34px; background: none; border: 0; border-radius: 0; color: var(--muted); display: grid; place-items: center; font-size: 15px; }
.wc button:hover { background: rgba(150, 172, 255, 0.16); color: #fff; }
.wc button.x:hover { background: #e5384f; color: #fff; }
body.has-wc .top { padding-right: 170px; }
.crumbs { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 13px; }
.crumbs svg { font-size: 15px; }
.crumbs .sep { color: var(--faint); }
.crumbs .here { color: var(--text); }
.top .grow { flex: 1; }
.pill {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 14px; border-radius: 30px;
  background: rgba(10, 16, 44, 0.75); border: 1px solid var(--line);
  color: var(--muted); font-size: 13px;
}
.pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); }
.pill.ok .dot { background: var(--ok); box-shadow: 0 0 10px var(--ok); }
.pill.bad .dot { background: var(--bad); }
.pill button { background: none; border: 0; padding: 0; color: #a9b9ff; text-decoration: underline; text-underline-offset: 3px; }
.round-btn {
  width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center;
  background: rgba(10, 16, 44, 0.75); border: 1px solid var(--line); color: var(--muted); font-size: 17px;
}
.round-btn:hover { color: var(--text); border-color: var(--line-strong); }

.scroller { flex: 1; min-height: 0; overflow-y: auto; padding: 8px 30px 30px; scrollbar-width: thin; scrollbar-color: #2b3a7a transparent; }
.scroller::-webkit-scrollbar { width: 10px; }
.scroller::-webkit-scrollbar-thumb { background: #2b3a7a; border-radius: 10px; border: 2px solid transparent; background-clip: content-box; }

h1 { margin: 22px 0 6px; font-size: 38px; font-weight: 300; letter-spacing: -0.01em; line-height: 1.15; }
h1 b { font-weight: 500; background: linear-gradient(90deg, #7f9bff, #a38bff); -webkit-background-clip: text; background-clip: text; color: transparent; }
.lede { margin: 0 0 22px; color: var(--muted); max-width: 62ch; }

/* ---------- карточки ---------- */
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: 20px;
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
}
.card h3 { margin: 0 0 14px; font-size: 16px; font-weight: 500; display: flex; align-items: center; gap: 10px; }
.card h3 .grow { flex: 1; }

/* ---------- фильтры ---------- */
.filters { display: grid; gap: 16px; position: relative; z-index: 20; }
.row { display: flex; gap: 12px; flex-wrap: wrap; align-items: stretch; }
.field {
  position: relative; flex: 1 1 200px; min-width: 0;
  display: flex; align-items: center; gap: 10px;
  padding: 0 14px; height: 46px;
  background: rgba(8, 13, 38, 0.7); border: 1px solid var(--line); border-radius: 30px;
  color: var(--muted); transition: border-color .15s, box-shadow .15s;
}
.field:focus-within { border-color: #7d93ff; box-shadow: 0 0 0 3px rgba(91, 124, 255, 0.18); color: #c9d3ff; }
.field input { flex: 1; min-width: 0; background: none; border: 0; outline: 0; color: var(--text); height: 100%; }
.field input::placeholder { color: var(--faint); }
.field.wide { flex: 2 1 320px; }
.field.small { flex: 0 1 190px; }
.field .x { background: none; border: 0; color: var(--faint); display: none; padding: 2px; font-size: 14px; }
.field.has .x { display: grid; }

.sug {
  position: absolute; left: 0; right: 0; top: calc(100% + 8px); z-index: 30;
  background: #101a45; border: 1px solid var(--line-strong); border-radius: var(--radius-md);
  padding: 6px; max-height: 300px; overflow: auto; box-shadow: 0 20px 50px rgba(0, 0, 0, 0.55);
}
.sug button {
  display: flex; align-items: center; gap: 12px; width: 100%; text-align: left;
  background: none; border: 0; padding: 7px 10px; border-radius: 10px; color: var(--text);
}
.sug button:hover, .sug button.act { background: rgba(91, 124, 255, 0.2); }
.sug img { width: 28px; height: 38px; border-radius: 5px; object-fit: cover; background: #1b2757; }
.sug .none { color: var(--muted); padding: 10px; }

.seg { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.chip {
  height: 38px; padding: 0 16px; border-radius: 30px;
  background: rgba(8, 13, 38, 0.7); border: 1px solid var(--line); color: var(--muted);
  display: inline-flex; align-items: center; gap: 8px; transition: all .15s;
}
.chip:hover { color: var(--text); border-color: var(--line-strong); }
.chip.on { color: #fff; background: linear-gradient(180deg, #5a7bff, #3f5ee8); border-color: transparent; box-shadow: 0 0 20px rgba(80, 110, 255, 0.4); }
.seg .lbl { color: var(--muted); margin-right: 4px; }

.switch { display: inline-flex; align-items: center; gap: 10px; color: var(--muted); background: none; border: 0; padding: 0; height: 38px; }
.switch .tr { width: 40px; height: 22px; border-radius: 20px; background: rgba(8, 13, 38, 0.9); border: 1px solid var(--line-strong); position: relative; transition: background .15s; }
.switch .tr::after { content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #8d99cf; transition: all .15s; }
.switch[aria-checked="true"] .tr { background: #4262f0; border-color: transparent; }
.switch[aria-checked="true"] .tr::after { left: 20px; background: #fff; }
.switch[aria-checked="true"] { color: var(--text); }

.go { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.btn {
  height: 46px; padding: 0 22px; border-radius: 30px; border: 1px solid var(--line-strong);
  background: rgba(10, 16, 44, 0.8); color: var(--text);
  display: inline-flex; align-items: center; justify-content: center; gap: 9px; transition: all .15s;
}
.btn:hover { border-color: #8aa0ff; }
.btn:disabled { opacity: 0.45; cursor: default; }
.btn.primary {
  background: linear-gradient(180deg, #5f80ff, #3b59e6); border-color: transparent;
  box-shadow: 0 0 28px rgba(80, 110, 255, 0.45); padding: 0 30px; font-weight: 500;
}
.btn.primary:hover:not(:disabled) { filter: brightness(1.1); }
.btn.slim { height: 38px; padding: 0 16px; font-size: 13px; }
.btn.danger { color: #ffb3bd; }
.hint { color: var(--faint); font-size: 12.5px; }

/* ---------- календарь ---------- */
.pop {
  position: absolute; z-index: 40; width: 640px; max-width: calc(100vw - 80px);
  background: #0f1843; border: 1px solid var(--line-strong); border-radius: 22px;
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.6); padding: 16px; display: grid;
  grid-template-columns: 150px 1fr; gap: 16px;
}
.pop .presets { display: grid; gap: 4px; align-content: start; }
.pop .presets button {
  text-align: left; background: none; border: 0; padding: 8px 12px; border-radius: 10px; color: var(--muted);
}
.pop .presets button:hover { background: rgba(91, 124, 255, 0.16); color: var(--text); }
.pop .cal { min-width: 0; }
.pop .head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.pop .head button { width: 34px; height: 34px; border-radius: 50%; border: 1px solid var(--line); background: rgba(8, 13, 38, 0.7); display: grid; place-items: center; }
.pop .head button:hover { border-color: #8aa0ff; }
.months { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.month .name { text-align: center; font-weight: 500; margin-bottom: 8px; text-transform: capitalize; }
.dow, .days { display: grid; grid-template-columns: repeat(7, 1fr); }
.dow span { text-align: center; color: var(--faint); font-size: 12px; padding: 4px 0; }
.days button {
  height: 34px; background: none; border: 0; color: var(--text); font-variant-numeric: tabular-nums; position: relative; border-radius: 0;
}
.days button:hover:not(:disabled):not(.sel) { background: rgba(91, 124, 255, 0.22); border-radius: 10px; }
.days button:disabled { color: #3c4676; cursor: default; }
.days button.in { background: rgba(91, 124, 255, 0.2); }
.days button.sel { background: linear-gradient(180deg, #5f80ff, #3d5be6); color: #fff; border-radius: 10px; box-shadow: 0 0 14px rgba(80, 110, 255, 0.5); z-index: 1; }
.days button.today::after { content: ""; position: absolute; left: 50%; bottom: 4px; width: 4px; height: 4px; margin-left: -2px; border-radius: 50%; background: var(--warn); }
.days i { display: block; }
.pop .inputs { display: flex; gap: 10px; align-items: center; margin-top: 14px; flex-wrap: wrap; }
.pop .inputs label { color: var(--muted); font-size: 13px; }
.pop .inputs input {
  width: 118px; height: 38px; border-radius: 30px; text-align: center; background: rgba(8, 13, 38, 0.8);
  border: 1px solid var(--line-strong); outline: 0; font-variant-numeric: tabular-nums;
}
.pop .inputs input:focus { border-color: #8aa0ff; }
.pop .inputs input.bad { border-color: var(--bad); }
.pop .last { display: flex; gap: 8px; align-items: center; margin-top: 12px; color: var(--muted); flex-wrap: wrap; }
.pop .last input { width: 64px; height: 36px; border-radius: 30px; text-align: center; background: rgba(8, 13, 38, 0.8); border: 1px solid var(--line-strong); outline: 0; }
.pop .last select { height: 36px; border-radius: 30px; background: rgba(8, 13, 38, 0.8); border: 1px solid var(--line-strong); padding: 0 10px; outline: 0; }
.pop .last select option { background: #0f1843; }
.pop .foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }

/* ---------- результаты ---------- */
.grid { display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 22px; margin-top: 22px; align-items: start; }
.res-head { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; }
.res-head h2 { margin: 0; font-size: 20px; font-weight: 400; }
.res-head .status { color: var(--muted); font-size: 13px; flex: 1 1 200px; min-width: 0; }
.sorts { display: flex; gap: 6px; flex-wrap: wrap; }
.sorts .chip { height: 32px; padding: 0 13px; font-size: 13px; }
.list { display: grid; gap: 12px; }
.clip {
  display: grid; grid-template-columns: auto 184px minmax(0, 1fr) auto; gap: 16px; align-items: center;
  padding: 12px 14px 12px 14px; border-radius: 20px; cursor: pointer;
  background: rgba(14, 22, 56, 0.62); border: 1px solid var(--line);
  transition: border-color .15s, background .15s;
}
.clip:hover { border-color: var(--line-strong); }
.clip.sel { background: linear-gradient(100deg, rgba(37, 60, 170, 0.85), rgba(22, 36, 106, 0.8)); border-color: rgba(130, 156, 255, 0.55); box-shadow: inset 0 -30px 50px -30px rgba(90, 120, 255, 0.5); }
.tick {
  width: 22px; height: 22px; border-radius: 50%; border: 1.5px solid var(--faint); display: grid; place-items: center;
  color: transparent; font-size: 13px; transition: all .15s; background: none; padding: 0;
}
.clip.sel .tick { background: var(--accent); border-color: transparent; color: #fff; }
.thumb { position: relative; width: 184px; aspect-ratio: 16 / 9; border-radius: 13px; overflow: hidden; background: #172254; }
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.thumb .dur { position: absolute; right: 7px; bottom: 7px; background: rgba(5, 8, 24, 0.82); padding: 1px 7px; border-radius: 8px; font-size: 12px; font-variant-numeric: tabular-nums; }
.clip h4 { margin: 0 0 8px; font-size: 15px; font-weight: 500; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 16px; color: var(--muted); font-size: 12.5px; }
.meta span { display: inline-flex; align-items: center; gap: 6px; }
.meta .rel { color: var(--faint); }
.acts { display: flex; gap: 8px; }
.acts button {
  width: 38px; height: 38px; border-radius: 50%; display: grid; place-items: center; font-size: 16px;
  background: rgba(8, 13, 38, 0.8); border: 1px solid var(--line); color: var(--muted);
}
.acts button:hover { color: #fff; border-color: #8aa0ff; background: rgba(60, 90, 230, 0.4); }
.more { margin: 16px auto 0; display: flex; }
.empty { text-align: center; padding: 56px 20px; color: var(--muted); }
.empty .big { width: 78px; height: 78px; margin: 0 auto 16px; border-radius: 50%; display: grid; place-items: center; font-size: 32px; background: rgba(10, 16, 44, 0.8); border: 1px solid var(--line); color: #8aa0ff; box-shadow: var(--glow); }
.empty h3 { margin: 0 0 6px; font-weight: 500; color: var(--text); font-size: 18px; }
.empty p { margin: 0 auto; max-width: 46ch; }
.alert { margin-bottom: 14px; padding: 12px 16px; border-radius: 16px; background: rgba(255, 107, 126, 0.12); border: 1px solid rgba(255, 107, 126, 0.45); color: #ffc2cb; }

.skel { height: 96px; border-radius: 20px; background: linear-gradient(90deg, rgba(20, 30, 70, 0.6), rgba(40, 56, 120, 0.5), rgba(20, 30, 70, 0.6)); background-size: 200% 100%; animation: sh 1.4s linear infinite; }
@keyframes sh { to { background-position: -200% 0; } }

/* ---------- правая колонка ---------- */
.rail { display: grid; gap: 18px; position: sticky; top: 0; }
.sel-num { font-size: 40px; font-weight: 300; line-height: 1; }
.sel-sub { color: var(--muted); margin: 4px 0 16px; }
.stack { display: grid; gap: 10px; }
.stack .btn { width: 100%; justify-content: flex-start; height: 42px; }
.hatch {
  height: 12px; border-radius: 8px; background: rgba(8, 13, 38, 0.9); border: 1px solid var(--line); overflow: hidden;
}
.hatch i {
  display: block; height: 100%; width: 0; transition: width .3s;
  background: repeating-linear-gradient(135deg, rgba(255, 255, 255, 0.5) 0 4px, rgba(255, 255, 255, 0.12) 4px 8px), #4262f0;
  background-blend-mode: overlay;
}
.hatch.done i { background: repeating-linear-gradient(135deg, rgba(255, 255, 255, 0.5) 0 4px, rgba(255, 255, 255, 0.12) 4px 8px), #2fbf86; }
.dl { display: grid; gap: 4px; padding: 10px 0; border-top: 1px solid var(--line); }
.dl:first-of-type { border-top: 0; }
.dl .t { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }
.dl .t span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dl .t span:last-child { color: var(--muted); flex: none; }
.dl .err { color: #ffb3bd; font-size: 12px; }
.folder { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 14px; background: rgba(8, 13, 38, 0.7); border: 1px solid var(--line); color: var(--muted); font-size: 12.5px; margin-bottom: 12px; }
.folder span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.folder button { background: none; border: 0; color: #a9b9ff; padding: 0; text-decoration: underline; text-underline-offset: 3px; flex: none; }
.chart { width: 100%; height: 130px; display: block; }
.chart text { fill: var(--faint); font-size: 10.5px; font-family: var(--font); }
.chart .bar { fill: url(#hatch); }
.chart .bar.top { fill: #fff; }
.note { color: var(--muted); font-size: 13px; }

/* ---------- модальное окно ---------- */
.veil { position: fixed; inset: 0; background: rgba(3, 6, 20, 0.66); backdrop-filter: blur(4px); z-index: 80; display: none; place-items: center; padding: 20px; }
.veil.on { display: grid; }
.modal { width: 520px; max-width: 100%; background: #101946; border: 1px solid var(--line-strong); border-radius: 26px; padding: 26px; box-shadow: 0 30px 90px rgba(0, 0, 0, 0.6); }
.modal h2 { margin: 0 0 6px; font-weight: 400; font-size: 24px; }
.modal p { color: var(--muted); margin: 0 0 18px; }
.modal .field { margin-bottom: 12px; flex: none; width: 100%; }
.modal .err { color: #ffb3bd; min-height: 20px; margin: 4px 2px 12px; font-size: 13px; }
.modal .foot { display: flex; gap: 10px; justify-content: flex-end; }

.toast { position: fixed; left: 50%; bottom: 34px; transform: translateX(-50%) translateY(20px); opacity: 0; transition: all .25s; z-index: 90; background: #1b2a73; border: 1px solid rgba(130, 156, 255, 0.5); padding: 11px 20px; border-radius: 30px; box-shadow: 0 14px 40px rgba(0, 0, 0, 0.5); pointer-events: none; }
.toast.on { opacity: 1; transform: translateX(-50%); }

.view { display: none; }
.view.on { display: block; }

@media (max-width: 1240px) { .grid { grid-template-columns: minmax(0, 1fr); } .rail { position: static; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); } }
@media (max-width: 980px) {
  .shell { grid-template-columns: 74px minmax(0, 1fr); }
  .nav button span:not(.ic), .brand b, .side .foot { display: none; }
  .nav .count { display: none; }
  .clip { grid-template-columns: auto 140px minmax(0, 1fr); }
  .thumb { width: 140px; }
  .acts { grid-column: 2 / -1; }
}
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
</style>
</head>
<body>
<canvas id="stars"></canvas>
<svg id="hills" viewBox="0 0 1440 220" preserveAspectRatio="none" aria-hidden="true">
  <defs><linearGradient id="hg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1a2349"/><stop offset="1" stop-color="#090d24"/></linearGradient></defs>
  <path d="M0 150 L90 118 L170 140 L260 96 L350 132 L430 108 L540 150 L650 104 L760 142 L860 112 L980 148 L1090 100 L1190 138 L1290 112 L1440 146 L1440 220 L0 220Z" fill="url(#hg)" opacity=".9"/>
  <path d="M0 190 L120 168 L240 186 L380 160 L520 184 L680 164 L820 188 L980 168 L1120 186 L1280 166 L1440 184 L1440 220 L0 220Z" fill="#070a1d"/>
</svg>

<div class="wc" id="wc" hidden>
  <button data-w="min" aria-label="Свернуть"><svg class="i" viewBox="0 0 24 24"><path d="M5 12h14"/></svg></button>
  <button data-w="max" aria-label="Развернуть"><svg class="i" viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="1.5"/></svg></button>
  <button data-w="close" class="x" aria-label="Закрыть"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
</div>
<div class="shell">
  <aside class="side">
    <div class="brand"><img src="/icon.png" alt=""><b>Clip<span>Search</span></b></div>
    <nav class="nav" aria-label="Разделы">
      <button class="on" data-view="search"><span class="ic"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></span><span>Поиск</span></button>
      <button data-view="downloads"><span class="ic"><svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg></span><span>Загрузки</span><span class="count" id="dlCount" hidden>0</span></button>
      <button id="navKeys"><span class="ic"><svg class="i" viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="m11 12 9-9m-3 3 3 3m-6-1 2 2"/></svg></span><span>Ключи Twitch</span></button>
    </nav>
    <div class="spacer"></div>
    <nav class="nav" aria-label="Программа">
      <button id="navQuit"><span class="ic"><svg class="i" viewBox="0 0 24 24"><path d="M10 4H5v16h5M15 8l4 4-4 4M19 12H9"/></svg></span><span>Выйти</span></button>
    </nav>
    <div class="foot">Версия __VERSION__</div>
  </aside>

  <section class="main">
    <header class="top">
      <div class="crumbs">
        <svg class="i" viewBox="0 0 24 24"><path d="m3 11 9-8 9 8M5 10v10h14V10"/></svg><span>Главная</span><span class="sep">/</span><span class="here" id="crumb">Поиск клипов</span>
      </div>
      <div class="grow"></div>
      <div class="pill" id="keyPill"><span class="dot"></span><span id="keyText">Проверяю ключи…</span></div>
    </header>

    <div class="scroller" id="scroller">
      <!-- ================= ПОИСК ================= -->
      <div class="view on" id="v-search">
        <h1>Найдите нужный <b>клип</b></h1>
        <p class="lede">Укажите стримера, игру, слова из названия и точный период. Можно заполнить только часть полей.</p>

        <div class="card filters">
          <div class="row">
            <label class="field" id="fLogin"><svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>
              <input id="login" placeholder="Стример, например shroud" autocomplete="off" spellcheck="false"><button class="x" type="button" aria-label="Очистить"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></label>
            <div class="field" id="fCat"><svg class="i" viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="11" rx="5"/><path d="M7 10v5M4.5 12.5h5M16 11h.01M18.5 14h.01"/></svg>
              <input id="cat" placeholder="Категория, например Just Chatting" autocomplete="off" spellcheck="false"><button class="x" type="button" aria-label="Очистить"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
              <div class="sug" id="sug" hidden></div></div>
          </div>
          <div class="row">
            <label class="field wide" id="fWords"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
              <input id="words" placeholder="Слова из названия. «-слово» исключает клипы с ним" autocomplete="off"><button class="x" type="button" aria-label="Очистить"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></label>
            <label class="field small" id="fCreator"><svg class="i" viewBox="0 0 24 24"><path d="M5 20 19 6M14 4l6 6M4 14l6 6"/></svg>
              <input id="creator" placeholder="Автор клипа" autocomplete="off"><button class="x" type="button" aria-label="Очистить"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></label>
            <label class="field small" id="fViews"><svg class="i" viewBox="0 0 24 24"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>
              <input id="minViews" inputmode="numeric" placeholder="Просмотров от" autocomplete="off"></label>
          </div>
          <div class="seg" id="periodSeg" style="position:relative">
            <span class="lbl">Период</span>
            <button class="chip" data-p="1h">1 час</button>
            <button class="chip" data-p="24h">24 часа</button>
            <button class="chip" data-p="7d">7 дней</button>
            <button class="chip" data-p="30d">30 дней</button>
            <button class="chip" data-p="all">Всё время</button>
            <button class="chip" id="rangeBtn" aria-haspopup="dialog"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M3 10h18M8 3v4M16 3v4"/></svg><span id="rangeText">Выбрать даты</span></button>
            <div class="pop" id="pop" role="dialog" aria-label="Выбор периода" hidden>
              <div class="presets" id="presets"></div>
              <div class="cal">
                <div class="head">
                  <button type="button" id="prevM" aria-label="Предыдущий месяц"><svg class="i" viewBox="0 0 24 24"><path d="m15 5-7 7 7 7"/></svg></button>
                  <span class="note" id="calHint">Нажмите на первую и последнюю дату</span>
                  <button type="button" id="nextM" aria-label="Следующий месяц"><svg class="i" viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></svg></button>
                </div>
                <div class="months" id="months"></div>
                <div class="inputs">
                  <label for="dFrom">С</label><input id="dFrom" placeholder="дд.мм.гггг" maxlength="10" inputmode="numeric" autocomplete="off">
                  <label for="dTo">по</label><input id="dTo" placeholder="дд.мм.гггг" maxlength="10" inputmode="numeric" autocomplete="off">
                </div>
                <div class="last">Или последние <input id="lastN" value="2" inputmode="numeric" maxlength="4"><select id="lastU"><option value="m">минут</option><option value="h" selected>часов</option><option value="d">дней</option><option value="w">недель</option><option value="mo">месяцев</option></select><button class="btn slim" type="button" id="lastGo">Применить</button></div>
                <div class="foot"><button class="btn slim" type="button" id="popClose">Закрыть</button><button class="btn slim primary" type="button" id="popOk">Готово</button></div>
              </div>
            </div>
          </div>
          <div class="go">
            <button class="btn primary" id="go"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>Искать</button>
            <button class="btn danger" id="stop" hidden>Остановить</button>
            <button class="switch" id="deep" role="switch" aria-checked="false" title="Период режется на недельные куски: находит больше старых и непопулярных клипов, но поиск идёт дольше"><span class="tr"></span>Глубокий поиск</button>
            <span class="hint" id="goHint"></span>
          </div>
        </div>

        <div class="grid">
          <div>
            <div id="alert"></div>
            <div class="res-head" id="resHead" hidden>
              <h2 id="resTitle">Найдено</h2>
              <span class="status" id="resStatus"></span>
              <div class="sorts" id="sorts">
                <button class="chip on" data-s="views">Просмотры</button>
                <button class="chip" data-s="date">Дата</button>
                <button class="chip" data-s="dur">Длительность</button>
                <button class="chip" data-s="title">Название</button>
              </div>
            </div>
            <div class="list" id="list"><div class="empty" id="firstEmpty"><div class="big"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></div><h3>Здесь появятся клипы</h3><p>Впишите стримера или категорию, выберите период и нажмите «Искать». Дату можно задать точно: с 01.02.2024 по 03.02.2026.</p></div></div>
            <button class="btn more" id="more" hidden>Показать ещё</button>
          </div>

          <aside class="rail">
            <div class="card">
              <h3>Выбранные клипы</h3>
              <div class="sel-num" id="selNum">0</div>
              <div class="sel-sub" id="selSub">Нажмите на карточки, чтобы выбрать</div>
              <div class="stack">
                <button class="btn" id="bDl" disabled><svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg>Скачать в 1080p</button>
                <button class="btn" id="bCopy" disabled><svg class="i" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="3"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>Скопировать ссылки</button>
                <button class="btn" id="bAll"><svg class="i" viewBox="0 0 24 24"><path d="m5 12 5 5 9-10"/></svg>Выбрать все найденные</button>
                <button class="btn" id="bCsv" disabled><svg class="i" viewBox="0 0 24 24"><path d="M7 3h8l4 4v14H7zM15 3v5h4M10 13h6M10 17h6"/></svg>Сохранить список в CSV</button>
              </div>
            </div>
            <div class="card">
              <h3>Загрузки <span class="grow"></span><span class="note" id="dlSum"></span></h3>
              <div class="folder"><svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><span id="dirText">…</span><button id="dirOpen">Открыть</button><button id="dirPick">Сменить</button></div>
              <div id="railDl"><div class="note">Скачанные клипы появятся здесь.</div></div>
            </div>
            <div class="card">
              <h3>Когда сняты клипы</h3>
              <svg class="chart" id="chart" viewBox="0 0 300 130" preserveAspectRatio="none" role="img" aria-label="График: сколько найденных клипов приходится на каждый отрезок периода"></svg>
              <div class="note" id="chartNote">После поиска здесь появится график.</div>
            </div>
          </aside>
        </div>
      </div>

      <!-- ================= ЗАГРУЗКИ ================= -->
      <div class="view" id="v-downloads">
        <h1>Загрузки</h1>
        <p class="lede">Клипы, которые вы скачали в этой сессии. Файлы лежат в папке загрузок.</p>
        <div class="card" style="max-width:760px">
          <div class="folder"><svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><span id="dirText2">…</span><button id="dirOpen2">Открыть</button><button id="dirPick2">Сменить</button></div>
          <div id="dlAll"><div class="note">Пока ничего не скачано. Выберите клипы на вкладке «Поиск» и нажмите «Скачать в 1080p».</div></div>
        </div>
      </div>
    </div>
  </section>
</div>

<div class="veil" id="keysVeil">
  <div class="modal" role="dialog" aria-labelledby="kTitle">
    <h2 id="kTitle">Ключи Twitch</h2>
    <p>Нужны Client ID и Client Secret вашего приложения на dev.twitch.tv/console. Те же, что вы вводили в Cloudflare. Они хранятся только на этом компьютере.</p>
    <label class="field"><svg class="i" viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="m11 12 9-9m-3 3 3 3"/></svg><input id="kId" placeholder="Client ID" autocomplete="off" spellcheck="false"></label>
    <label class="field"><svg class="i" viewBox="0 0 24 24"><rect x="4" y="10" width="16" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg><input id="kSecret" type="password" placeholder="Client Secret" autocomplete="off" spellcheck="false"></label>
    <div class="err" id="kErr"></div>
    <div class="foot"><button class="btn" id="kCancel">Позже</button><button class="btn primary" id="kSave">Проверить и сохранить</button></div>
  </div>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
(function () {
"use strict";
const T = "__TOKEN__";
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- связь с программой ---------- */
async function api(path, body) {
  const opt = { headers: { "X-Token": T, "Content-Type": "application/json" } };
  if (body !== undefined) { opt.method = "POST"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  return r.json();
}
setInterval(() => { fetch("/api/ping", { headers: { "X-Token": T } }).catch(() => {}); }, 3000);
fetch("/api/ping", { headers: { "X-Token": T } }).catch(() => {});
window.addEventListener("pagehide", () => {
  fetch("/api/bye", { method: "POST", headers: { "X-Token": T }, body: "{}", keepalive: true }).catch(() => {});
});

function toast(text) {
  const t = $("toast"); t.textContent = text; t.classList.add("on");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("on"), 2600);
}

/* ---------- фон ---------- */
(function stars() {
  const c = $("stars"), g = c.getContext("2d");
  function draw() {
    c.width = innerWidth; c.height = innerHeight; g.clearRect(0, 0, c.width, c.height);
    let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    for (let i = 0; i < 130; i++) {
      const x = rnd() * c.width, y = rnd() * c.height * 0.8, r = rnd() * 1.2 + 0.2;
      g.fillStyle = `rgba(210,222,255,${0.15 + rnd() * 0.6})`; g.beginPath(); g.arc(x, y, r, 0, 6.3); g.fill();
    }
  }
  draw(); addEventListener("resize", draw);
})();

/* ---------- даты ---------- */
const MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];
const day0 = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const endOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59);
const fmtD = (d) => String(d.getDate()).padStart(2, "0") + "." + String(d.getMonth() + 1).padStart(2, "0") + "." + d.getFullYear();
function parseD(s) {
  const m = /^(\d{1,2})[.\-/ ](\d{1,2})[.\-/ ](\d{4})$/.exec(s.trim());
  if (!m) return null;
  const d = new Date(+m[3], +m[2] - 1, +m[1]);
  return d.getFullYear() === +m[3] && d.getMonth() === +m[2] - 1 && d.getDate() === +m[1] ? d : null;
}
const sameDay = (a, b) => a && b && a.getTime() === b.getTime();
const TODAY = day0(new Date());
const UNIT = { m: 1, h: 60, d: 1440, w: 10080, mo: 43200 };
const UNIT_NAME = { m: ["минуту", "минуты", "минут"], h: ["час", "часа", "часов"], d: ["день", "дня", "дней"], w: ["неделю", "недели", "недель"], mo: ["месяц", "месяца", "месяцев"] };
function plural(n, f) { const a = n % 100, b = a % 10; return a > 10 && a < 20 ? f[2] : b > 1 && b < 5 ? f[1] : b === 1 ? f[0] : f[2]; }

/* ---------- состояние ---------- */
const S = {
  period: { kind: "preset", key: "7d" },
  rows: [], shown: 0, sort: { key: "views", desc: true },
  selected: new Set(), catId: "", catName: "",
  job: null, downloads: new Map(), dlJob: null, hasKeys: false, dir: "",
};
const PRESETS = { "1h": 60, "24h": 1440, "7d": 10080, "30d": 43200 };

function periodRange() {
  const p = S.period, now = new Date();
  if (p.kind === "preset") {
    if (p.key === "all") return { start: null, end: null };
    return { start: new Date(now - PRESETS[p.key] * 60000), end: now };
  }
  if (p.kind === "range") return { start: day0(p.from), end: endOfDay(p.to) };
  return { start: new Date(now - p.n * UNIT[p.u] * 60000), end: now };
}
function periodText() {
  const p = S.period;
  if (p.kind === "preset") return { all: "за всё время" }[p.key] || "за " + { "1h": "1 час", "24h": "24 часа", "7d": "7 дней", "30d": "30 дней" }[p.key];
  if (p.kind === "range") return `с ${fmtD(p.from)} по ${fmtD(p.to)}`;
  return `за ${p.n} ${plural(p.n, UNIT_NAME[p.u])}`;
}
function paintPeriod() {
  document.querySelectorAll("#periodSeg [data-p]").forEach((b) => b.classList.toggle("on", S.period.kind === "preset" && S.period.key === b.dataset.p));
  const rb = $("rangeBtn"), isCustom = S.period.kind !== "preset";
  rb.classList.toggle("on", isCustom);
  $("rangeText").textContent = isCustom ? periodText().replace(/^за /, "Последние ") : "Выбрать даты";
}
document.querySelectorAll("#periodSeg [data-p]").forEach((b) => b.addEventListener("click", () => { S.period = { kind: "preset", key: b.dataset.p }; paintPeriod(); closePop(); }));

/* ---------- календарь ---------- */
const cal = { y: TODAY.getFullYear(), m: TODAY.getMonth() - 1, from: null, to: null, hover: null };
function openPop() {
  const p = S.period;
  if (p.kind === "range") { cal.from = day0(p.from); cal.to = day0(p.to); cal.y = cal.to.getFullYear(); cal.m = cal.to.getMonth() - 1; }
  else { cal.from = cal.to = null; cal.y = TODAY.getFullYear(); cal.m = TODAY.getMonth() - 1; }
  fixMonth(); $("pop").hidden = false; placePop(); drawCal(); syncInputs();
}
function placePop() {
  const pop = $("pop"), seg = $("periodSeg"), btn = $("rangeBtn");
  const segR = seg.getBoundingClientRect(), bR = btn.getBoundingClientRect();
  let left = bR.right - segR.left - pop.offsetWidth;
  left = Math.max(-8, Math.min(left, segR.width - pop.offsetWidth + 8));
  pop.style.left = left + "px"; pop.style.top = (bR.bottom - segR.top + 10) + "px";
}
function closePop() { $("pop").hidden = true; }
function fixMonth() { while (cal.m < 0) { cal.m += 12; cal.y--; } while (cal.m > 11) { cal.m -= 12; cal.y++; } }
function drawCal() {
  fixMonth();
  const box = $("months"); box.innerHTML = "";
  for (let k = 0; k < 2; k++) {
    let y = cal.y, m = cal.m + k; if (m > 11) { m -= 12; y++; }
    const mm = document.createElement("div"); mm.className = "month";
    mm.innerHTML = `<div class="name">${MONTHS[m]} ${y}</div><div class="dow">${["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((d) => `<span>${d}</span>`).join("")}</div>`;
    const days = document.createElement("div"); days.className = "days";
    const first = (new Date(y, m, 1).getDay() + 6) % 7, count = new Date(y, m + 1, 0).getDate();
    for (let i = 0; i < first; i++) days.appendChild(document.createElement("i"));
    for (let d = 1; d <= count; d++) {
      const dt = new Date(y, m, d), b = document.createElement("button");
      b.type = "button"; b.textContent = d; b.dataset.t = dt.getTime();
      if (dt > TODAY) b.disabled = true;
      if (sameDay(dt, TODAY)) b.classList.add("today");
      const a = cal.from, z = cal.to || (a && cal.hover && cal.hover >= a ? cal.hover : null);
      if (sameDay(dt, a) || sameDay(dt, cal.to)) b.classList.add("sel");
      else if (a && z && dt > a && dt < z) b.classList.add("in");
      days.appendChild(b);
    }
    mm.appendChild(days); box.appendChild(mm);
  }
  $("calHint").textContent = cal.from && !cal.to ? "Теперь выберите последнюю дату" : "Нажмите на первую и последнюю дату";
}
$("months").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-t]"); if (!b) return;
  const d = new Date(+b.dataset.t);
  if (!cal.from || cal.to) { cal.from = d; cal.to = null; }
  else if (d < cal.from) { cal.to = cal.from; cal.from = d; }
  else cal.to = d;
  cal.hover = null; drawCal(); syncInputs();
});
$("months").addEventListener("mouseover", (e) => {
  const b = e.target.closest("button[data-t]");
  if (b && cal.from && !cal.to && !sameDay(cal.hover, new Date(+b.dataset.t))) { cal.hover = new Date(+b.dataset.t); drawCal(); }
});
function syncInputs() { $("dFrom").value = cal.from ? fmtD(cal.from) : ""; $("dTo").value = cal.to ? fmtD(cal.to) : cal.from ? fmtD(cal.from) : ""; $("dFrom").classList.remove("bad"); $("dTo").classList.remove("bad"); }
function typed() {
  const a = parseD($("dFrom").value), b = parseD($("dTo").value || $("dFrom").value);
  $("dFrom").classList.toggle("bad", !a && !!$("dFrom").value);
  $("dTo").classList.toggle("bad", !b && !!$("dTo").value);
  if (a && b) {
    cal.from = a <= b ? a : b; cal.to = a <= b ? b : a;
    if (cal.to > TODAY) cal.to = TODAY;
    cal.y = cal.from.getFullYear(); cal.m = cal.from.getMonth(); if (cal.to.getFullYear() * 12 + cal.to.getMonth() > cal.y * 12 + cal.m + 1) cal.m = cal.to.getMonth() - 1;
    drawCal();
  }
}
["dFrom", "dTo"].forEach((id) => {
  $(id).addEventListener("input", (e) => {
    let v = e.target.value.replace(/[^\d.]/g, "");
    if (e.inputType !== "deleteContentBackward") v = v.replace(/^(\d{2})(\d)/, "$1.$2").replace(/^(\d{2}\.\d{2})(\d)/, "$1.$2");
    e.target.value = v.slice(0, 10); typed();
  });
});
$("prevM").addEventListener("click", () => { cal.m--; drawCal(); });
$("nextM").addEventListener("click", () => { cal.m++; drawCal(); });
$("rangeBtn").addEventListener("click", () => ($("pop").hidden ? openPop() : closePop()));
$("popClose").addEventListener("click", closePop);
function applyRange() {
  if (!cal.from) { toast("Выберите даты в календаре"); return; }
  S.period = { kind: "range", from: cal.from, to: cal.to || cal.from }; paintPeriod(); closePop();
}
$("popOk").addEventListener("click", applyRange);
$("lastGo").addEventListener("click", () => {
  const n = parseInt($("lastN").value, 10);
  if (!(n > 0)) { toast("Впишите число больше нуля"); return; }
  S.period = { kind: "last", n, u: $("lastU").value }; paintPeriod(); closePop();
});
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const PRE = [
  ["Сегодня", () => [TODAY, TODAY]],
  ["Вчера", () => [addDays(TODAY, -1), addDays(TODAY, -1)]],
  ["Последние 7 дней", () => [addDays(TODAY, -6), TODAY]],
  ["Последние 30 дней", () => [addDays(TODAY, -29), TODAY]],
  ["Последние 90 дней", () => [addDays(TODAY, -89), TODAY]],
  ["Этот год", () => [new Date(TODAY.getFullYear(), 0, 1), TODAY]],
  ["Прошлый год", () => [new Date(TODAY.getFullYear() - 1, 0, 1), new Date(TODAY.getFullYear() - 1, 11, 31)]],
];
PRE.forEach(([name, fn]) => {
  const b = document.createElement("button"); b.type = "button"; b.textContent = name;
  b.addEventListener("click", () => { const [a, z] = fn(); cal.from = a; cal.to = z; cal.y = z.getFullYear(); cal.m = z.getMonth() - 1; drawCal(); syncInputs(); });
  $("presets").appendChild(b);
});
document.addEventListener("click", (e) => {
  const path = e.composedPath();
  if (!$("pop").hidden && !path.includes($("pop")) && !path.includes($("rangeBtn"))) closePop();
  if (!path.includes($("fCat"))) $("sug").hidden = true;
});
addEventListener("resize", () => { if (!$("pop").hidden) placePop(); });
addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closePop(); $("sug").hidden = true; if ($("keysVeil").classList.contains("on") && S.hasKeys) $("keysVeil").classList.remove("on"); }
});

/* ---------- поля ---------- */
["fLogin", "fCat", "fWords", "fCreator"].forEach((id) => {
  const f = $(id), inp = f.querySelector("input"), x = f.querySelector(".x");
  const upd = () => f.classList.toggle("has", !!inp.value);
  inp.addEventListener("input", upd); upd();
  x.addEventListener("click", () => { inp.value = ""; upd(); inp.dispatchEvent(new Event("input")); inp.focus(); });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && !(id === "fCat" && !$("sug").hidden && sugIdx >= 0)) startSearch(); });
});
$("minViews").addEventListener("keydown", (e) => { if (e.key === "Enter") startSearch(); });
$("deep").addEventListener("click", () => { const b = $("deep"); b.setAttribute("aria-checked", b.getAttribute("aria-checked") === "true" ? "false" : "true"); });

/* ---------- категории ---------- */
let catTimer = 0, sugIdx = -1, sugItems = [];
$("cat").addEventListener("input", () => {
  S.catId = ""; S.catName = "";
  clearTimeout(catTimer);
  const q = $("cat").value.trim();
  if (q.length < 2 || !S.hasKeys) { $("sug").hidden = true; return; }
  catTimer = setTimeout(async () => {
    const r = await api("/api/categories", { query: q });
    if ($("cat").value.trim() !== q) return;
    sugItems = r.items || []; sugIdx = -1; drawSug(r.error);
  }, 320);
});
function drawSug(err) {
  const box = $("sug"); box.innerHTML = "";
  if (err) box.innerHTML = `<div class="none">${esc(err)}</div>`;
  else if (!sugItems.length) box.innerHTML = '<div class="none">Такой категории не нашлось</div>';
  sugItems.forEach((c, i) => {
    const b = document.createElement("button"); b.type = "button";
    b.innerHTML = `<img src="${esc(c.art)}" alt="" loading="lazy"><span>${esc(c.name)}</span>`;
    b.addEventListener("click", () => pickCat(c));
    if (i === sugIdx) b.classList.add("act");
    box.appendChild(b);
  });
  box.hidden = false;
}
function pickCat(c) { S.catId = c.id; S.catName = c.name; $("cat").value = c.name; $("fCat").classList.add("has"); $("sug").hidden = true; }
$("cat").addEventListener("keydown", (e) => {
  if ($("sug").hidden || !sugItems.length) return;
  if (e.key === "ArrowDown") { sugIdx = (sugIdx + 1) % sugItems.length; drawSug(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { sugIdx = (sugIdx - 1 + sugItems.length) % sugItems.length; drawSug(); e.preventDefault(); }
  else if (e.key === "Enter" && sugIdx >= 0) { pickCat(sugItems[sugIdx]); e.preventDefault(); }
});

/* ---------- поиск ---------- */
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function showAlert(t) { $("alert").innerHTML = t ? `<div class="alert">${esc(t)}</div>` : ""; }
function setBusy(b) { $("go").disabled = b; $("stop").hidden = !b; }

async function startSearch() {
  if ($("go").disabled) return;
  if (!S.hasKeys) { openKeys(); return; }
  showAlert("");
  const login = $("login").value.trim(), cat = $("cat").value.trim();
  if (!login && !cat) { showAlert("Укажите стримера или категорию. Можно и то и другое."); $("login").focus(); return; }
  const { start, end } = periodRange();
  const body = {
    login, category: cat, category_id: S.catName && S.catName === cat ? S.catId : "",
    words: $("words").value, creator: $("creator").value, min_views: parseInt($("minViews").value, 10) || 0,
    pages: 10, deep: $("deep").getAttribute("aria-checked") === "true",
    start: start ? start.toISOString() : null, end: end ? end.toISOString() : null,
  };
  api("/api/settings", { last: { login, category: cat, words: $("words").value, creator: $("creator").value, period: serPeriod() } });
  setBusy(true); S.selected.clear(); S.rows = []; paintSel();
  $("resHead").hidden = false; $("resTitle").textContent = "Ищу…"; $("resStatus").textContent = "Подключаюсь к Twitch";
  $("list").innerHTML = '<div class="skel"></div><div class="skel"></div><div class="skel"></div>'; $("more").hidden = true;
  drawChart();
  const r = await api("/api/search", body);
  if (r.error) { fail(r.error); return; }
  S.job = r.job;
  for (;;) {
    const j = await api("/api/job?id=" + r.job);
    if (j.state === "running") { $("resStatus").textContent = j.progress || "Ищу…"; await sleep(350); continue; }
    setBusy(false);
    if (j.state === "error") { fail(j.error); return; }
    if (j.state === "cancelled") { $("resTitle").textContent = "Остановлено"; $("resStatus").textContent = ""; $("list").innerHTML = ""; return; }
    done(j.result); return;
  }
}
function fail(msg) { setBusy(false); showAlert(msg); $("resHead").hidden = true; $("list").innerHTML = ""; }
function done(res) {
  S.rows = res.rows.map((r) => ({ ...r, ts: new Date(r.created) }));
  if (res.category) { $("cat").value = res.category; $("fCat").classList.add("has"); }
  const n = S.rows.length;
  $("resTitle").textContent = n ? `Найдено ${n.toLocaleString("ru-RU")} ${plural(n, ["клип", "клипа", "клипов"])}` : "Ничего не найдено";
  let st = `${periodText()}. Просмотрено клипов: ${res.info.scanned.toLocaleString("ru-RU")}.`;
  if (res.info.truncated) st += " Часть клипов не просмотрена: сузьте период или включите глубокий поиск.";
  $("resStatus").textContent = st;
  sortRows(); S.shown = 0; $("list").innerHTML = ""; renderMore(); drawChart();
  if (!n) $("list").innerHTML = `<div class="empty"><div class="big"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M8.5 11h5"/></svg></div><h3>Ничего не нашлось</h3><p>Расширьте период, уберите часть слов или включите глубокий поиск.</p></div>`;
}
$("go").addEventListener("click", startSearch);
$("stop").addEventListener("click", () => { if (S.job) api("/api/cancel", { id: S.job }); $("resStatus").textContent = "Останавливаю…"; });

function serPeriod() { const p = S.period; return p.kind === "range" ? { kind: "range", from: p.from.getTime(), to: p.to.getTime() } : p; }
function loadPeriod(p) {
  if (!p) return;
  if (p.kind === "range") S.period = { kind: "range", from: new Date(p.from), to: new Date(p.to) };
  else if (p.kind === "preset" && (p.key === "all" || PRESETS[p.key])) S.period = p;
  else if (p.kind === "last") S.period = p;
}

/* ---------- таблица результатов ---------- */
function sortRows() {
  const { key, desc } = S.sort, k = { views: (r) => r.views, date: (r) => r.ts, dur: (r) => r.duration, title: (r) => r.title.toLowerCase() }[key];
  S.rows.sort((a, b) => (k(a) < k(b) ? -1 : k(a) > k(b) ? 1 : 0) * (desc ? -1 : 1));
}
document.querySelectorAll("#sorts .chip").forEach((b) => b.addEventListener("click", () => {
  const key = b.dataset.s;
  S.sort = { key, desc: S.sort.key === key ? !S.sort.desc : key !== "title" };
  document.querySelectorAll("#sorts .chip").forEach((x) => { x.classList.toggle("on", x === b); x.textContent = x.textContent.replace(/ [↑↓]$/, ""); });
  b.textContent += S.sort.desc ? " ↓" : " ↑";
  sortRows(); S.shown = 0; $("list").innerHTML = ""; renderMore();
}));
function ago(d) {
  const m = Math.max(0, Math.round((Date.now() - d) / 60000));
  if (m < 60) return `${m} мин назад`;
  const h = Math.round(m / 60); if (h < 48) return `${h} ч назад`;
  const dd = Math.round(h / 24); if (dd < 60) return `${dd} дн назад`;
  return `${Math.round(dd / 30)} мес назад`;
}
const mmss = (s) => { s = Math.round(s); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
const IC = {
  user: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>',
  game: '<svg class="i" viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="11" rx="5"/><path d="M7 10v5M4.5 12.5h5"/></svg>',
  clock: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  eye: '<svg class="i" viewBox="0 0 24 24"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>',
  play: '<svg class="i" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  copy: '<svg class="i" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="3"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>',
  dl: '<svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg>',
  ok: '<svg class="i" viewBox="0 0 24 24" style="stroke-width:3"><path d="m5 12 5 5 9-10"/></svg>',
};
function clipEl(r, i) {
  const el = document.createElement("div"); el.className = "clip" + (S.selected.has(r.id) ? " sel" : ""); el.dataset.id = r.id; el.dataset.i = i;
  const thumb = r.thumb ? `<img src="${esc(r.thumb)}" alt="" loading="lazy">` : "";
  el.innerHTML =
    `<button class="tick" aria-label="Выбрать клип">${IC.ok}</button>` +
    `<div class="thumb">${thumb}<span class="dur">${mmss(r.duration)}</span></div>` +
    `<div><h4>${esc(r.title)}</h4><div class="meta">` +
    `<span>${IC.user}${esc(r.creator || "неизвестно")}</span>` + (r.game ? `<span>${IC.game}${esc(r.game)}</span>` : "") +
    `<span>${IC.clock}${r.ts.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" })} <span class="rel">(${ago(r.ts)})</span></span>` +
    `<span>${IC.eye}${r.views.toLocaleString("ru-RU")}</span></div></div>` +
    `<div class="acts"><button data-a="open" title="Открыть на Twitch">${IC.play}</button><button data-a="copy" title="Скопировать ссылку">${IC.copy}</button><button data-a="dl" title="Скачать в 1080p">${IC.dl}</button></div>`;
  return el;
}
function renderMore() {
  const list = $("list"), end = Math.min(S.rows.length, S.shown + 40);
  const frag = document.createDocumentFragment();
  for (let i = S.shown; i < end; i++) frag.appendChild(clipEl(S.rows[i], i));
  list.appendChild(frag); S.shown = end; $("more").hidden = S.shown >= S.rows.length;
  $("more").textContent = `Показать ещё (${S.rows.length - S.shown})`;
}
$("more").addEventListener("click", renderMore);
$("scroller").addEventListener("scroll", () => { const s = $("scroller"); if (!$("more").hidden && s.scrollHeight - s.scrollTop - s.clientHeight < 400) renderMore(); });
$("list").addEventListener("click", (e) => {
  const el = e.target.closest(".clip"); if (!el) return;
  const r = S.rows[+el.dataset.i], a = e.target.closest("[data-a]");
  if (a) {
    e.stopPropagation();
    if (a.dataset.a === "open") api("/api/openurl", { url: r.url });
    if (a.dataset.a === "copy") copyText(r.url, "Ссылка скопирована");
    if (a.dataset.a === "dl") startDownload([r]);
    return;
  }
  if (S.selected.has(r.id)) S.selected.delete(r.id); else S.selected.add(r.id);
  el.classList.toggle("sel", S.selected.has(r.id)); paintSel();
});
$("list").addEventListener("dblclick", (e) => { const el = e.target.closest(".clip"); if (el && !e.target.closest("[data-a]")) api("/api/openurl", { url: S.rows[+el.dataset.i].url }); });
function selRows() { return S.rows.filter((r) => S.selected.has(r.id)); }
function paintSel() {
  const n = S.selected.size;
  $("selNum").textContent = n;
  $("selSub").textContent = n ? `${plural(n, ["клип выбран", "клипа выбрано", "клипов выбрано"])}` : "Нажмите на карточки, чтобы выбрать";
  ["bDl", "bCopy"].forEach((id) => ($(id).disabled = !n));
  $("bCsv").disabled = !S.rows.length;
  $("bAll").lastChild.textContent = n && n === S.rows.length ? "Снять выбор" : "Выбрать все найденные";
}
$("bAll").addEventListener("click", () => {
  if (!S.rows.length) return;
  if (S.selected.size === S.rows.length) S.selected.clear(); else S.rows.forEach((r) => S.selected.add(r.id));
  document.querySelectorAll(".clip").forEach((el) => el.classList.toggle("sel", S.selected.has(el.dataset.id))); paintSel();
});
async function copyText(t, msg) {
  try { await navigator.clipboard.writeText(t); toast(msg); }
  catch (e) { const ta = document.createElement("textarea"); ta.value = t; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); toast(msg); } catch (_) { toast("Не удалось скопировать"); } ta.remove(); }
}
$("bCopy").addEventListener("click", () => { const rows = selRows(); copyText(rows.map((r) => r.url).join("\n"), `Скопировано ссылок: ${rows.length}`); });
$("bCsv").addEventListener("click", async () => {
  const r = await api("/api/csv", { rows: S.rows });
  if (r.error) { showAlert(r.error); return; }
  toast("Список сохранён в папку загрузок"); api("/api/reveal", { path: r.path });
});

/* ---------- загрузки ---------- */
async function startDownload(rows) {
  if (!rows.length) return;
  if (S.dlJob) { toast("Подождите, предыдущая загрузка ещё идёт"); return; }
  const r = await api("/api/download", { items: rows.map((x) => ({ id: x.id, title: x.title, broadcaster: x.broadcaster })) });
  if (r.error) { showAlert(r.error); return; }
  S.dlJob = r.job; toast(`Скачиваю клипов: ${rows.length}`);
  for (;;) {
    const j = await api("/api/job?id=" + r.job);
    (j.items || []).forEach((it) => S.downloads.set(it.id, it));
    paintDl();
    if (j.state === "running") { await sleep(500); continue; }
    S.dlJob = null;
    if (j.state === "error") showAlert(j.error);
    else if (j.state === "done") toast("Клипы скачаны");
    return;
  }
}
$("bDl").addEventListener("click", () => startDownload(selRows()));
function dlHtml(it) {
  const pct = it.state === "done" ? 100 : it.pct, label = { wait: "в очереди", load: pct + "%", done: "готово", fail: "ошибка", stop: "остановлено" }[it.state] || "";
  return `<div class="dl"><div class="t"><span title="${esc(it.title)}">${esc(it.title || it.id)}</span><span>${label}</span></div><div class="hatch ${it.state === "done" ? "done" : ""}"><i style="width:${pct}%"></i></div>${it.error ? `<div class="err">${esc(it.error)}</div>` : ""}</div>`;
}
function paintDl() {
  const items = [...S.downloads.values()].reverse();
  $("dlCount").hidden = !items.length; $("dlCount").textContent = items.length;
  $("dlSum").textContent = items.length ? `всего ${items.length}` : "";
  $("railDl").innerHTML = items.length ? items.slice(0, 4).map(dlHtml).join("") : '<div class="note">Скачанные клипы появятся здесь.</div>';
  $("dlAll").innerHTML = items.length ? items.map(dlHtml).join("") : '<div class="note">Пока ничего не скачано. Выберите клипы на вкладке «Поиск» и нажмите «Скачать в 1080p».</div>';
}
function shortPath(p) { return p.length > 30 ? "…" + p.slice(-29) : p; }
function paintDir() { $("dirText").textContent = shortPath(S.dir); $("dirText2").textContent = S.dir; $("dirText").title = S.dir; }
async function pickDir() { const r = window.cs ? { path: await window.cs.pickFolder(S.dir) } : await api("/api/pickfolder", {}); if (r.path) { const s = await api("/api/settings", { download_dir: r.path }); S.dir = s.downloadDir; paintDir(); } else toast("Папка не выбрана"); }
["dirPick", "dirPick2"].forEach((id) => $(id).addEventListener("click", pickDir));
["dirOpen", "dirOpen2"].forEach((id) => $(id).addEventListener("click", () => api("/api/reveal", { path: S.dir })));

/* ---------- график ---------- */
function drawChart() {
  const svg = $("chart"), rows = S.rows; svg.innerHTML = '<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#3a56d6"/><rect width="3" height="6" fill="#8ea4ff" opacity=".75"/></pattern></defs>';
  if (!rows.length) { $("chartNote").textContent = "После поиска здесь появится график."; return; }
  const ts = rows.map((r) => +r.ts), lo = Math.min(...ts), hi = Math.max(...ts), N = Math.min(14, Math.max(2, rows.length));
  const span = Math.max(hi - lo, 1), cnt = new Array(N).fill(0);
  ts.forEach((t) => cnt[Math.min(N - 1, Math.floor(((t - lo) / span) * N))]++);
  const max = Math.max(...cnt), W = 300, H = 130, bw = W / N, base = H - 18;
  let top = cnt.indexOf(max), out = "";
  cnt.forEach((c, i) => {
    const h = Math.max(c ? 4 : 0, (c / max) * (base - 26)), x = i * bw + 3;
    out += `<rect class="bar ${i === top ? "top" : ""}" x="${x}" y="${base - h}" width="${bw - 6}" height="${h}" rx="3"/>`;
  });
  const tx = top * bw + bw / 2;
  out += `<g><rect x="${Math.min(Math.max(tx - 22, 0), W - 44)}" y="2" width="44" height="18" rx="9" fill="#4262f0"/><text x="${Math.min(Math.max(tx, 22), W - 22)}" y="14.5" text-anchor="middle" style="fill:#fff">${max}</text></g>`;
  const f = (t) => new Date(t).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit" });
  out += `<text x="0" y="${H - 3}">${f(lo)}</text><text x="${W}" y="${H - 3}" text-anchor="end">${f(hi)}</text>`;
  svg.insertAdjacentHTML("beforeend", out);
  $("chartNote").textContent = `Больше всего клипов (${max}) пришлось на период около ${f(lo + span * ((top + 0.5) / N))}.`;
}

/* ---------- ключи, разделы, выход ---------- */
function openKeys() { $("kErr").textContent = ""; $("keysVeil").classList.add("on"); setTimeout(() => $("kId").focus(), 50); }
$("navKeys").addEventListener("click", openKeys);
$("keyPill").addEventListener("click", (e) => { if (e.target.closest("button")) openKeys(); });
$("kCancel").addEventListener("click", () => $("keysVeil").classList.remove("on"));
$("kSave").addEventListener("click", async () => {
  $("kErr").textContent = ""; $("kSave").disabled = true; $("kSave").textContent = "Проверяю…";
  const r = await api("/api/keys", { client_id: $("kId").value, client_secret: $("kSecret").value });
  $("kSave").disabled = false; $("kSave").textContent = "Проверить и сохранить";
  if (r.error) { $("kErr").textContent = r.error; return; }
  S.hasKeys = true; paintKeys(); $("keysVeil").classList.remove("on"); $("kSecret").value = ""; toast("Ключи сохранены");
});
function paintKeys() {
  const p = $("keyPill"); p.className = "pill " + (S.hasKeys ? "ok" : "bad");
  $("keyText").innerHTML = S.hasKeys ? "Twitch подключён" : 'Нужны ключи Twitch <button>Ввести</button>';
}
document.querySelectorAll(".nav [data-view]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".nav [data-view]").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.id === "v-" + b.dataset.view));
  $("crumb").textContent = b.dataset.view === "search" ? "Поиск клипов" : "Загрузки";
  $("scroller").scrollTop = 0;
}));
if (window.cs) {
  document.body.classList.add("has-wc"); $("wc").hidden = false;
  $("wc").addEventListener("click", (e) => { const b = e.target.closest("[data-w]"); if (b) window.cs.win(b.dataset.w); });
}
$("navQuit").addEventListener("click", async () => {
  if (window.cs) { window.cs.win("close"); return; }
  await api("/api/quit", {});
  document.body.innerHTML = '<div style="height:100vh;display:grid;place-items:center;color:#95a0d0;font:16px system-ui">Программа закрыта. Это окно можно закрыть.</div>';
  setTimeout(() => window.close(), 300);
});

/* ---------- старт ---------- */
(async function init() {
  paintPeriod(); paintSel(); paintDl();
  const st = await api("/api/state");
  S.hasKeys = st.hasKeys; S.dir = st.downloadDir; paintKeys(); paintDir();
  const l = st.last || {};
  $("login").value = l.login || ""; $("cat").value = l.category || ""; $("words").value = l.words || ""; $("creator").value = l.creator || "";
  ["login", "cat", "words", "creator"].forEach((id) => $(id).dispatchEvent(new Event("input")));
  S.catId = ""; loadPeriod(l.period); paintPeriod();
  if (!S.hasKeys) openKeys();
  $("goHint").textContent = "Enter в любом поле запускает поиск";
})();
})();
</script>
</body>
</html>
'''
ICON_PNG_B64 = "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"

HEARTBEAT_LIMIT = 150      # сек без сигнала от окна -> программа закрывается
BYE_DELAY = 6              # сек после закрытия окна


def data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, "ClipSearch")
    os.makedirs(path, exist_ok=True)
    return path


def log(msg):
    try:
        with open(os.path.join(data_dir(), "log.txt"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def default_download_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads", "ClipSearch")


def icon_ico_bytes():
    png = base64.b64decode(ICON_PNG_B64)
    header = b"\x00\x00\x01\x00\x01\x00"
    entry = bytes([0, 0, 0, 0]) + (1).to_bytes(2, "little") + (32).to_bytes(2, "little")
    entry += len(png).to_bytes(4, "little") + (22).to_bytes(4, "little")
    return header + entry + png


class Job:
    def __init__(self, kind):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.state = "running"
        self.progress = ""
        self.result = None
        self.error = ""
        self.items = []
        self.cancel = threading.Event()

    def view(self):
        d = {"id": self.id, "kind": self.kind, "state": self.state, "progress": self.progress,
             "error": self.error, "items": self.items}
        if self.state == "done":
            d["result"] = self.result
        return d


class Ctx:
    def __init__(self):
        self.token = secrets.token_urlsafe(16)
        self.cfg = load_config()
        self.jobs = {}
        self.last_ping = time.time()
        self.seen_ping = False
        self.bye_at = None
        self.quit = threading.Event()
        self.lock = threading.Lock()

    def twitch(self):
        cid, sec = self.cfg.get("client_id", ""), self.cfg.get("client_secret", "")
        if not cid or not sec:
            raise TwitchError("Сначала введите ключи Twitch (раздел «Ключи Twitch»).")
        key = (cid, sec)
        if getattr(self, "_tw_key", None) != key:
            self._tw, self._tw_key = Twitch(cid, sec), key
        return self._tw

    def download_dir(self):
        return self.cfg.get("download_dir") or default_download_dir()

    def start_job(self, kind, fn):
        job = Job(kind)
        self.jobs[job.id] = job

        def run():
            try:
                job.result = fn(job)
                job.state = "done"
            except Cancelled:
                job.state = "cancelled"
            except (TwitchError, ValueError) as e:
                job.state, job.error = "error", str(e)
            except Exception as e:
                log(f"job {kind}: {e!r}")
                job.state, job.error = "error", f"Неожиданная ошибка: {e!r}"

        threading.Thread(target=run, daemon=True).start()
        return job


def row_json(r):
    return {**r, "created": r["created"].strftime("%Y-%m-%dT%H:%M:%SZ")}


def parse_iso(s):
    if not s:
        return None
    try:
        return parse_time(str(s)[:19] + "Z")
    except Exception:
        raise ValueError("Не удалось прочитать дату.")


def pick_folder(initial):
    if not sys.platform.startswith("win"):
        return ""
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$f.Description='Куда сохранять клипы';"
        "if($env:CLIPS_INIT -and (Test-Path $env:CLIPS_INIT)){$f.SelectedPath=$env:CLIPS_INIT};"
        "if($f.ShowDialog() -eq 'OK'){Write-Output $f.SelectedPath}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, timeout=600, env={**os.environ, "CLIPS_INIT": initial},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def reveal(path):
    try:
        if sys.platform.startswith("win"):
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", path])
            else:
                os.startfile(path)  # noqa
    except Exception as e:
        log(f"reveal: {e!r}")


def make_handler(ctx):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            return secrets.compare_digest(self.headers.get("X-Token", ""), ctx.token)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                html = UI_HTML.replace("__TOKEN__", ctx.token).replace("__VERSION__", VERSION)
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path == "/icon.png":
                return self._send(200, base64.b64decode(ICON_PNG_B64), "image/png")
            if not self._authed():
                return self._send(403, {"error": "forbidden"})
            if u.path == "/api/ping":
                ctx.last_ping, ctx.seen_ping, ctx.bye_at = time.time(), True, None
                return self._send(200, {"ok": True})
            if u.path == "/api/state":
                cfg = ctx.cfg
                return self._send(200, {
                    "hasKeys": bool(cfg.get("client_id") and cfg.get("client_secret")),
                    "downloadDir": ctx.download_dir(),
                    "last": cfg.get("last", {}),
                    "version": VERSION,
                })
            if u.path == "/api/job":
                job = ctx.jobs.get(urllib.parse.parse_qs(u.query).get("id", [""])[0])
                if not job:
                    return self._send(404, {"error": "нет такой задачи"})
                return self._send(200, job.view())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return self._send(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                out = self.route(self.path, body)
                self._send(200, out)
            except (TwitchError, ValueError) as e:
                self._send(200, {"error": str(e)})
            except Exception as e:
                log(f"POST {self.path}: {e!r}")
                self._send(200, {"error": f"Неожиданная ошибка: {e!r}"})

        def route(self, path, b):
            if path == "/api/bye":
                ctx.bye_at = time.time() + BYE_DELAY
                return {"ok": True}
            if path == "/api/quit":
                ctx.quit.set()
                return {"ok": True}
            if path == "/api/keys":
                cid, sec = str(b.get("client_id", "")).strip(), str(b.get("client_secret", "")).strip()
                if not cid or not sec:
                    raise ValueError("Заполните оба поля.")
                Twitch(cid, sec)._login()
                ctx.cfg["client_id"], ctx.cfg["client_secret"] = cid, sec
                save_config(ctx.cfg)
                return {"ok": True}
            if path == "/api/settings":
                if "download_dir" in b:
                    ctx.cfg["download_dir"] = str(b["download_dir"])
                if "last" in b and isinstance(b["last"], dict):
                    ctx.cfg["last"] = b["last"]
                save_config(ctx.cfg)
                return {"ok": True, "downloadDir": ctx.download_dir()}
            if path == "/api/categories":
                q = str(b.get("query", "")).strip()
                return {"items": ctx.twitch().categories(q) if q else []}
            if path == "/api/search":
                return self.search(b)
            if path == "/api/cancel":
                job = ctx.jobs.get(b.get("id"))
                if job:
                    job.cancel.set()
                return {"ok": True}
            if path == "/api/download":
                return self.download(b)
            if path == "/api/csv":
                rows = b.get("rows") or []
                folder = ctx.download_dir()
                os.makedirs(folder, exist_ok=True)
                p = os.path.join(folder, f"clips_{datetime.now():%Y%m%d_%H%M%S}.csv")
                with open(p, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f, delimiter=";")
                    w.writerow(["Название", "Автор клипа", "Стример", "Игра", "Дата (UTC)", "Просмотры", "Секунд", "Ссылка"])
                    for r in rows:
                        w.writerow([r.get("title"), r.get("creator"), r.get("broadcaster"), r.get("game"),
                                    r.get("created"), r.get("views"), round(float(r.get("duration") or 0)), r.get("url")])
                return {"path": p}
            if path == "/api/pickfolder":
                return {"path": pick_folder(ctx.download_dir())}
            if path == "/api/openurl":
                url = str(b.get("url", ""))
                host = urllib.parse.urlparse(url).hostname or ""
                if not (host == "twitch.tv" or host.endswith(".twitch.tv")):
                    raise ValueError("Можно открывать только ссылки Twitch.")
                webbrowser.open(url)
                return {"ok": True}
            if path == "/api/reveal":
                p = str(b.get("path", "")) or ctx.download_dir()
                os.makedirs(p if not os.path.splitext(p)[1] else os.path.dirname(p), exist_ok=True)
                reveal(p)
                return {"ok": True}
            raise ValueError("Неизвестная команда.")

        def search(self, b):
            tw = ctx.twitch()
            login = str(b.get("login", "")).strip().lower()
            login = re.sub(r"^https?://(www\.)?twitch\.tv/", "", login).split("/")[0].split("?")[0]
            if login and not re.fullmatch(r"[a-z0-9_]{3,25}", login):
                raise ValueError("Ник стримера выглядит неверно: только латинские буквы, цифры и «_».")
            game_id = str(b.get("category_id", "")).strip()
            cat_name = str(b.get("category", "")).strip()
            start, end = parse_iso(b.get("start")), parse_iso(b.get("end"))
            args = dict(
                words=str(b.get("words", "")), start=start, end=end, creator=str(b.get("creator", "")),
                min_views=max(0, int(b.get("min_views") or 0)),
                max_pages=min(50, max(1, int(b.get("pages") or 10))), deep=bool(b.get("deep")),
            )
            if not login and not game_id and not cat_name:
                raise ValueError("Укажите стримера или категорию (можно и то и другое).")

            def fn(job):
                gid, gname = game_id, cat_name
                if cat_name and not gid:
                    cats = tw.categories(cat_name, job.cancel)
                    exact = [c for c in cats if c["name"].lower() == cat_name.lower()]
                    pick = exact[0] if exact else (cats[0] if cats else None)
                    if not pick:
                        raise ValueError(f"Категория «{cat_name}» не найдена.")
                    gid, gname = pick["id"], pick["name"]
                rows, info = search_clips(
                    tw, login=login, game_id=gid,
                    progress=lambda t: setattr(job, "progress", t), cancel=job.cancel, **args,
                )
                return {"rows": [row_json(r) for r in rows], "info": info, "category": gname if gid else ""}

            return {"job": ctx.start_job("search", fn).id}

        def download(self, b):
            items = [i for i in (b.get("items") or []) if re.fullmatch(r"[A-Za-z0-9_-]+", str(i.get("id", "")))]
            if not items:
                raise ValueError("Не выбрано ни одного клипа.")
            folder = ctx.download_dir()

            def fn(job):
                os.makedirs(folder, exist_ok=True)
                job.items = [{"id": i["id"], "title": i.get("title", ""), "pct": 0, "state": "wait", "path": ""} for i in items]
                for it, src in zip(job.items, items):
                    it["state"] = "load"
                    row = {"id": src["id"], "title": str(src.get("title", "")), "broadcaster": str(src.get("broadcaster", ""))}
                    try:
                        def prog(p, it=it):
                            it["pct"] = p
                        it["path"] = download_clip(row, folder, progress=prog, cancel=job.cancel)
                        it["pct"], it["state"] = 100, "done"
                    except Cancelled:
                        it["state"] = "stop"
                        raise
                    except Exception as e:
                        it["state"] = "fail"
                        it["error"] = str(e)
                return {"folder": folder}

            return {"job": ctx.start_job("download", fn).id}

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def find_browser():
    pf = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")]
    rel = [
        os.path.join("Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join("Google", "Chrome", "Application", "chrome.exe"),
        os.path.join("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        os.path.join("Chromium", "Application", "chrome.exe"),
    ]
    for r in rel:
        for base in pf:
            if base and os.path.exists(os.path.join(base, r)):
                return os.path.join(base, r)
    return None


def launch_window(url):
    exe = os.environ.get("CLIPS_BROWSER") or find_browser()
    if exe:
        try:
            profile = os.path.join(data_dir(), "window")
            subprocess.Popen([
                exe, f"--app={url}", "--window-size=1400,900", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-features=Translate,msEdgeSidebar",
            ])
            return True
        except Exception as e:
            log(f"browser: {e!r}")
    webbrowser.open(url)
    return False


def message_box(text):
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, "Поиск клипов", 0x10)
    except Exception:
        try:
            print(text)
        except Exception:
            pass


def run_app(open_window=True):
    ctx = Ctx()
    srv = Server(("127.0.0.1", 0), make_handler(ctx))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    log(f"start {VERSION} port={port}")
    if open_window:
        launch_window(url)
    started = time.time()
    while not ctx.quit.is_set():
        time.sleep(0.5)
        now = time.time()
        if ctx.bye_at and now > ctx.bye_at:
            break
        if ctx.seen_ping and now - ctx.last_ping > HEARTBEAT_LIMIT:
            break
        if not ctx.seen_ping and now - started > 120:
            break
    srv.shutdown()
    return ctx


# ---------------------------------------------------------------- оболочка окна (Electron)
# Эти три файла сборка кладёт в папку shell и упаковывает вместе с программой.

SHELL_PACKAGE_JSON = r'''{
  "name": "clipsearch",
  "productName": "ClipSearch",
  "version": "2.1.0",
  "description": "Поиск клипов Twitch",
  "main": "main.js",
  "devDependencies": {
    "electron": "^33.2.1"
  }
}
'''

SHELL_PRELOAD_JS = r'''const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("cs", {
  win: (what) => ipcRenderer.send("win", what),
  pickFolder: (initial) => ipcRenderer.invoke("pick-folder", initial),
});
'''

SHELL_MAIN_JS = r'''const { app, BrowserWindow, ipcMain, dialog, Menu } = require("electron");
const path = require("path");
const net = require("net");
const http = require("http");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");

const SMOKE = process.argv.includes("--smoke");
let win = null;
let backend = null;

function smokeLog(text) {
  if (!SMOKE) return;
  try { fs.appendFileSync(path.join(os.tmpdir(), "clipsearch-smoke.txt"), text + "\n"); } catch (e) {}
}

function fatal(e) {
  smokeLog("FATAL " + String(e));
  try { dialog.showErrorBox("Поиск клипов", "Программа не запустилась: " + String(e)); } catch (x) {}
  app.exit(1);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}

function waitReady(port) {
  return new Promise((resolve, reject) => {
    let tries = 0;
    const attempt = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/icon.png", timeout: 1500 }, (res) => {
        res.resume();
        if (res.statusCode === 200) resolve(); else retry();
      });
      req.on("error", retry);
      req.on("timeout", () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (++tries > 120) reject(new Error("не запустился встроенный сервер"));
      else setTimeout(attempt, 250);
    };
    attempt();
  });
}

function killBackend() {
  if (!backend) return;
  const pid = backend.pid;
  try {
    if (process.platform === "win32") spawn("taskkill", ["/pid", String(pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
    else backend.kill();
  } catch (e) {}
}

function backendPath() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "clip_backend.exe")
    : path.join(__dirname, "backend", "clip_backend.exe");
}

function createWindow(url) {
  win = new BrowserWindow({
    width: 1380,
    height: 880,
    minWidth: 1000,
    minHeight: 640,
    frame: false,
    backgroundColor: "#070b1f",
    show: false,
    title: "Поиск клипов",
    icon: path.join(__dirname, "clip_search.ico"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  win.once("ready-to-show", () => win.show());
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (e, target) => { if (!target.startsWith(url)) e.preventDefault(); });
  win.on("closed", () => { win = null; app.quit(); });
  win.webContents.on("did-finish-load", async () => {
    if (!SMOKE) return;
    try {
      const r = await win.webContents.executeJavaScript(
        'document.title + "|shell=" + !!document.querySelector(".shell") + "|cs=" + (typeof window.cs)'
      );
      smokeLog("SMOKE OK " + r);
    } catch (e) { smokeLog("SMOKE FAIL " + e); }
    setTimeout(() => app.quit(), 500);
  });
  win.loadURL(url);
}

ipcMain.on("win", (e, what) => {
  const w = BrowserWindow.fromWebContents(e.sender);
  if (!w) return;
  if (what === "min") w.minimize();
  else if (what === "max") { if (w.isMaximized()) w.unmaximize(); else w.maximize(); }
  else if (what === "close") w.close();
});

ipcMain.handle("pick-folder", async (e, initial) => {
  const w = BrowserWindow.fromWebContents(e.sender);
  const r = await dialog.showOpenDialog(w, {
    title: "Куда сохранять клипы",
    defaultPath: initial || undefined,
    properties: ["openDirectory", "createDirectory"],
  });
  return r.canceled || !r.filePaths.length ? "" : r.filePaths[0];
});

async function main() {
  Menu.setApplicationMenu(null);
  const port = await freePort();
  const token = crypto.randomBytes(16).toString("hex");
  backend = spawn(backendPath(), ["--serve", "--port", String(port), "--token", token], { windowsHide: true, stdio: "ignore" });
  backend.on("error", fatal);
  backend.on("exit", (code) => { backend = null; if (code) fatal("встроенный сервер остановился, код " + code); else app.quit(); });
  await waitReady(port);
  createWindow("http://127.0.0.1:" + port + "/");
  if (SMOKE) setTimeout(() => { smokeLog("SMOKE TIMEOUT"); app.exit(2); }, 60000);
}

if (!SMOKE && !app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.on("before-quit", killBackend);
  app.on("window-all-closed", () => app.quit());
  app.whenReady().then(main).catch(fatal);
}
'''


def write_shell(folder):
    os.makedirs(folder, exist_ok=True)
    for name, text in (("package.json", SHELL_PACKAGE_JSON), ("preload.js", SHELL_PRELOAD_JS), ("main.js", SHELL_MAIN_JS)):
        with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    with open(os.path.join(folder, "clip_search.ico"), "wb") as f:
        f.write(icon_ico_bytes())


def serve(port, token):
    """Режим для оболочки: только сервер, окно открывает Electron."""
    ctx = Ctx()
    ctx.token = token
    srv = Server(("127.0.0.1", port), make_handler(ctx))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"serve {VERSION} port={port}")
    started = time.time()
    while not ctx.quit.is_set():
        time.sleep(0.5)
        now = time.time()
        if ctx.bye_at and now > ctx.bye_at:
            break
        if ctx.seen_ping and now - ctx.last_ping > 40:
            break
        if not ctx.seen_ping and now - started > 180:
            break
    srv.shutdown()


def arg_value(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""


def selftest():
    import urllib.request as ur

    ctx = Ctx()
    srv = Server(("127.0.0.1", 0), make_handler(ctx))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    html = ur.urlopen(base + "/").read().decode("utf-8")
    assert "<title>" in html and ctx.token in html
    req = ur.Request(base + "/api/state", headers={"X-Token": ctx.token})
    state = json.load(ur.urlopen(req))
    assert "hasKeys" in state
    try:
        ur.urlopen(base + "/api/state")
        raise AssertionError("без токена должно быть 403")
    except urllib.error.HTTPError as e:
        assert e.code == 403
    assert len(icon_ico_bytes()) > 100
    srv.shutdown()
    print("SELFTEST OK", VERSION, "browser:", find_browser())
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--write-icon" in sys.argv:
        with open("clip_search.ico", "wb") as f:
            f.write(icon_ico_bytes())
        return 0
    if "--write-shell" in sys.argv:
        write_shell(arg_value("--write-shell") or "shell")
        return 0
    if "--serve" in sys.argv:
        try:
            serve(int(arg_value("--port")), arg_value("--token"))
        except Exception as e:
            log(f"fatal serve: {e!r}")
            return 1
        return 0
    try:
        run_app()
    except Exception as e:
        log(f"fatal: {e!r}")
        message_box(f"Программа не запустилась: {e}\nПодробности: {os.path.join(data_dir(), 'log.txt')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
