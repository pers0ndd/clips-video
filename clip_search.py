"""Поиск клипов Twitch: стример, категория, слова из названия, период.

Запуск: двойной щелчок по clip_search.exe (или: python clip_search.py).
Ключи Twitch (Client ID и Client Secret) программа спросит при первом запуске
и сохранит только на этом компьютере.
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

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    HAVE_TK = True
except Exception:  # на сервере без окон можно проверять только логику
    HAVE_TK = False

VERSION = "1.0"

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
        return [{"id": c["id"], "name": c["name"]} for c in d]

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


def search_clips(tw, login="", game_id="", words="", minutes=0, creator="", min_views=0,
                 max_pages=10, deep=False, progress=None, cancel=None):
    """Ищет клипы. Возвращает (список клипов, сведения)."""
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
    if game_id:
        base["game_id"] = game_id

    now = datetime.now(timezone.utc)
    if minutes > 0:
        start = now - timedelta(minutes=minutes)
    else:
        start = max(created, TWITCH_CLIPS_START) if created else TWITCH_CLIPS_START
    if minutes > 0 or deep:
        windows = make_windows(start, now, deep)
        dated = True
    else:
        windows = [(None, None)]
        dated = False

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


# ---------------------------------------------------------------- окно

if HAVE_TK:

    class App(tk.Tk):
        def __init__(self, ask_keys=True):
            super().__init__()
            self.title(f"Поиск клипов Twitch {VERSION}")
            self.geometry("1100x680")
            self.minsize(900, 520)
            self.cfg = load_config()
            self.q = queue.Queue()
            self.cancel = threading.Event()
            self.busy = False
            self.rows = []
            self.sort_key = ("views", True)
            self.cat_map = {}
            self._build()
            self.after(100, self._poll)
            if ask_keys and not (self.cfg.get("client_id") and self.cfg.get("client_secret")):
                self.after(300, self.keys_dialog)

        # ---- построение окна
        def _build(self):
            pad = {"padx": 6, "pady": 4}
            form = ttk.Frame(self, padding=8)
            form.pack(fill="x")
            form.columnconfigure(1, weight=1)
            form.columnconfigure(3, weight=1)

            ttk.Label(form, text="Стример (ник):").grid(row=0, column=0, sticky="e", **pad)
            self.v_login = tk.StringVar(value=self.cfg.get("login", ""))
            self.e_login = ttk.Entry(form, textvariable=self.v_login)
            self.e_login.grid(row=0, column=1, sticky="ew", **pad)

            ttk.Label(form, text="Категория (игра):").grid(row=0, column=2, sticky="e", **pad)
            self.v_cat = tk.StringVar(value=self.cfg.get("category", ""))
            self.c_cat = ttk.Combobox(form, textvariable=self.v_cat, values=[])
            self.c_cat.grid(row=0, column=3, sticky="ew", **pad)
            self.b_cat = ttk.Button(form, text="Найти категорию", command=self.find_categories)
            self.b_cat.grid(row=0, column=4, **pad)

            ttk.Label(form, text="Слова в названии:").grid(row=1, column=0, sticky="e", **pad)
            self.v_words = tk.StringVar()
            self.e_words = ttk.Entry(form, textvariable=self.v_words)
            self.e_words.grid(row=1, column=1, columnspan=3, sticky="ew", **pad)
            ttk.Label(form, text="через пробел; «-слово» исключает", foreground="#666").grid(row=1, column=4, sticky="w", **pad)

            ttk.Label(form, text="Период:").grid(row=2, column=0, sticky="e", **pad)
            self.v_period = tk.StringVar(value=self.cfg.get("period", "7 дней"))
            self.c_period = ttk.Combobox(form, textvariable=self.v_period, values=PRESETS)
            self.c_period.grid(row=2, column=1, sticky="ew", **pad)
            ttk.Label(form, text="или свой: «2 часа», «3 дня», «2 недели»", foreground="#666").grid(row=2, column=2, columnspan=2, sticky="w", **pad)

            ttk.Label(form, text="Автор клипа:").grid(row=3, column=0, sticky="e", **pad)
            self.v_creator = tk.StringVar()
            ttk.Entry(form, textvariable=self.v_creator).grid(row=3, column=1, sticky="ew", **pad)
            ttk.Label(form, text="Мин. просмотров:").grid(row=3, column=2, sticky="e", **pad)
            self.v_minviews = tk.StringVar(value="0")
            ttk.Entry(form, textvariable=self.v_minviews, width=10).grid(row=3, column=3, sticky="w", **pad)

            opts = ttk.Frame(form)
            opts.grid(row=4, column=0, columnspan=5, sticky="ew", **pad)
            self.v_deep = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                opts, text="Глубокий поиск (медленнее, находит больше старых и непопулярных клипов)",
                variable=self.v_deep,
            ).pack(side="left")
            ttk.Label(opts, text="   Страниц на период:").pack(side="left")
            self.v_pages = tk.StringVar(value="10")
            ttk.Entry(opts, textvariable=self.v_pages, width=5).pack(side="left", padx=4)

            btns = ttk.Frame(form)
            btns.grid(row=5, column=0, columnspan=5, sticky="ew", **pad)
            self.b_search = ttk.Button(btns, text="🔎 Искать", command=self.start_search)
            self.b_search.pack(side="left")
            self.b_stop = ttk.Button(btns, text="Стоп", command=self.stop, state="disabled")
            self.b_stop.pack(side="left", padx=6)
            ttk.Button(btns, text="Ключи Twitch", command=self.keys_dialog).pack(side="right")

            mid = ttk.Frame(self)
            mid.pack(fill="both", expand=True, padx=8)
            self.tree = ttk.Treeview(mid, columns=[c[0] for c in COLUMNS], show="headings", selectmode="extended")
            for key, text, width, anchor in COLUMNS:
                self.tree.heading(key, text=text, command=lambda k=key: self.sort_by(k))
                self.tree.column(key, width=width, anchor=anchor, stretch=(key == "title"))
            sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=sb.set)
            self.tree.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.tree.bind("<Double-1>", lambda e: self.open_selected())
            self.tree.bind("<Return>", lambda e: self.open_selected())
            self.tree.bind("<Button-3>", self._popup)

            self.menu = tk.Menu(self, tearoff=0)
            self.menu.add_command(label="Открыть клип в браузере", command=self.open_selected)
            self.menu.add_command(label="Скопировать ссылку", command=self.copy_selected)
            self.menu.add_command(label="Скачать 1080p…", command=self.download_selected)

            bottom = ttk.Frame(self, padding=8)
            bottom.pack(fill="x")
            ttk.Button(bottom, text="Открыть клип", command=self.open_selected).pack(side="left")
            ttk.Button(bottom, text="Скопировать ссылку", command=self.copy_selected).pack(side="left", padx=6)
            ttk.Button(bottom, text="Скачать 1080p…", command=self.download_selected).pack(side="left")
            ttk.Button(bottom, text="Сохранить список в CSV…", command=self.save_csv).pack(side="left", padx=6)
            self.v_status = tk.StringVar(value="Введите ник стримера и/или категорию и нажмите «Искать».")
            ttk.Label(self, textvariable=self.v_status, wraplength=1050, anchor="w").pack(fill="x", padx=10, pady=(0, 8))

            self.bind("<Return>", lambda e: self.start_search() if self.focus_get() is not self.tree else None)

        # ---- служебное
        def _creds(self):
            cid, sec = self.cfg.get("client_id", ""), self.cfg.get("client_secret", "")
            if not cid or not sec:
                self.keys_dialog()
                return None
            return Twitch(cid, sec)

        def set_busy(self, busy):
            self.busy = busy
            self.b_search.configure(state="disabled" if busy else "normal")
            self.b_stop.configure(state="normal" if busy else "disabled")
            self.b_cat.configure(state="disabled" if busy else "normal")

        def say(self, text):
            self.v_status.set(text)

        def _poll(self):
            try:
                while True:
                    kind, *args = self.q.get_nowait()
                    getattr(self, "_on_" + kind)(*args)
            except queue.Empty:
                pass
            self.after(100, self._poll)

        def stop(self):
            self.cancel.set()
            self.say("Останавливаю…")

        def _run(self, fn):
            self.cancel = threading.Event()
            self.set_busy(True)

            def wrapper():
                try:
                    fn()
                except Cancelled:
                    self.q.put(("cancelled",))
                except (TwitchError, ValueError) as e:
                    self.q.put(("error", str(e)))
                except Exception as e:  # чтобы окно не зависало молча
                    self.q.put(("error", f"Неожиданная ошибка: {e!r}"))

            threading.Thread(target=wrapper, daemon=True).start()

        # ---- ключи
        def keys_dialog(self):
            win = tk.Toplevel(self)
            win.title("Ключи Twitch")
            win.transient(self)
            win.resizable(False, False)
            f = ttk.Frame(win, padding=14)
            f.pack()
            ttk.Label(
                f, justify="left",
                text="Нужны Client ID и Client Secret вашего приложения на dev.twitch.tv/console.\n"
                     "Те же, что вы вводили в Cloudflare. Они сохранятся только на этом компьютере.",
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
            ttk.Label(f, text="Client ID:").grid(row=1, column=0, sticky="e", padx=4, pady=3)
            v1 = tk.StringVar(value=self.cfg.get("client_id", ""))
            ttk.Entry(f, textvariable=v1, width=48).grid(row=1, column=1, pady=3)
            ttk.Label(f, text="Client Secret:").grid(row=2, column=0, sticky="e", padx=4, pady=3)
            v2 = tk.StringVar(value=self.cfg.get("client_secret", ""))
            ttk.Entry(f, textvariable=v2, width=48, show="•").grid(row=2, column=1, pady=3)

            def save():
                self.cfg["client_id"] = v1.get().strip()
                self.cfg["client_secret"] = v2.get().strip()
                try:
                    save_config(self.cfg)
                except OSError as e:
                    messagebox.showerror("Ошибка", f"Не удалось сохранить настройки: {e}", parent=win)
                    return
                win.destroy()
                self.say("Ключи сохранены.")

            ttk.Button(f, text="Сохранить", command=save).grid(row=3, column=1, sticky="e", pady=(10, 0))

        # ---- категории
        def find_categories(self):
            text = self.v_cat.get().strip()
            tw = self._creds()
            if not tw:
                return
            if not text:
                messagebox.showinfo("Категория", "Впишите часть названия игры, например: just chatting")
                return
            self.say("Ищу категории…")
            self._run(lambda: self.q.put(("cats", tw.categories(text, self.cancel), True)))

        def _on_cats(self, cats, show):
            self.set_busy(False)
            self.cat_map = {c["name"]: c["id"] for c in cats}
            self.c_cat.configure(values=list(self.cat_map))
            if not cats:
                self.say("Категорий с таким названием не найдено.")
                return
            self.v_cat.set(cats[0]["name"])
            self.say(f"Найдено категорий: {len(cats)}. Выберите нужную в списке.")
            if show and len(cats) > 1:
                try:
                    self.c_cat.event_generate("<Down>")
                except Exception:
                    pass

        # ---- поиск
        def start_search(self):
            if self.busy:
                return
            login = self.v_login.get().strip().lower().replace("https://www.twitch.tv/", "").strip("/")
            cat_text = self.v_cat.get().strip()
            if not login and not cat_text:
                messagebox.showinfo("Поиск", "Укажите стримера или категорию (можно и то и другое).")
                return
            minutes = parse_period(self.v_period.get())
            if minutes is None:
                messagebox.showinfo("Период", "Не понял период. Примеры: 2 часа, 3 дня, 2 недели, 45 минут, всё время.")
                return
            try:
                min_views = max(0, int(self.v_minviews.get() or 0))
                pages = min(50, max(1, int(self.v_pages.get() or 10)))
            except ValueError:
                messagebox.showinfo("Поиск", "«Мин. просмотров» и «Страниц» должны быть числами.")
                return
            tw = self._creds()
            if not tw:
                return
            args = dict(words=self.v_words.get(), minutes=minutes, creator=self.v_creator.get(),
                        min_views=min_views, max_pages=pages, deep=self.v_deep.get())
            known = dict(self.cat_map)
            self.cfg.update({"login": login, "category": cat_text, "period": self.v_period.get()})
            try:
                save_config(self.cfg)
            except OSError:
                pass
            self.say("Ищу…")
            self.tree.delete(*self.tree.get_children())
            self.rows = []

            def job():
                game_id = ""
                if cat_text:
                    game_id = known.get(cat_text, "")
                    if not game_id:
                        cats = tw.categories(cat_text, self.cancel)
                        exact = [c for c in cats if c["name"].lower() == cat_text.lower()]
                        pick = exact[0] if exact else (cats[0] if cats else None)
                        if not pick:
                            raise ValueError(f"Категория «{cat_text}» не найдена.")
                        game_id = pick["id"]
                        self.q.put(("catpicked", pick["name"]))
                rows, info = search_clips(
                    tw, login=login, game_id=game_id,
                    progress=lambda t: self.q.put(("progress", t)), cancel=self.cancel, **args,
                )
                self.q.put(("done", rows, info, minutes))

            self._run(job)

        def _on_progress(self, text):
            self.say(text)

        def _on_catpicked(self, name):
            self.v_cat.set(name)

        def _on_done(self, rows, info, minutes):
            self.set_busy(False)
            self.rows = rows
            self.render()
            msg = f"Найдено клипов: {len(rows)} ({period_label(minutes)}). Просмотрено: {info['scanned']}."
            if info["truncated"]:
                msg += " Часть клипов не просмотрена: сузьте период, увеличьте число страниц или включите глубокий поиск."
            self.say(msg)

        def _on_cancelled(self):
            self.set_busy(False)
            self.say("Остановлено.")

        def _on_error(self, text):
            self.set_busy(False)
            self.say("Ошибка: " + text)
            messagebox.showerror("Ошибка", text)

        # ---- таблица
        def render(self):
            key, desc = self.sort_key
            getter = {
                "n": None,
                "title": lambda r: r["title"].lower(),
                "creator": lambda r: r["creator"].lower(),
                "game": lambda r: r["game"].lower(),
                "date": lambda r: r["created"],
                "views": lambda r: r["views"],
                "dur": lambda r: r["duration"],
            }[key]
            if getter:
                self.rows.sort(key=getter, reverse=desc)
            self.tree.delete(*self.tree.get_children())
            for i, r in enumerate(self.rows):
                self.tree.insert("", "end", iid=str(i), values=(
                    i + 1, r["title"], r["creator"], r["game"],
                    f'{local_text(r["created"])} ({ago(r["created"])})', r["views"], round(r["duration"]),
                ))

        def sort_by(self, key):
            old, desc = self.sort_key
            self.sort_key = (key, (not desc) if key == old else key in ("views", "date", "dur"))
            self.render()

        def selected(self):
            return [self.rows[int(i)] for i in self.tree.selection()]

        def _popup(self, event):
            row = self.tree.identify_row(event.y)
            if row:
                if row not in self.tree.selection():
                    self.tree.selection_set(row)
                self.menu.tk_popup(event.x_root, event.y_root)

        def open_selected(self):
            sel = self.selected()
            if not sel:
                self.say("Сначала выберите клип в таблице.")
                return
            webbrowser.open(sel[0]["url"])

        def copy_selected(self):
            sel = self.selected()
            if not sel:
                self.say("Сначала выберите клип в таблице.")
                return
            self.clipboard_clear()
            self.clipboard_append("\n".join(r["url"] for r in sel))
            self.say(f"Скопировано ссылок: {len(sel)}.")

        def save_csv(self):
            if not self.rows:
                self.say("Список пуст.")
                return
            path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile="clips.csv")
            if not path:
                return
            try:
                export_csv(self.rows, path)
                self.say(f"Список сохранён: {path}")
            except OSError as e:
                messagebox.showerror("Ошибка", f"Не удалось сохранить файл: {e}")

        # ---- скачивание
        def download_selected(self):
            if self.busy:
                return
            sel = self.selected()
            if not sel:
                self.say("Сначала выберите клипы в таблице (можно несколько, с Ctrl или Shift).")
                return
            folder = filedialog.askdirectory(title="Куда сохранить клипы")
            if not folder:
                return

            def job():
                done = 0
                for i, r in enumerate(sel, 1):
                    self.q.put(("progress", f"Скачиваю {i} из {len(sel)}: {r['title'][:50]}…"))
                    download_clip(
                        r, folder,
                        progress=lambda p, i=i: self.q.put(("progress", f"Скачиваю {i} из {len(sel)}: {p}%")),
                        cancel=self.cancel,
                    )
                    done += 1
                self.q.put(("downloaded", done, folder))

            self._run(job)

        def _on_downloaded(self, n, folder):
            self.set_busy(False)
            self.say(f"Скачано клипов: {n}. Папка: {folder}")
            try:
                if sys.platform.startswith("win"):
                    os.startfile(folder)  # noqa
            except Exception:
                pass


def main():
    if "--selftest" in sys.argv:
        if not HAVE_TK:
            print("tkinter недоступен")
            return 1
        app = App(ask_keys=False)
        app.update_idletasks()
        app.update()
        assert len(app.tree["columns"]) == len(COLUMNS)
        assert parse_period("2 часа") == 120
        app.destroy()
        print("SELFTEST OK")
        return 0
    if not HAVE_TK:
        print("Не найден tkinter: программа должна запускаться как clip_search.exe.")
        return 1
    try:  # чёткий текст на мониторах с масштабированием Windows
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
