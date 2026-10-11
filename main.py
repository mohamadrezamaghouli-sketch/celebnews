#!/usr/bin/env python3
"""Celebrity news Telegram bot (gossip, fashion, music, cinema).

Each run: read the feeds in sources.json, find items not seen before, translate them to
Persian (Gemini -> Groq -> Google Translate), post them to the Telegram channel with the
original link, and remember them in seen.json.

Secrets (GitHub: Settings > Secrets and variables > Actions):
  TELEGRAM_BOT_TOKEN  - token from @BotFather, like 123456789:AAF...
  TELEGRAM_CHAT_ID    - @channelusername (public) or -100xxxxxxxxxx (private)
  GEMINI_API_KEY      - optional, free key from aistudio.google.com/apikey
  GROQ_API_KEY        - optional, free key from console.groq.com

Usage:
  python main.py          normal run
  python main.py --test   check token + channel, send one test message, exit
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from calendar import timegm
from datetime import datetime, timedelta, timezone

import feedparser
import requests


_DIGITS = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}          # Persian digits
_DIGITS.update({ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})    # Arabic-Indic digits
_MINUS = {ord(c): "-" for c in "−–—‐‑‒﹣－ـ"}                            # look-alike minus signs
_COLON = {ord(c): ":" for c in "：﹕꞉∶"}                                   # look-alike colons


def clean_secret(value):
    """Secrets are often typed or pasted with a Persian keyboard, stray spaces, invisible
    right-to-left marks or quotes: normalise all of that."""
    text = (value or "").translate(_DIGITS).translate(_MINUS).translate(_COLON)
    text = "".join(ch for ch in text
                   if not ch.isspace() and unicodedata.category(ch) not in ("Cf", "Cc"))
    return text.strip("\"'`")


RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
RAW_CHAT = os.environ.get("TELEGRAM_CHAT_ID") or ""

TOKEN = clean_secret(RAW_TOKEN)
if re.match(r"(?i)bot[0-9]", TOKEN):  # copied from a URL like .../bot123456789:AAF...
    TOKEN = TOKEN[3:]
TOKEN_FIXED = False
if ":" not in TOKEN:
    # The ':' between the bot id and the secret got lost or was typed as another character
    # (for example ';' when Shift was not pressed). The secret part of a token starts with 'A'.
    _m = re.fullmatch(r"([0-9]{6,12})[^A-Za-z0-9_-]?(A[A-Za-z0-9_-]{30,})", TOKEN)
    if _m:
        TOKEN = _m.group(1) + ":" + _m.group(2)
        TOKEN_FIXED = True
if TOKEN.count(":") == 1:
    # Junk glued to the token while copying (a letter of 'bot', '/', '.', quotes...).
    # The bot id before ':' is digits only; the secret after it uses letters, digits, '_' and '-'.
    _id, _secret = TOKEN.split(":")
    _id2 = re.sub(r"[^0-9]", "", _id)
    _secret2 = re.sub(r"[^A-Za-z0-9_-]", "", _secret)
    if (_id2, _secret2) != (_id, _secret) and 6 <= len(_id2) <= 12 and len(_secret2) >= 30:
        TOKEN = _id2 + ":" + _secret2
        TOKEN_FIXED = True
if not re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{30,}", TOKEN):
    # Token buried in longer text, e.g. a whole URL: https://api.telegram.org/bot123:AAF.../getUpdates
    _m = re.search(r"(?<![0-9])([0-9]{6,12}):(A[A-Za-z0-9_-]{30,})", TOKEN)
    if _m:
        TOKEN = _m.group(1) + ":" + _m.group(2)
        TOKEN_FIXED = True
CHAT_ID = clean_secret(RAW_CHAT)
CHAT_ID_FIXED = False
if re.fullmatch(r"[0-9]+-", CHAT_ID):  # right-to-left typing put the minus sign at the end
    CHAT_ID = "-" + CHAT_ID[:-1]
    CHAT_ID_FIXED = True
if re.fullmatch(r"100[0-9]{8,}", CHAT_ID):  # the leading minus sign got lost while copying
    CHAT_ID = "-" + CHAT_ID
    CHAT_ID_FIXED = True

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$", re.ASCII)
CHAT_RE = re.compile(r"^(-100\d{6,}|@[A-Za-z][A-Za-z0-9_]{3,})$", re.ASCII)


def nonascii_note(raw):
    n = sum(1 for ch in raw if ord(ch) > 127)
    return " Non-English characters in the secret: %d." % n if n else ""


def token_fingerprint():
    """Safe description of the token so it can be compared with @BotFather's:
    the number before ':' is the public bot id; of the secret part only its length
    and last two characters are shown."""
    bot_id, _, secret = TOKEN.partition(":")
    return ("Fingerprint of the saved token: bot id=%s, secret part has %d characters and ends "
            "with '%s'. In @BotFather (/mybots > your bot > API Token) the number before ':' must "
            "be the same and the token must end with the same two characters."
            % (safe(bot_id, 20), len(secret), safe(secret[-2:], 2)))


def token_shape():
    """Structure of the saved token without revealing it: where the ':' is and which
    characters can never occur in a token."""
    def show(chars):
        return ", ".join(("'%s' " % ch if ch.isprintable() else "") + "U+%04X" % ord(ch)
                         for ch in chars[:5]) or "none"

    before, _, after = TOKEN.partition(":")
    bad_id, bad_secret = [], []
    for ch in before:
        if not ch.isdigit() and ch not in bad_id:
            bad_id.append(ch)
    for ch in after:
        if not re.fullmatch(r"[A-Za-z0-9_-]", ch) and ch not in bad_secret:
            bad_secret.append(ch)
    return ("Shape: %d colon(s); the id part before ':' has %d characters (%d digits; a bot id is "
            "10 digits) and the secret part after it has %d (normally 35). Non-digit characters "
            "in the id part: %s. Invalid characters in the secret part: %s."
            % (TOKEN.count(":"), len(before), sum(c.isdigit() for c in before), len(after),
               show(bad_id), show(bad_secret)))


CELEB = True
SOURCES_FILE = "sources.json"
SEEN_FILE = "seen.json"

MAX_AGE_HOURS = 24          # Persian items older than this are never posted
MAX_AGE_FOREIGN_HOURS = 12 if CELEB else 8   # foreign items: only fresh ones (avoids backlog floods)
MAX_PERSIAN_PER_RUN = 8     # per run (every ~15 min); keeps the channel readable
MAX_FOREIGN_PER_RUN = 8 if CELEB else 4      # total foreign items per run
MAX_PER_SOURCE = 2 if CELEB else 1           # per foreign source and run
WORLD_PER_RUN = 3           # at most this many "rank 3" (non-Iran, non-Middle-East) items per run
FIRST_RUN_POSTS = 8 if CELEB else 5         # on the very first run, post only the newest few
SUMMARY_CHARS = 280
TITLE_CHARS = 500
SEEN_LIMIT = 5000
PAUSE_BETWEEN_POSTS = 3     # seconds (Telegram allows ~20 msgs/min to a channel)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

GEMINI_KEY = clean_secret(os.environ.get("GEMINI_API_KEY", ""))
GROQ_KEY = clean_secret(os.environ.get("GROQ_API_KEY", ""))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash-lite"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "").strip() or "llama-3.3-70b-versatile"

# ---- Filter: skipped when the title contains one of these (hard politics / war, betting,
# shopping spam). Matching is case-insensitive. Edit freely. ----
EXCLUDE_WORDS = [
    "election", "senate", "congress", "gaza", "ukraine", "war in", "ceasefire", "tariff", "supreme court",
    "shutdown", "immigration", "sponsored", "promo code", "coupon", "best deals", "% off", "on sale",
    "shop the", "amazon prime day", "black friday", "horoscope", "nfl", "nba", "mlb", "nhl", "fantasy football",
    "point spread", "odds", "betting",
]


# ----------------------------------------------------------------- helpers

def safe(value, limit=200):
    """Outside text made safe for the Actions log: one line, no '::' workflow commands."""
    return re.sub(r"\s+", " ", str(value)).replace("::", ": :")[:limit]


def alert(msg):
    """Log line plus a GitHub annotation, which is shown at the top of the run page
    even when the browser fails to load the step logs."""
    line = re.sub(r"\s+", " ", msg).strip()
    print(line)
    print("::error title=News bot::%s" % line.replace("%", "%25"))


def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" .,،؛:-") + "…"


def make_key(source_name, ident):
    """Short stable fingerprint of an item (keeps seen.json small)."""
    return hashlib.sha1(("%s|%s" % (source_name, ident)).encode("utf-8")).hexdigest()[:16]


def load_sources():
    try:
        with open(SOURCES_FILE, encoding="utf-8") as f:
            sources = json.load(f)
        if not isinstance(sources, list):
            raise ValueError("top level must be a list")
        return sources
    except (OSError, ValueError) as exc:
        sys.exit("sources.json is missing or invalid: %s" % exc)


def load_seen():
    """Returns (list_of_seen_keys, first_run)."""
    if not os.path.exists(SEEN_FILE):
        return [], True
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data, False
    except (OSError, ValueError):
        pass
    print("WARNING: seen.json is unreadable - starting fresh.")
    return [], True


def save_seen(seen):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen[-SEEN_LIMIT:], f, ensure_ascii=False, indent=1)
    os.replace(tmp, SEEN_FILE)


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(timegm(t), tz=timezone.utc)
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------- filters

def norm_text(text):
    return unicodedata.normalize("NFKC", text or "").lower()


def compile_words(words):
    """Words must start at a word boundary (and end at one when very short)."""
    parts = []
    for w in words:
        w = re.escape(norm_text(w)).replace("\\ ", " ")
        parts.append(r"\b" + w + (r"\b" if len(w) <= 4 else ""))
    return re.compile("|".join(parts))


EXCLUDE_RE = compile_words(EXCLUDE_WORDS)


def classify(item):
    """Returns (rank, keep): rank = source priority (1 first), keep = False for skipped items."""
    return item.get("priority", 3), not EXCLUDE_RE.search(norm_text(item["title"]))


# ------------------------------------------------------------------- feeds

def fetch_feed(source):
    resp = requests.get(source["url"], headers=HEADERS, timeout=25)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        ctype = re.sub(r"[^\w/;=.+-]", "", resp.headers.get("Content-Type", ""))[:60]
        raise ValueError(
            "no feed entries found (HTTP %s, content-type %s) - the site may be "
            "blocking this server, or the URL changed" % (resp.status_code, ctype or "unknown"))
    items = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        ident = e.get("id") or link
        summary = clean_text(e.get("summary") or e.get("description"))
        items.append({
            "id": ident,
            "key": make_key(source["name"], ident),
            "title": title,
            "summary": shorten(summary, SUMMARY_CHARS),
            "link": link,
            "ts": entry_time(e),
            "name": source["name"],
            "label": source.get("label", ""),
            "priority": source.get("priority", 3),
            "lang": source.get("lang", "fa"),
            "strict": bool(source.get("strict", False)),
        })
    return items


def format_post(item):
    title = shorten(item["title"], TITLE_CHARS)
    parts = ["<b>%s</b>" % html.escape(title)]
    summary = item["summary"]
    # Many feeds repeat the title as the summary - skip it in that case.
    if summary and not summary.startswith(title[:30]):
        parts.append(html.escape(summary))
    source_line = item["name"]
    if item["label"]:
        source_line += " · " + item["label"]
    if item.get("translated"):
        source_line += " · ترجمه‌ی ماشینی"
    parts.append("📰 " + html.escape(source_line))
    parts.append('<a href="%s">لینک خبر اصلی</a>' % html.escape(item["link"], quote=True))
    return "\n\n".join(parts)


# ------------------------------------------------------------- translation

HAS_PERSIAN = re.compile("[\u0600-\u06FF]")
TRANSLATE_PROMPT = (
    "Translate each string in the JSON array below into fluent, neutral Persian (Farsi) as used in "
    "news writing. Keep names, numbers and meaning exact; do not add, explain or editorialise. "
    "Reply with ONLY a JSON array of the same length, in the same order.\n\n%s")


def parse_json_array(text, n):
    """Pulls a list of n non-empty Persian strings out of a model reply (or None)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list) or len(data) != n:
        return None
    out = [str(x).strip() for x in data]
    return out if all(out) else None


