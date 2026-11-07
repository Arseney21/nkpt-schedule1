#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрапер расписания колледжа (сайт на «Экспресс-расписание»).

1. Открывает bg.htm и собирает все ссылки вида bgNN.htm (текст ссылки = название группы).
2. Для каждой группы открывает cgNN.htm (текущее расписание на 2 недели) и разбирает его.
3. Сохраняет всё в docs/schedule.json:
   {"updated": "...", "groups": ["МТ-24/9", ...],
    "schedules": {"МТ-24/9": {"days": [...]}, ...}, "bells": {...}}

Только стандартная библиотека Python — ничего устанавливать (pip) не нужно.
"""

import json
import re
import sys
import time
import datetime as dt
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

# Первый адрес — punycode-вариант нкпт.рф (тот же сайт, но надёжнее для Python),
# второй — обычный адрес. Пробуются по очереди.
BASES = [
    "https://xn--j1afhm.xn--p1ai/raspisanie-zanyatiy/raspisanie-nemirovicha/www/",
    "https://нкпт.рф/raspisanie-zanyatiy/raspisanie-nemirovicha/www/",
]

# Страницы со списком групп. bg.htm — основной список; cg.htm — запасной.
GROUP_LIST_PAGES = [("bg.htm", "bg"), ("cg.htm", "cg")]

# На страницах расписания НЕТ времени пар, поэтому используем это расписание звонков.
# Если на странице время встретится — оно имеет приоритет.
# ⚠ Поправьте под ваш колледж, если время другое.
BELLS = {
    "1": "08:30-10:00",
    "2": "10:10-11:40",
    "3": "12:10-13:40",
    "4": "13:50-15:20",
    "5": "15:30-17:00",
    "6": "17:10-18:40",
}

OUT_FILE = Path(__file__).resolve().parent / "docs" / "schedule.json"
USER_AGENT = "Mozilla/5.0 (compatible; ScheduleBot/1.0; +github-actions)"
TIMEOUT = 30
RETRIES = 3
PAUSE_BETWEEN = 0.4  # секунд между запросами, чтобы не нагружать сайт

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def log(*args):
    print(*args, flush=True)


# --------------------------------------------------------------------------
# Загрузка страниц
# --------------------------------------------------------------------------

def decode_bytes(raw, header_charset=None):
    encodings = []
    if header_charset:
        encodings.append(header_charset)
    encodings += ["utf-8", "cp1251"]
    for enc in encodings:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch(rel_url):
    """Скачивает страницу (пробует все зеркала и делает повторы)."""
    last_error = None
    for attempt in range(RETRIES):
        for base in BASES:
            try:
                req = Request(
                    base + rel_url,
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
                )
                with urlopen(req, timeout=TIMEOUT) as resp:
                    raw = resp.read()
                    charset = resp.headers.get_content_charset()
                return decode_bytes(raw, charset)
            except Exception as exc:  # noqa: BLE001 — нужно поймать всё
                last_error = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("не удалось скачать %s: %s" % (rel_url, last_error))


# --------------------------------------------------------------------------
# Разбор HTML (на стандартном html.parser)
# --------------------------------------------------------------------------

def clean(text):
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


class Tokenizer(HTMLParser):
    """
    Превращает страницу в плоский поток токенов в порядке следования:
      ("text", "строка")                      — обычный текст вне ссылок
      ("link", href, "текст", "title")        — ссылка <a>
    Не зависит от того, как именно сверстаны таблицы.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self._href = None
        self._title = ""
        self._buf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            self._flush_link()
            attrs = dict(attrs)
            self._href = attrs.get("href") or ""
            self._title = attrs.get("title") or ""
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a":
            self._flush_link()

    def handle_data(self, data):
        if self._skip:
            return
        if self._href is not None:
            self._buf.append(data)
        else:
            s = clean(data)
            if s:
                self.tokens.append(("text", s))

    def _flush_link(self):
        if self._href is not None:
            self.tokens.append(("link", self._href, clean("".join(self._buf)), self._title))
        self._href = None
        self._title = ""
        self._buf = []

    def finish(self):
        self.close()
        self._flush_link()
        return self.tokens


def tokenize(html):
    t = Tokenizer()
    t.feed(html)
    return t.finish()


# --------------------------------------------------------------------------
# Список групп
# --------------------------------------------------------------------------

def parse_group_list(html, prefix):
    """Ищет в любых <a> ссылки вида bgNN.htm (или cgNN.htm). Возвращает [(id, название)]."""
    rx = re.compile(r"(?:^|/)" + prefix + r"(\d+)\.htm", re.I)
    found = []
    seen = set()
    for tok in tokenize(html):
        if tok[0] != "link":
            continue
        m = rx.search(tok[1])
        name = tok[2]
        if not m or not name:
            continue
        gid = m.group(1)
        if gid in seen:
            continue
        seen.add(gid)
        found.append((gid, name))
    return found