def translate_gemini(texts):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % GEMINI_MODEL)
    body = {"contents": [{"parts": [{"text": TRANSLATE_PROMPT % json.dumps(texts, ensure_ascii=False)}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
    r = requests.post(url, json=body, headers={"x-goog-api-key": GEMINI_KEY}, timeout=60)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return parse_json_array("".join(p.get("text", "") for p in parts), len(texts))


def translate_groq(texts):
    body = {"model": GROQ_MODEL, "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": TRANSLATE_PROMPT % json.dumps(
                {"translations_input": texts}, ensure_ascii=False)
                + '\n(Return the object {"translations": [...]}.)'}]}
    r = requests.post("https://api.groq.com/openai/v1/chat/completions", json=body,
                      headers={"Authorization": "Bearer " + GROQ_KEY}, timeout=60)
    r.raise_for_status()
    return parse_json_array(r.json()["choices"][0]["message"]["content"], len(texts))


def translate_google(texts):
    """Free, key-less Google Translate web endpoint (last resort)."""
    out = []
    for t in texts:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "auto", "tl": "fa", "dt": "t", "q": t},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        out.append("".join(seg[0] for seg in r.json()[0] if seg and seg[0]).strip())
    return out if all(out) else None


def translate_texts(texts):
    """Persian translation of a list of strings: Gemini, then Groq, then Google Translate.
    Returns (list, engine) or (None, None)."""
    engines = []
    if GEMINI_KEY:
        engines.append(("Gemini", translate_gemini))
    if GROQ_KEY:
        engines.append(("Groq", translate_groq))
    engines.append(("Google", translate_google))
    for name, fn in engines:
        try:
            result = fn(texts)
        except Exception as exc:  # network, quota (429), bad JSON ...
            # Do not print exception text (URLs can contain keys).
            print("Translation via %s failed: %s" % (name, type(exc).__name__))
            continue
        if result and len(result) == len(texts) and all(HAS_PERSIAN.search(t) for t in result):
            return result, name
        print("Translation via %s returned an unusable answer." % name)
    return None, None


def translate_items(items):
    """Translates title + summary of non-Persian items in one request.
    Returns the items that were translated; the others are left for the next run."""
    todo = [i for i in items if i.get("lang", "fa") != "fa"]
    done = [i for i in items if i.get("lang", "fa") == "fa"]
    if not todo:
        return done
    texts = []
    for i in todo:
        texts.append(shorten(i["title"], TITLE_CHARS))
        texts.append(i["summary"] or "-")
    result, engine = translate_texts(texts)
    if not result:
        print("No translator available - %d foreign item(s) will be retried next run." % len(todo))
        return done
    print("Translated %d foreign item(s) with %s." % (len(todo), engine))
    for k, i in enumerate(todo):
        i["title"] = result[2 * k]
        summ = result[2 * k + 1]
        i["summary"] = "" if summ.strip(" -") == "" else summ
        i["translated"] = True
        done.append(i)
    return done


# ---------------------------------------------------------------- telegram

def tg(method, payload=None):
    """Calls the Bot API. Returns (http_status_or_None, json_dict)."""
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    try:
        r = requests.post(url, json=payload or {}, timeout=25)
    except requests.RequestException as exc:
        # Never print the exception text: it contains the URL (and the token).
        print("Telegram request failed: %s" % type(exc).__name__)
        return None, {}
    try:
        data = r.json()
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    return r.status_code, data


def error_text(status, data):
    return "%s %s" % (status, safe(data.get("description", "")))


def send_message(text, preview_url=None):
    """Returns 'ok', 'skip' (this one message is bad), 'fatal' (token/channel
    problem) or 'retry' (temporary problem - try again next run)."""
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": ({"url": preview_url, "prefer_large_media": True}
                                 if preview_url else {"is_disabled": True}),
    }
    for _ in range(3):
        status, data = tg("sendMessage", payload)
        if status == 200:
            return "ok"
        if status is None:
            return "retry"
        if status == 429:
            params = data.get("parameters")
            wait = params.get("retry_after", 5) if isinstance(params, dict) else 5
            time.sleep(min(int(wait), 60) + 1)
            continue
        alert("Telegram error %s" % error_text(status, data))
        if status in (401, 403, 404):
            return "fatal"
        desc = str(data.get("description", "")).lower()
        if status == 400 and ("chat not found" in desc or "chat_id" in desc or "peer_id" in desc):
            return "fatal"
        if status == 400:
            return "skip"
        return "retry"
    return "retry"


def config_problems():
    """Offline sanity check of the two secrets (nothing secret is printed)."""
    problems = []
    if not TOKEN:
        problems.append("secret TELEGRAM_BOT_TOKEN is empty or missing (check its exact name).")
    elif not TOKEN_RE.match(TOKEN):
        hint = ""
        if re.fullmatch(r"-?\d+|@\w+", TOKEN):
            hint = " It looks like a chat id - the two secrets may be swapped."
        problems.append("secret TELEGRAM_BOT_TOKEN has a wrong format (length %d). %s%s%s "
                        "Expected like 1234567890:AAF... (digits, a colon, 35 letters or digits)."
                        % (len(TOKEN), token_shape(), hint, nonascii_note(RAW_TOKEN)))
    if not CHAT_ID:
        problems.append("secret TELEGRAM_CHAT_ID is empty or missing (check its exact name).")
    elif not CHAT_RE.match(CHAT_ID):
        hint = ""
        if TOKEN_RE.match(CHAT_ID):
            hint = " It looks like a bot token - the two secrets may be swapped."
        elif re.fullmatch(r"-?\d+", CHAT_ID):
            hint = " A channel id must start with -100."
        problems.append("secret TELEGRAM_CHAT_ID has a wrong format (length %d; a private channel "
                        "looks like -1001234567890, a public one like @name).%s%s"
                        % (len(CHAT_ID), hint, nonascii_note(RAW_CHAT)))
    return problems