def get_groups():
    for page, prefix in GROUP_LIST_PAGES:
        try:
            groups = parse_group_list(fetch(page), prefix)
        except Exception as exc:  # noqa: BLE001
            log("! Не удалось получить список групп со страницы %s: %s" % (page, exc))
            continue
        if groups:
            log("Список групп (%s): найдено %d" % (page, len(groups)))
            return groups
        log("! На странице %s ссылок на группы не найдено" % page)
    return []


# --------------------------------------------------------------------------
# Страница расписания группы
# --------------------------------------------------------------------------

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
WD_RE = re.compile(
    r"^(Пн|Вт|Ср|Чт|Пт|Сб|Вс)(?:\s*-\s*(\d+))?(?:\s+(\d{1,2}))?"
    r"(?:\s+(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2}))?$",
    re.I,
)
PAIR_RE = re.compile(
    r"^(\d{1,2})(?:\s+(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2}))?$"
)
TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})$")
SUBJECT_HREF = re.compile(r"(?:^|/)j\d+\.htm", re.I)
ROOM_HREF = re.compile(r"(?:^|/)ca\d+\.htm", re.I)
TEACHER_HREF = re.compile(r"(?:^|/)cp\d+\.htm", re.I)


def fmt_time(h1, m1, h2, m2):
    return "%02d:%s-%02d:%s" % (int(h1), m1, int(h2), m2)


def link_kind(href, title):
    t = (title or "").lower()
    if "журнал" in t or SUBJECT_HREF.search(href):
        return "subject"
    if "аудитор" in t or ROOM_HREF.search(href):
        return "room"
    if "преподават" in t or TEACHER_HREF.search(href):
        return "teacher"
    return None


def split_kind(subject):
    """«Иностранный язык (Лек)» -> («Иностранный язык», «Лек»)."""
    m = re.match(r"^(.*?)\s*\(([^()]{1,25})\)\s*$", subject)
    if m and m.group(1):
        return m.group(1).strip(), m.group(2).strip()
    return subject, ""


def add_unique(current, value):
    if not value:
        return current
    if not current:
        return value
    if value in [p.strip() for p in current.split(",")]:
        return current
    return current + ", " + value


def parse_schedule(html):
    """
    Разбирает страницу cgNN.htm по потоку токенов:
      дата (26.10.2026) -> день недели (Пн-1) -> номер пары (1..9)
      -> ссылки: предмет (j…), аудитория (ca…), преподаватель (cp…).
    Возвращает список дней.
    """
    tokens = tokenize(html)

    days = []
    seen_dates = set()
    state = {"day": None, "pair": None, "lesson": None}

    def start_day(d, m, y):
        try:
            date = dt.date(int(y), int(m), int(d))
        except ValueError:
            return False
        iso = date.isoformat()
        if iso in seen_dates:           # повтор той же даты — игнорируем
            state["day"] = state["pair"] = state["lesson"] = None
            return True
        seen_dates.add(iso)
        day = {
            "date": "%02d.%02d.%04d" % (date.day, date.month, date.year),
            "iso": iso,
            "weekday": WEEKDAYS[date.weekday()],
            "week": None,
            "pairs": [],
        }
        days.append(day)
        state["day"], state["pair"], state["lesson"] = day, None, None
        return True

    def start_pair(num, time_str=""):
        day = state["day"]
        if day is None:
            return
        n = int(num)
        pair = {"n": n, "time": time_str or "", "lessons": []}
        day["pairs"].append(pair)
        state["pair"], state["lesson"] = pair, None

    def new_lesson(subject=""):
        pair = state["pair"]
        if pair is None:
            return None
        lesson = {"subject": subject, "kind": "", "room": "", "teacher": ""}
        pair["lessons"].append(lesson)
        state["lesson"] = lesson
        return lesson

    def handle_text(s):
        if not s:
            return
        # Подвал страницы — дальше расписания нет
        if s.startswith("Обновлено") or s.startswith("Copyright"):
            if days:
                raise StopIteration
            return

        # Дата (может идти вместе с днём недели в одной строке)
        dm = DATE_RE.search(s) if len(s) <= 40 else None
        if dm and start_day(dm.group(1), dm.group(2), dm.group(3)):
            rest = clean(s[: dm.start()] + " " + s[dm.end():])
            handle_text(rest)
            return

        if state["day"] is None:
            return

        # День недели: «Пн-1» (+ возможно номер пары: «Пн-1 1»)
        wm = WD_RE.match(s)
        if wm:
            if wm.group(2) and state["day"]["week"] is None:
                state["day"]["week"] = int(wm.group(2))
            if wm.group(3):
                t = fmt_time(wm.group(4), wm.group(5), wm.group(6), wm.group(7)) if wm.group(4) else ""
                start_pair(wm.group(3), t)
            return

        # Номер пары (возможно с временем): «1» или «1 08:30-10:00»
        pm = PAIR_RE.match(s)
        if pm:
            t = fmt_time(pm.group(2), pm.group(3), pm.group(4), pm.group(5)) if pm.group(2) else ""
            start_pair(pm.group(1), t)
            return

        # Отдельное время рядом с номером пары
        tm = TIME_RE.match(s)
        if tm:
            if state["pair"] is not None and not state["pair"]["time"]:
                state["pair"]["time"] = fmt_time(tm.group(1), tm.group(2), tm.group(3), tm.group(4))
            return

        # Предмет обычным текстом (без ссылки) — на всякий случай
        if (
            state["pair"] is not None
            and not state["pair"]["lessons"]
            and len(s) >= 3
            and re.search(r"[A-Za-zА-Яа-яЁё]", s)
        ):
            new_lesson(s)

    try:
        for tok in tokens:
            if tok[0] == "text":
                handle_text(tok[1])
                continue

            _, href, text, title = tok
            kind = link_kind(href, title)

            if kind is None:
                # Обычная ссылка (например, дата-ссылка) — обрабатываем её текст как текст
                handle_text(text)
                continue
            if state["pair"] is None or not text:
                continue

            if kind == "subject":
                new_lesson(text)
            else:
                lesson = state["lesson"] or new_lesson("")
                key = "room" if kind == "room" else "teacher"
                lesson[key] = add_unique(lesson[key], text)
    except StopIteration:
        pass

    # Финализация: убираем пустые пары, делим «Предмет (Лек)», подставляем время звонков
    for day in days:
        pairs = []
        for pair in day["pairs"]:
            lessons = []
            for ls in pair["lessons"]:
                if not (ls["subject"] or ls["room"] or ls["teacher"]):
                    continue
                subject, kind = split_kind(ls["subject"])
                ls["subject"] = subject or "Без названия"
                ls["kind"] = kind
                lessons.append(ls)
            if not lessons:
                continue
            pair["lessons"] = lessons
            if not pair["time"]:
                pair["time"] = BELLS.get(str(pair["n"]), "")
            pairs.append(pair)
        pairs.sort(key=lambda p: p["n"])
        day["pairs"] = pairs

    return days