def self_check():
    """Verifies token, channel and admin rights; prints a plain diagnosis."""
    status, data = tg("getMe")
    if status is None:
        print("CHECK: cannot reach api.telegram.org from this server.")
        return False
    if status == 401:
        alert("CHECK token: REJECTED (401). The value in secret TELEGRAM_BOT_TOKEN is wrong or was "
              "revoked. Copy it again from @BotFather (/mybots > your bot > API Token) and update the secret. "
              + token_fingerprint())
        return False
    if status != 200 or not data.get("ok"):
        alert("CHECK token: unexpected answer: %s" % error_text(status, data))
        return False
    bot = data.get("result") or {}
    print("CHECK token: OK (bot @%s)" % safe(bot.get("username", "?")))

    status, data = tg("getChat", {"chat_id": CHAT_ID})
    if status != 200 or not data.get("ok"):
        alert("CHECK channel: FAILED (%s). Wrong TELEGRAM_CHAT_ID, or the bot was never added "
              "to the channel as an administrator." % error_text(status, data))
        return False
    print("CHECK channel: OK (type=%s)" % safe((data.get("result") or {}).get("type", "?")))

    status, data = tg("getChatMember", {"chat_id": CHAT_ID, "user_id": bot.get("id")})
    if status == 200 and data.get("ok"):
        member = data.get("result") or {}
        if member.get("status") not in ("administrator", "creator"):
            alert("CHECK admin: the bot is NOT an administrator of the channel (status=%s)."
                  % safe(member.get("status", "?")))
            return False
        if member.get("can_post_messages") is False:
            alert("CHECK admin: the bot is an admin but lacks the 'Post messages' permission.")
            return False
        print("CHECK admin: OK")
    else:
        print("CHECK admin: could not verify (%s)" % error_text(status, data))
    return True


# -------------------------------------------------------------------- main

def main():
    if CHAT_ID_FIXED:
        print("::warning title=News bot::Secret TELEGRAM_CHAT_ID is missing the leading minus "
              "sign; it was fixed automatically for this run. Please edit the secret so that "
              "it starts with -100.")
    if TOKEN_FIXED:
        print("::warning title=News bot::Secret TELEGRAM_BOT_TOKEN had a missing or wrong ':' "
              "between the bot id and the secret part; it was repaired for this run. Please "
              "paste the token again from @BotFather.")
    problems = config_problems()
    if problems:
        for p in problems:
            alert("CONFIG PROBLEM: " + p)
        sys.exit(1)

    if "--test" in sys.argv:
        if not self_check():
            sys.exit(1)
        result = send_message("✅ ربات کانال سلبریتی فعال است و به کانال وصل شد." if CELEB
                              else "✅ ربات خبر فعال است و به کانال وصل شد.")
        print("Test message: %s" % result)
        sys.exit(0 if result == "ok" else 1)


    sources = load_sources()
    seen, first_run = load_seen()
    seen_set = set(seen)

    now = datetime.now(timezone.utc)
    cutoff_fa = now - timedelta(hours=MAX_AGE_HOURS)
    cutoff_foreign = now - timedelta(hours=MAX_AGE_FOREIGN_HOURS)

    all_items, failed, active = [], [], 0
    for source in sources:
        if not source.get("enabled", True):
            continue
        active += 1
        try:
            items = fetch_feed(source)
            all_items.extend(items)
            print("OK   %s: %d items" % (source["name"], len(items)))
        except Exception as exc:  # keep going if one feed breaks
            failed.append(source["name"])
            print("FAIL %s: %s" % (source["name"], safe(exc)))

    if active == 0:
        sys.exit("No enabled sources in sources.json.")
    if len(failed) == active:
        alert("All %d feeds failed - nothing processed, state not saved." % active)
        sys.exit(1)  # red X in GitHub (and an e-mail to you)

    # One entry per item; seen.json from older versions stored the raw id, so accept both.
    unique = {}
    for i in all_items:
        unique.setdefault(i["key"], i)
    new_items = [i for i in unique.values()
                 if i["key"] not in seen_set and i["id"] not in seen_set]

    # Old or not-important items are marked as seen without posting them.
    fresh = []
    dropped = 0
    for i in new_items:
        cutoff = cutoff_fa if i["lang"] == "fa" else cutoff_foreign
        i["rank"], keep = classify(i)
        if i["ts"] < cutoff or not keep:
            seen.append(i["key"])
            dropped += 1
        else:
            fresh.append(i)
    print("Filter: %d new, %d dropped (old / not important), %d candidates." % (
        len(new_items), dropped, len(fresh)))

    if first_run:
        fresh.sort(key=lambda i: i["ts"], reverse=True)
        to_post = translate_items(list(reversed(fresh[:FIRST_RUN_POSTS])))
        # Everything not posted now counts as old news; posted items are added
        # to "seen" only after Telegram accepts them (see below).
        posting = {i["key"] for i in to_post}
        seen.extend(i["key"] for i in fresh if i["key"] not in posting)
    else:
        # Persian agencies first, then foreign ones; within each: Iran, Middle East, world.
        persian = sorted((i for i in fresh if i["lang"] == "fa"), key=lambda i: (i["rank"], i["ts"]))
        foreign = sorted((i for i in fresh if i["lang"] != "fa"), key=lambda i: (i["rank"], i["ts"]))
        to_post, world, used = [], 0, []
        for i in persian:
            if len(to_post) >= MAX_PERSIAN_PER_RUN:
                break
            if i["rank"] == 3:
                if world >= WORLD_PER_RUN:
                    continue
                world += 1
            to_post.append(i)
        foreign_pick = []
        for i in foreign:  # at most one item per foreign source per run, for variety
            if len(foreign_pick) >= MAX_FOREIGN_PER_RUN:
                break
            if used.count(i["name"]) >= MAX_PER_SOURCE or (not CELEB and i["rank"] == 3 and world >= WORLD_PER_RUN):
                continue
            used.append(i["name"])
            world += (not CELEB) and i["rank"] == 3
            foreign_pick.append(i)
        to_post += translate_items(foreign_pick)
        to_post.sort(key=lambda i: (i["lang"] != "fa", i["rank"], i["ts"]))

    posted = skipped = 0
    stop_reason = None
    for item in to_post:
        result = send_message(format_post(item), item["link"] if CELEB else None)
        if result == "ok":
            posted += 1
            seen.append(item["key"])
            time.sleep(PAUSE_BETWEEN_POSTS)
        elif result == "skip":
            skipped += 1
            seen.append(item["key"])  # a message Telegram refuses must not block the queue
            print("Skipped one item from %s: Telegram rejected the message." % item["name"])
        else:
            stop_reason = result
            print("Stopping this run (%s)." % result)
            break

    # Nothing reached Telegram: do NOT save state, or these items would be lost.
    if stop_reason and posted == 0 and skipped == 0:
        alert("Nothing could be posted - state not saved, the items will be retried.")
        if stop_reason == "fatal":
            self_check()  # explains what is wrong
        sys.exit(1)

    save_seen(seen)
    print("Done. posted=%d, skipped=%d, new=%d, failed feeds=%s"
          % (posted, skipped, len(new_items), failed or "none"))
    if stop_reason == "fatal":
        sys.exit(1)


if __name__ == "__main__":
    main()