# --------------------------------------------------------------------------
# Основная логика
# --------------------------------------------------------------------------

def load_old():
    try:
        with open(OUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("schedules"), dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def main():
    groups = get_groups()
    if not groups:
        log("::warning::Список групп получить не удалось — schedule.json не изменён")
        return 0

    old = load_old()
    old_schedules = old.get("schedules", {}) if isinstance(old.get("schedules"), dict) else {}

    group_names = []
    schedules = {}
    ok_count = 0
    failed = []

    for i, (gid, name) in enumerate(groups):
        if name in group_names:             # на случай одинаковых названий
            name = "%s (%s)" % (name, gid)
        group_names.append(name)

        try:
            days = parse_schedule(fetch("cg%s.htm" % gid))
            if not days:
                raise ValueError("на странице не найдено ни одного дня")
            schedules[name] = {"id": gid, "days": days}
            ok_count += 1
            lessons = sum(len(p["lessons"]) for d in days for p in d["pairs"])
            log("  [ok]   %-22s cg%s.htm: дней %d, занятий %d" % (name, gid, len(days), lessons))
        except Exception as exc:  # noqa: BLE001 — одна группа не должна ронять скрипт
            failed.append(name)
            log("  [FAIL] %-22s cg%s.htm: %s" % (name, gid, exc))
            if name in old_schedules:        # оставляем прошлые данные этой группы
                schedules[name] = old_schedules[name]

        if i < len(groups) - 1:
            time.sleep(PAUSE_BETWEEN)

    log("Успешно: %d из %d групп" % (ok_count, len(groups)))

    # Защита от сбоев: если спарсилось меньше половины — старый JSON не трогаем
    if ok_count * 2 < len(groups):
        log("::warning::Спарсилось меньше половины групп (%d из %d) — schedule.json не перезаписан"
            % (ok_count, len(groups)))
        return 0

    payload = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "groups": group_names,
        "schedules": schedules,
        "bells": BELLS,
    }

    # Если ничего не изменилось — файл не трогаем (меньше лишних коммитов)
    def comparable(d):
        return {k: v for k, v in d.items() if k != "updated"}

    if old and comparable(old) == comparable(payload):
        log("Изменений нет — schedule.json оставлен как есть")
        return 0

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(OUT_FILE)
    log("Сохранено: %s (групп: %d)" % (OUT_FILE, len(group_names)))
    if failed:
        log("Не обновились (взяты старые данные, если были): " + ", ".join(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
