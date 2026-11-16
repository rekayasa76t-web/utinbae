import hashlib
import html
import html as html_module
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

# ============================================================
#  PATH & KONSTANTA
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
COUNTRY_FILE = BASE_DIR / "country.json"
LANG_FILE = BASE_DIR / "bahasa.json"
DATA_DIR = BASE_DIR / "data"
EMOJI_FILE = DATA_DIR / "id_emoji_premium.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_SEEN = 5000
SEND_RETRIES = 2
SEND_RETRY_DELAY = 2
BURST_DELAY = 1.5
GROUP_DELAY = 0.8
MAX_BACKOFF = 300
BROADCAST_WORKERS = 1
FETCH_RETRIES = 3
FETCH_RETRY_DELAY = 2

# Emoji ID yang TIDAK ada di id_emoji_premium.json — hardcoded fallback
PHONE_EMOJI_ID_HARDCODED = "5028776056388061520"    # 🟣 tengah nomor
WA_EMOJI_ID_HARDCODED = "5334998226636390258"        # 📱 WhatsApp
APP_EMOJI_ID_HARDCODED = "5850692390066591154"       # 📩 SMS biasa
COPY_OTP_EMOJI_ID_HARDCODED = "5296587908906511469"  # 📋 salin

# Key JSON untuk tombol (dari file kamu)
NUMBER_BTN_KEY = "get_numbers_btn"
DISCUSSION_BTN_KEY = "help_support_btn"

# Fallback tombol kalau JSON belum ada
NUMBER_BTN_EMOJI_FALLBACK = "5298853345241358103"
DISCUSSION_BTN_EMOJI_FALLBACK = "5375410054960795477"

SENDER_MAP = {
    "12403486504": "WhatsApp",
}

# Default filter bahasa (bisa di-override via config.json -> filter)
DEFAULT_BLOCKED_LANGS = {"ID"}
BLOCKED_WARNING = "JANGAN MENGGUNAKAN BAHASA INDONESIA"
BLOCKED_COPY_TEXT = "Jangan menggunakan bahasa Indonesia"

# Status return send_to_one_group
SEND_OK = "ok"
SEND_FAIL = "fail"
SEND_PERMANENT = "permanent"

# Kata kunci error Telegram yang sifatnya PERMANEN (grup hilang / bot di-kick)
PERMANENT_ERR_KEYWORDS = (
    "chat not found",
    "bot was kicked",
    "bot is not a member",
    "user is deactivated",
    "peer_id_invalid",
    "chat_write_forbidden",
)


# ============================================================
#  PANEL DEFINISI
# ============================================================
PANELS = {
    "thirdwave": {
        "label": "ThirdWave",
        "base_url": "https://thirdwave.im/api/v1/traffic",
        "auth": "query-key",
    },
    "cnsms": {
        "label": "CNSMS",
        "base_url": "http://158.51.78.74/api/messages/cdr",
        "auth": "x-api-token",
    },
    "jacksms": {
        "label": "JackSMS",
        "base_url": "https://jackx.site/api/messages/cdr",
        "auth": "x-api-token",
    },
    "unixsms": {
        "label": "Unix SMS",
        "base_url": "https://agent-api.unixsms.com/v1/cdr",
        "auth": "bearer",
    },
}
DEFAULT_PANEL = "thirdwave"

STATE_LOCK = threading.RLock()
CONFIG_LOCK = threading.RLock()
TG_LOCK = threading.Lock()


# ============================================================
#  RATE LIMITER (global, lintas worker)
# ============================================================
class RateLimiter:
    def __init__(self, min_interval):
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            delay = self.min_interval - (now - self.last)
            if delay > 0:
                time.sleep(delay)
            self.last = time.time()


TG_RATE_LIMITER = RateLimiter(GROUP_DELAY)


# ============================================================
#  LOAD JSON HELPER
# ============================================================
def load_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _int_or(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============================================================
#  LOAD CONFIG
# ============================================================
CONFIG = load_json(CONFIG_FILE)
if not isinstance(CONFIG, dict):
    raise SystemExit("config.json belum dibuat / tidak valid.")

TG = CONFIG.get("telegram", {}) or {}
TW = CONFIG.get("thirdwave", {}) or {}
MSG = CONFIG.get("message", {}) or {}
FILTER = CONFIG.get("filter", {}) or {}

BOT_TOKEN = str(TG.get("bot_token", "")).strip()
ADMIN_IDS = {str(x).strip() for x in (TG.get("admin_ids") or []) if str(x).strip()}

POLL_SECONDS = max(3, _int_or(TW.get("poll_seconds"), 5))
REQUEST_TIMEOUT = max(5, _int_or(TW.get("request_timeout"), 15))
PAGE_SIZE = min(100, max(1, _int_or(TW.get("page_size"), 25)))

FIRST_RUN_MODE = str(TW.get("first_run_mode", "mark_only")).lower()
if FIRST_RUN_MODE not in ("mark_only", "send_all"):
    FIRST_RUN_MODE = "mark_only"

NUMBER_URL = str(MSG.get("number_url", "https://t.me/numberotp"))
OWNER_URL = str(MSG.get("owner_url", "https://t.me/hbzil"))
DISCUSSION_URL = str(MSG.get("discussion_url", OWNER_URL))

BLOCKED_LANGS = {
    str(x).strip().upper()
    for x in (FILTER.get("blocked_langs") or DEFAULT_BLOCKED_LANGS)
    if str(x).strip()
}

if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
    raise SystemExit("telegram.bot_token belum diisi di config.json.")

COUNTRIES = load_json(COUNTRY_FILE, {}) or {}
STATES = {}


# ============================================================
#  EMOJI PREMIUM
# ============================================================
EMOJI = load_json(EMOJI_FILE, {}) or {}

if not EMOJI:
    print(f"[WARN] {EMOJI_FILE.name} tidak ditemukan — emoji premium dinonaktifkan")


def e(key):
    eid = str(EMOJI.get(key) or "").strip()
    if not eid.isdigit():
        return None
    return eid


def e_or(key, fallback):
    return e(key) or fallback


# Emoji yang hardcoded (tidak ada di JSON)
PHONE_EMOJI_ID = PHONE_EMOJI_ID_HARDCODED
WA_EMOJI_ID = WA_EMOJI_ID_HARDCODED
APP_EMOJI_ID = APP_EMOJI_ID_HARDCODED
COPY_OTP_EMOJI_ID = COPY_OTP_EMOJI_ID_HARDCODED

# Tombol: baca dari JSON, fallback hardcoded
NUMBER_BTN_EMOJI_ID = e_or(NUMBER_BTN_KEY, NUMBER_BTN_EMOJI_FALLBACK)
DISCUSSION_BTN_EMOJI_ID = e_or(DISCUSSION_BTN_KEY, DISCUSSION_BTN_EMOJI_FALLBACK)


def pe(emoji_char, key, fallback_char="•"):
    eid = e(key)
    if not eid:
        return html.escape(fallback_char or emoji_char)
    return (
        f'<tg-emoji emoji-id="{html.escape(eid, quote=True)}">'
        f'{html.escape(emoji_char)}</tg-emoji>'
    )


# ============================================================
#  LOAD BAHASA
# ============================================================
LANG_DATA = load_json(LANG_FILE, None)
if not isinstance(LANG_DATA, dict):
    print(f"[WARN] {LANG_FILE.name} tidak ditemukan / invalid, pakai fallback minimal.")
    LANG_DATA = {
        "default": "EN",
        "scripts": {},
        "keywords": {"EN": ["your", "you", "share", "with", "this", "the"]},
    }

DEFAULT_LANG = str(LANG_DATA.get("default") or "EN").upper()
KEYWORDS = LANG_DATA.get("keywords") or {}
SCRIPT_PATTERNS = []


def build_script_patterns():
    patterns = []
    for name, info in (LANG_DATA.get("scripts") or {}).items():
        if not isinstance(info, dict):
            continue
        pattern = info.get("pattern")
        langs = info.get("langs") or []
        if not pattern or not langs:
            continue
        try:
            patterns.append((re.compile(pattern), list(langs)))
        except re.error as exc:
            print(f"[WARN] bahasa.json script '{name}' invalid: {exc}")
    return patterns


SCRIPT_PATTERNS = build_script_patterns()


# ============================================================
#  UTIL
# ============================================================
def safe_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "").strip())[:32]
    if cleaned in (".", "..", ""):
        return ""
    return cleaned


def account_seen_file(name):
    return DATA_DIR / f"seen_{safe_name(name)}.json"


def save_config():
    with CONFIG_LOCK:
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_FILE)


def get_groups():
    raw = CONFIG.get("groups")
    if not isinstance(raw, list):
        return []
    out = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "").strip()
        if not gid:
            continue
        out.append({"id": gid, "label": str(g.get("label") or gid)})
    return out


# ============================================================
#  AKUN
# ============================================================
def load_accounts():
    raw = CONFIG.get("accounts")
    result = []
    seen_keys = set()
    if not isinstance(raw, list):
        return result
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        name = safe_name(item.get("name") or f"acc{i+1}") or f"acc{i+1}"
        key = str(item.get("api_key") or "").strip()
        panel = str(item.get("panel") or DEFAULT_PANEL).strip().lower()

        if panel not in PANELS:
            print(f"[WARN] akun '{name}' panel '{panel}' tidak dikenal, dilewati")
            continue
        if not key or key.startswith("PASTE_"):
            print(f"[WARN] akun '{name}' api_key kosong, dilewati")
            continue
        if key in seen_keys:
            print(f"[WARN] akun '{name}' api_key duplikat, dilewati")
            continue
        seen_keys.add(key)
        result.append({
            "name": name,
            "panel": panel,
            "api_key": key,
            "seen_file": account_seen_file(name),
        })
    return result


ACCOUNTS = load_accounts()
GROUPS = get_groups()

if ACCOUNTS:
    print(f"[BOOT] {len(ACCOUNTS)} akun: " +
          ", ".join(f"{a['name']}({a['panel']})" for a in ACCOUNTS))
else:
    print("[BOOT] Belum ada akun. Tambahkan lewat /addacc.")
if GROUPS:
    print(f"[BOOT] {len(GROUPS)} grup tujuan: {', '.join(g['label'] for g in GROUPS)}")
else:
    print("[WARN] Belum ada grup tujuan. Tambahkan lewat /addgc <chat_id> <label>.")
if not ADMIN_IDS:
    print("[WARN] telegram.admin_ids kosong — admin belum aktif.")
print(f"[BOOT] Bahasa: {len(KEYWORDS)} bahasa, {len(SCRIPT_PATTERNS)} script, default={DEFAULT_LANG}")
print(f"[BOOT] Bahasa diblokir: {', '.join(sorted(BLOCKED_LANGS)) or '-'}")
print(f"[BOOT] Panel tersedia: {', '.join(PANELS.keys())}")
print(f"[BOOT] Emoji premium: {len(EMOJI)} id dimuat")
print(f"[BOOT] Tombol NUMBER emoji: {NUMBER_BTN_EMOJI_ID} (key={NUMBER_BTN_KEY})")
print(f"[BOOT] Tombol DISKUSI emoji: {DISCUSSION_BTN_EMOJI_ID} (key={DISCUSSION_BTN_KEY})")
print(f"[BOOT] Delay: burst={BURST_DELAY}s grup={GROUP_DELAY}s poll={POLL_SECONDS}s workers={BROADCAST_WORKERS}")


# ============================================================
#  HELPER
# ============================================================
def clean_number(value):
    return re.sub(r"[^0-9]", "", str(value or ""))


def country_info(phone):
    number = clean_number(phone)
    for code in sorted(COUNTRIES, key=len, reverse=True):
        if number.startswith(code):
            info = COUNTRIES[code]
            return info if isinstance(info, dict) else {
                "name": str(info), "flag": "🏳️", "shortName": "UN"
            }
    return {"name": "Unknown", "flag": "🏳️", "shortName": "UN", "custom_emoji_id": ""}


def tg_custom_emoji(emoji, emoji_id):
    if not str(emoji_id or "").isdigit():
        return html.escape(emoji)
    return (
        f'<tg-emoji emoji-id="{html.escape(str(emoji_id), quote=True)}">'
        f'{html.escape(emoji)}</tg-emoji>'
    )


def _first(raw, *keys, default=""):
    for k in keys:
        v = (raw or {}).get(k)
        if v is not None and str(v).strip() != "":
            return v
    return default


# ------------------------------------------------------------
#  DETEKSI BAHASA
# ------------------------------------------------------------
def _score_languages(text, candidates):
    tokens = set(re.findall(r"\w+", str(text).lower()))
    if not tokens:
        return DEFAULT_LANG
    best_lang = DEFAULT_LANG
    best_score = 0
    for lang in candidates:
        kws = KEYWORDS.get(lang) or []
        if not kws:
            continue
        score = sum(1 for w in kws if w in tokens)
        if score > best_score:
            best_score = score
            best_lang = lang
    return best_lang if best_score >= 2 else DEFAULT_LANG


def detect_language(text):
    if not text:
        return DEFAULT_LANG
    script_candidates = set()
    for pattern, langs in SCRIPT_PATTERNS:
        if pattern.search(text):
            script_candidates.update(langs)
    if script_candidates:
        if len(script_candidates) == 1:
            return next(iter(script_candidates))
        return _score_languages(text, script_candidates)
    return _score_languages(text, set(KEYWORDS.keys()))


def is_blocked_lang(lang_code):
    return str(lang_code).upper() in BLOCKED_LANGS


class TelegramError(RuntimeError):
    def __init__(self, message, retry_after=None, status=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


def _current_bot_token():
    tg = CONFIG.get("telegram") or {}
    return str(tg.get("bot_token") or "").strip() or BOT_TOKEN


def telegram_call(method, data=None, timeout=None):
    token = _current_bot_token()
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = requests.post(url, data=data or {}, timeout=timeout or REQUEST_TIMEOUT)
        payload = response.json()
    except ValueError:
        raise TelegramError(
            f"Telegram HTTP {response.status_code}: {response.text}",
            status=response.status_code,
        )
    if not response.ok or not payload.get("ok"):
        retry_after = (payload.get("parameters") or {}).get("retry_after")
        raise TelegramError(
            f"Telegram HTTP {response.status_code}: {payload}",
            retry_after=retry_after,
            status=response.status_code,
        )
    return payload.get("result")


def send_message(chat_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True}),
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    with TG_LOCK:
        try:
            return telegram_call("sendMessage", data)
        except TelegramError as exc:
            msg = str(exc).lower()
            if "custom_emoji" in msg or "can't parse entities" in msg or "can't find end" in msg:
                plain = re.sub(r"<tg-emoji[^>]*>|</tg-emoji>", "", text)
                plain = re.sub(r"<[^>]+>", "", plain)
                plain = html_module.unescape(plain)
                data["text"] = plain
                data.pop("parse_mode", None)
                return telegram_call("sendMessage", data)
            raise
        finally:
            time.sleep(0.05)


def answer_callback(callback_id, text=None):
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    try:
        return telegram_call("answerCallbackQuery", data)
    except Exception:
        pass


# ============================================================
#  FETCH PER PANEL
# ============================================================
def _http_get(url, headers=None, params=None):
    r = requests.get(url, headers=headers or {}, params=params, timeout=45)

    if r.status_code == 429:
        ra = r.headers.get("Retry-After", "60")
        raise RuntimeError(f"rate limit (429), retry after {ra}s")
    if r.status_code == 403:
        raise RuntimeError("API access not enabled for this account (403)")
    if r.status_code == 401:
        raise RuntimeError("API key missing or invalid (401)")
    if r.status_code == 400:
        try:
            body = r.json()
            err = body.get("error") or {}
            raise RuntimeError(
                f"bad request (400): {err.get('code')} - {err.get('message')}"
            )
        except ValueError:
            raise RuntimeError(f"bad request (400): {r.text[:200]}")
    if r.status_code == 503:
        raise RuntimeError("service unavailable (503)")

    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        raise RuntimeError("invalid JSON response")


def fetch_thirdwave(api_key):
    cfg = PANELS["thirdwave"]
    data = _http_get(
        cfg["base_url"],
        params={
            "api_key": api_key,
            "limit": PAGE_SIZE,
        },
    )
    if isinstance(data, dict):
        items = data.get("items") or data.get("rows") or []
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def fetch_cnsms(api_key):
    cfg = PANELS["cnsms"]
    data = _http_get(
        cfg["base_url"],
        headers={
            "Accept": "application/json",
            "X-API-Token": api_key,
        },
    )
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        rows = data.get("data") or data.get("rows") or data.get("items") or []
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def fetch_jacksms(api_key):
    cfg = PANELS["jacksms"]
    data = _http_get(
        cfg["base_url"],
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Token": api_key,
        },
    )
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        rows = data.get("data") or data.get("rows") or data.get("items") or []
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def fetch_unixsms(api_key):
    cfg = PANELS["unixsms"]
    data = _http_get(
        cfg["base_url"],
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        params={
            "page": 1,
            "page_size": PAGE_SIZE,
        },
    )
    if isinstance(data, dict):
        inner = data.get("data") or {}
        items = inner.get("items") or data.get("items") or []
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    return []


FETCHERS = {
    "thirdwave": fetch_thirdwave,
    "cnsms": fetch_cnsms,
    "jacksms": fetch_jacksms,
    "unixsms": fetch_unixsms,
}


def fetch_panel(panel, api_key):
    fetcher = FETCHERS.get(panel)
    if not fetcher:
        raise RuntimeError(f"panel tidak dikenal: {panel}")
    return fetcher(api_key)


def fetch_panel_with_retry(panel, api_key, name=""):
    last_exc = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            return fetch_panel(panel, api_key)
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            m = re.search(r"retry after (\d+)s", msg)
            if m:
                wait = min(int(m.group(1)), 120)
                print(f"[{name}] rate limit, tidur {wait}s (attempt {attempt}/{FETCH_RETRIES})")
                time.sleep(wait)
            else:
                if attempt < FETCH_RETRIES:
                    print(f"[{name}] fetch gagal (attempt {attempt}/{FETCH_RETRIES}): {msg}")
                    time.sleep(FETCH_RETRY_DELAY)
    raise RuntimeError(f"fetch gagal setelah {FETCH_RETRIES}x: {last_exc}")


# ============================================================
#  NORMALIZE — TOLERAN MULTI NAMA FIELD
# ============================================================
def _make_item(panel, number, message, sender, received, raw_id=None,
               range_template=None, otp_hint=None, raw_debug=None):
    if not number and not message:
        keys = list((raw_debug or {}).keys())[:8]
        print(f"[DROP][{panel}] number/message kosong | keys={keys}")
        return None

    rec = re.sub(r"\.\d+", "", str(received or "")).strip()

    if raw_id:
        key = hashlib.sha1(f"{panel}|id|{raw_id}".encode("utf-8")).hexdigest()
    else:
        key = hashlib.sha1(
            f"{panel}|{number}|{rec}|{str(range_template or '')}|{str(message)[:40]}".encode("utf-8")
        ).hexdigest()

    item = {
        "id": key,
        "panel": panel,
        "number": number,
        "message": message,
        "sender": sender or "Unknown",
        "received_at": received or datetime.now(timezone.utc).isoformat(),
    }
    if otp_hint:
        item["_otp_hint"] = str(otp_hint).strip()
    return item


def normalize_thirdwave(raw):
    return _make_item(
        "thirdwave",
        str(_first(raw, "number", "destinationNumber", "destination", "phone", "msisdn")),
        str(_first(raw, "sms", "messageBody", "message", "content", "body", "text")),
        str(_first(raw, "sender_id", "sourceAddress", "sender", "cli", default="Unknown")),
        str(_first(raw, "received_at", "receivedAt", "sent_at", "sentAt", "time", "created_at")),
        raw_id=_first(raw, "id", "message_id", "messageId", "uuid", default=None),
        range_template=str(_first(raw, "range_template", "range", "operator")),
        raw_debug=raw,
    )


def normalize_cnsms(raw):
    return _make_item(
        "cnsms",
        str(_first(raw, "number", "destinationNumber", "phone", "msisdn")),
        str(_first(raw, "content", "message", "sms", "body", "text")),
        str(_first(raw, "cllr", "sender", "sender_id", "sourceAddress", default="Unknown")),
        str(_first(raw, "time", "received_at", "receivedAt", "sentAt")),
        raw_id=_first(raw, "id", "message_id", "messageId", default=None),
        range_template=str(_first(raw, "range_template", "range", "operator")),
        raw_debug=raw,
    )


def normalize_jacksms(raw):
    return _make_item(
        "jacksms",
        str(_first(raw, "number", "destinationNumber", "phone", "msisdn")),
        str(_first(raw, "message", "content", "sms", "body", "text")),
        str(_first(raw, "cllr", "sender", "sender_id", "sourceAddress", default="Unknown")),
        str(_first(raw, "time", "received_at", "receivedAt", "sentAt")),
        raw_id=_first(raw, "id", "message_id", "messageId", default=None),
        range_template=str(_first(raw, "range_template", "range", "operator")),
        raw_debug=raw,
    )


def normalize_unixsms(raw):
    return _make_item(
        "unixsms",
        str(_first(raw, "number", "destinationNumber", "phone", "msisdn")),
        str(_first(raw, "sms", "message", "content", "body", "text")),
        str(_first(raw, "cli", "sender", "sender_id", "sourceAddress", default="Unknown")),
        str(_first(raw, "message_at", "received_at", "receivedAt", "sentAt", "time")),
        raw_id=_first(raw, "id", "message_id", "messageId", default=None),
        range_template=str(_first(raw, "range_name", "range", "operator")),
        otp_hint=raw.get("extracted_code"),
        raw_debug=raw,
    )


NORMALIZERS = {
    "thirdwave": normalize_thirdwave,
    "cnsms": normalize_cnsms,
    "jacksms": normalize_jacksms,
    "unixsms": normalize_unixsms,
}


def normalize(raw, panel):
    fn = NORMALIZERS.get(panel)
    if not fn:
        return None
    return fn(raw)


# ============================================================
#  FORMAT SMS & BROADCAST
# ============================================================
def extract_otp(text):
    if not text:
        return ""
    s = str(text)
    s = s.replace("<#>", " ").replace("\\u003C#\\u003E", " ")

    s_clean = re.sub(r"https?://\S+", " ", s, flags=re.I)
    s_clean = re.sub(r"\S+@\S+", " ", s_clean)

    patterns = [
        r"(?:whatsapp(?:\s+business)?\s+code\s*:?\s*)(\d{3}[-\s]\d{3})",
        r"(?:apple\s+account\s+code\s+is\s*:?\s*)(\d{6})",
        r"(?:verification|verify|security|confirmation|one[-\s]?time|passcode|otp|code|kode|kod|mã|رمز|código|κωδικός|пароль|код|पुष्टि|कोड)[^0-9A-Za-z]{0,15}([0-9]{2,4}(?:[-\s][0-9]{2,4})+|[0-9]{3,8})",
        r"(?<!\d)(\d{3,4}[-\s]\d{3,4}(?:[-\s]\d{2,4})?)(?!\d)",
    ]
    for p in patterns:
        m = re.search(p, s_clean, flags=re.I)
        if m:
            cleaned = re.sub(r"[^0-9]", "", m.group(1))
            if cleaned:
                return cleaned
    return ""


def format_sms(sms):
    phone = clean_number(sms.get("number"))
    info = country_info(phone)

    flag = str(info.get("flag") or "🏳️")
    flag_id = str(info.get("custom_emoji_id") or "")
    flag_text = tg_custom_emoji(flag, flag_id)
    country = html.escape(str(info.get("shortName") or "UN"))

    if len(phone) >= 9:
        masked = (
            f"<code>{html.escape(phone[:4])}</code>"
            f"{tg_custom_emoji('🟣', PHONE_EMOJI_ID)}"
            f"<code>{html.escape(phone[-4:])}</code>"
        )
    elif len(phone) > 4:
        masked = (
            f"<code>{html.escape(phone[:2])}</code>"
            f"{tg_custom_emoji('🟣', PHONE_EMOJI_ID)}"
            f"<code>{html.escape(phone[-2:])}</code>"
        )
    else:
        masked = f"<code>{html.escape(phone or 'XXXX')}</code>"

    sender = str(sms.get("sender") or "Unknown")
    sender_key = re.sub(r"[^0-9]", "", sender)
    app_name = SENDER_MAP.get(sender_key, sender)

    if app_name.lower() == "whatsapp":
        app_label = tg_custom_emoji("📱", WA_EMOJI_ID)
    else:
        app_label = tg_custom_emoji("📩", APP_EMOJI_ID)

    lang = detect_language(sms.get("message", ""))

    if is_blocked_lang(lang):
        text = (
            f"{flag_text} <b>{country}</b>{app_label} {masked}\n"
            f"{pe('🚫', 'warning_info', '🚫')} <b>{BLOCKED_WARNING}</b>"
        )
        return text, ""

    text = f"{flag_text} <b>{country}</b>{app_label} {masked} #{lang}"
    otp = sms.get("_otp_hint") or extract_otp(sms.get("message", ""))
    return text, otp


def build_markup(sms, otp):
    message = str(sms.get("message") or "")
    lang = detect_language(message)

    if is_blocked_lang(lang):
        return {
            "inline_keyboard": [
                [{
                    "text": f"🚫 {BLOCKED_COPY_TEXT}",
                    "copy_text": {"text": BLOCKED_COPY_TEXT},
                    "icon_custom_emoji_id": WA_EMOJI_ID,
                }],
                [
                    {
                        "text": "NUMBER",
                        "url": NUMBER_URL,
                        "icon_custom_emoji_id": NUMBER_BTN_EMOJI_ID,
                        "style": "primary",
                    },
                    {
                        "text": "DISKUSI",
                        "url": DISCUSSION_URL,
                        "icon_custom_emoji_id": DISCUSSION_BTN_EMOJI_ID,
                        "style": "primary",
                    },
                ],
            ]
        }

    copy_text = message.strip()
    copy_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\ufeff]", " ", copy_text)
    copy_text = re.sub(r"\s+", " ", copy_text).strip()
    if not copy_text:
        copy_text = "—"
    copy_text = copy_text[:256]

    if otp:
        copy_label = str(otp).strip()[:64] or "KODE"
    else:
        first_line = ""
        for line in message.replace("<#>", "").splitlines():
            line = line.strip()
            if line:
                first_line = line
                break
        copy_label = (first_line[:28] + ("…" if len(first_line) > 28 else "")) if first_line else "KODE"
        copy_label = copy_label[:64] or "KODE"

    return {
        "inline_keyboard": [
            [{
                "text": copy_label,
                "copy_text": {"text": copy_text},
                "icon_custom_emoji_id": COPY_OTP_EMOJI_ID,
            }],
            [
                {
                    "text": "NUMBER",
                    "url": NUMBER_URL,
                    "icon_custom_emoji_id": NUMBER_BTN_EMOJI_ID,
                    "style": "primary",
                },
                {
                    "text": "DISKUSI",
                    "url": DISCUSSION_URL,
                    "icon_custom_emoji_id": DISCUSSION_BTN_EMOJI_ID,
                    "style": "primary",
                },
            ],
        ]
    }


def _fallback_markup(markup):
    if not markup:
        return markup
    import copy
    m = copy.deepcopy(markup)
    for row in m.get("inline_keyboard", []):
        for btn in row:
            btn.pop("icon_custom_emoji_id", None)
            btn.pop("style", None)
            ct = btn.get("copy_text")
            if isinstance(ct, dict) and "text" in ct:
                ct["text"] = str(ct["text"])[:256]
    return m


def send_to_one_group(group, text, markup):
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            TG_RATE_LIMITER.wait()
            send_message(group["id"], text, markup)
            return SEND_OK
        except TelegramError as exc:
            msg = str(exc)
            low = msg.lower()

            if exc.status in (400, 403) and any(k in low for k in PERMANENT_ERR_KEYWORDS):
                print(f"[TG PERMANENT][{group['label']}] {msg} — SKIP grup ini")
                return SEND_PERMANENT

            if exc.status == 400:
                if "DOCUMENT_INVALID" in msg or "can't parse" in low:
                    print(f"[TG 400 FALLBACK1][{group['label']}] {msg}")
                    try:
                        TG_RATE_LIMITER.wait()
                        send_message(group["id"], text, _fallback_markup(markup))
                        return SEND_OK
                    except Exception as exc2:
                        print(f"[TG FALLBACK1 FAIL][{group['label']}] {exc2}")
                    try:
                        TG_RATE_LIMITER.wait()
                        send_message(group["id"], text, None)
                        print(f"[TG FALLBACK2 OK][{group['label']}] terkirim tanpa tombol")
                        return SEND_OK
                    except Exception as exc3:
                        print(f"[TG FATAL FALLBACK][{group['label']}] {exc3}")
                        if any(k in str(exc3).lower() for k in PERMANENT_ERR_KEYWORDS):
                            return SEND_PERMANENT
                        return SEND_FAIL
                print(f"[TG FATAL][{group['label']}] {msg}")
                return SEND_PERMANENT if exc.status == 403 else SEND_FAIL

            if exc.retry_after:
                delay = min(int(exc.retry_after) + 1, 60)
                print(f"[TG 429][{group['label']}] retry_after={exc.retry_after}s, tunggu {delay}s")
            else:
                delay = SEND_RETRY_DELAY
                print(f"[TG RETRY {attempt}/{SEND_RETRIES}][{group['label']}] {exc}")
            time.sleep(delay)
        except Exception as exc:
            print(f"[TG RETRY {attempt}/{SEND_RETRIES}][{group['label']}] {exc}")
            time.sleep(SEND_RETRY_DELAY)
    print(f"[TG ERROR][{group['label']}] gagal kirim setelah {SEND_RETRIES}x")
    return SEND_FAIL


def broadcast_sms(account, sms):
    groups = get_groups()
    if not groups:
        print(f"[SKIP][{account['name']}] tidak ada grup — SMS TIDAK ditandai seen")
        return {"all_ok": False, "failed": ["<no-groups>"], "permanent": [],
                "total": 0, "ok": 0}

    text, otp = format_sms(sms)
    markup = build_markup(sms, otp)

    failed = []
    permanent = []
    ok_count = 0

    if BROADCAST_WORKERS <= 1:
        for g in groups:
            status = send_to_one_group(g, text, markup)
            if status == SEND_OK:
                ok_count += 1
            elif status == SEND_PERMANENT:
                permanent.append(g["label"])
                ok_count += 1
            else:
                failed.append(g["label"])
    else:
        workers = min(BROADCAST_WORKERS, len(groups))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(send_to_one_group, g, text, markup): g
                for g in groups
            }
            for fut in as_completed(future_map):
                g = future_map[fut]
                try:
                    status = fut.result()
                except Exception as exc:
                    print(f"[BROADCAST EXC][{g['label']}] {exc}")
                    status = SEND_FAIL
                if status == SEND_OK:
                    ok_count += 1
                elif status == SEND_PERMANENT:
                    permanent.append(g["label"])
                    ok_count += 1
                else:
                    failed.append(g["label"])

    return {
        "all_ok": not failed,
        "failed": failed,
        "permanent": permanent,
        "total": len(groups),
        "ok": ok_count,
    }


# ============================================================
#  SEEN STORAGE
# ============================================================
def load_seen(path):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        backup = path.with_suffix(f".corrupt.{int(time.time())}.json")
        try:
            path.rename(backup)
            print(f"[WARN] seen corrupt → backup ke {backup.name}, dianggap first_run")
        except Exception:
            pass
        return None
    if isinstance(data, list):
        return {str(x): True for x in data}
    return {}


def save_seen(path, seen):
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(seen.keys())[-MAX_SEEN:]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(keys), encoding="utf-8")
    tmp.replace(path)


# ============================================================
#  ADMIN PANEL
# ============================================================
def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


def admin_help_text():
    return (
        f"{pe('🛠', 'admin_panel_btn', '🛠')} <b>ADMIN PANEL</b>\n"
        f"{'▬' * 12}\n"
        "\n"
        f"{pe('📡', 'active_groups_icon', '📡')} <b>Grup Tujuan</b>\n"
        "• <code>/addgc &lt;chat_id&gt; &lt;label&gt;</code>\n"
        "• <code>/delgc &lt;label|chat_id&gt;</code>\n"
        "• <code>/listgc</code>\n"
        "\n"
        f"{pe('🔑', 'kunci_traffic', '🔑')} <b>Akun</b>\n"
        f"• Panel: <code>{' | '.join(PANELS.keys())}</code>\n"
        "• <code>/addacc &lt;panel&gt; &lt;name&gt; &lt;api_key&gt;</code>\n"
        "• <code>/delacc &lt;name&gt;</code>\n"
        "• <code>/listacc</code>\n"
        "\n"
        f"{pe('🛠', 'settings_manage_btn', '🛠')} <b>Lain</b>\n"
        "• <code>/whoami</code> — lihat user &amp; chat id\n"
        "• <code>/reload</code> — baca ulang config.json + bahasa.json\n"
        "\n"
        f"{pe('🚫', 'warning_info', '🚫')} Bahasa diblokir: "
        f"<code>{', '.join(sorted(BLOCKED_LANGS)) or '-'}</code>\n"
        "\n"
        f"{pe('ℹ️', 'info_icon', 'ℹ️')} Setiap SMS dari akun manapun dikirim ke <b>semua</b> grup."
    )


def admin_add_gc(reply_chat, args):
    if len(args) < 1:
        send_message(reply_chat,
            f"{pe('⚠️', 'warning_info', '⚠️')} Format: <code>/addgc &lt;chat_id&gt; &lt;label&gt;</code>\n"
            "Contoh: <code>/addgc -8282829292 1</code>")
        return
    gid = args[0].strip()
    label = args[1].strip() if len(args) > 1 else f"g{len(get_groups()) + 1}"

    if not re.fullmatch(r"-?\d+", gid):
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} chat_id harus numerik.")
        return
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", label):
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Label hanya A-Z a-z 0-9 _ . - (max 32).")
        return

    groups = CONFIG.setdefault("groups", [])
    if any(str(g.get("id")) == gid for g in groups):
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Grup <code>{gid}</code> sudah terdaftar.")
        return
    if any(str(g.get("label", "")).lower() == label.lower() for g in groups):
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Label <b>{html.escape(label)}</b> sudah dipakai.")
        return

    try:
        send_message(gid,
            f"{pe('✅', 'success_icon', '✅')} <b>Grup <code>{label}</code> terhubung.</b>\n"
            "SMS dari semua akun akan masuk ke sini.")
    except Exception as exc:
        send_message(reply_chat,
            f"{pe('⚠️', 'warning_info', '⚠️')} Tidak bisa kirim ke <code>{html.escape(gid)}</code>:\n"
            f"<code>{html.escape(str(exc))}</code>")
        return

    with CONFIG_LOCK:
        CONFIG.setdefault("groups", []).append({"id": gid, "label": label})
        save_config()
    GROUPS[:] = get_groups()

    send_message(reply_chat,
        f"{pe('✅', 'success_icon', '✅')} Grup <b>{html.escape(label)}</b> "
        f"(<code>{html.escape(gid)}</code>) ditambahkan.\n"
        f"{pe('📡', 'active_groups_icon', '📡')} Total: <b>{len(GROUPS)}</b>")


def admin_del_gc(reply_chat, args):
    if not args:
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Format: <code>/delgc &lt;label|chat_id&gt;</code>")
        return
    target = args[0].strip()
    with CONFIG_LOCK:
        groups = CONFIG.get("groups") or []
        new_list = [
            g for g in groups
            if str(g.get("id")) != target and str(g.get("label", "")).lower() != target.lower()
        ]
        if len(new_list) == len(groups):
            send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Grup <code>{html.escape(target)}</code> tidak ditemukan.")
            return
        CONFIG["groups"] = new_list
        save_config()
    GROUPS[:] = get_groups()
    send_message(reply_chat, f"{pe('🗑', 'delete_icon', '🗑')} Grup <b>{html.escape(target)}</b> dihapus.")


def admin_list_gc(reply_chat):
    groups = get_groups()
    if not groups:
        send_message(reply_chat,
            f"{pe('ℹ️', 'info_icon', 'ℹ️')} Belum ada grup tujuan.\n"
            "Tambah: <code>/addgc -8282829292 1</code>")
        return
    lines = [f"{pe('📡', 'active_groups_icon', '📡')} <b>GRUP BROADCAST</b> ({len(groups)})", ""]
    for i, g in enumerate(groups, 1):
        lines.append(
            f"{i}. {pe('📄', 'group_doc_icon', '📄')} <b>{html.escape(g['label'])}</b> "
            f"→ <code>{html.escape(g['id'])}</code>"
        )
    send_message(reply_chat, "\n".join(lines))


def admin_add_account(reply_chat, args):
    if len(args) < 3:
        send_message(reply_chat,
            f"{pe('⚠️', 'warning_info', '⚠️')} Format: "
            "<code>/addacc &lt;panel&gt; &lt;name&gt; &lt;api_key&gt;</code>\n"
            f"Panel: <code>{' | '.join(PANELS.keys())}</code>")
        return

    panel = args[0].strip().lower()
    name = safe_name(args[1])
    api_key = args[2].strip()

    if panel not in PANELS:
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Panel <code>{html.escape(panel)}</code> tidak dikenal.")
        return
    if not name:
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Nama tidak valid.")
        return
    if any(a["name"] == name for a in ACCOUNTS):
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Nama <code>{html.escape(name)}</code> sudah dipakai.")
        return
    if any(a["api_key"] == api_key for a in ACCOUNTS):
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} API key sudah terdaftar.")
        return

    try:
        fetch_panel(panel, api_key)
    except Exception as exc:
        send_message(reply_chat,
            f"{pe('⚠️', 'warning_info', '⚠️')} API key ditolak:\n"
            f"<code>{html.escape(str(exc))}</code>")
        return

    with CONFIG_LOCK:
        CONFIG.setdefault("accounts", []).append(
            {"name": name, "panel": panel, "api_key": api_key}
        )
        save_config()
    ACCOUNTS[:] = load_accounts()

    with STATE_LOCK:
        if name not in STATES:
            seen = load_seen(account_seen_file(name))
            first_run = seen is None
            STATES[name] = {"seen": seen or {}, "first_run": first_run}

    gc_count = len(get_groups())
    mode_note = (
        "SMS lama TIDAK dikirim (mode mark_only)."
        if FIRST_RUN_MODE == "mark_only"
        else f"{pe('⚠️', 'warning_info', '⚠️')} SMS lama AKAN dikirim (mode send_all)!"
    )
    send_message(reply_chat,
        f"{pe('✅', 'success_icon', '✅')} Akun <b>{html.escape(name)}</b> aktif [{html.escape(panel)}].\n"
        f"{pe('📡', 'active_groups_icon', '📡')} Akan broadcast ke <b>{gc_count}</b> grup.\n"
        f"{pe('ℹ️', 'info_icon', 'ℹ️')} {mode_note}")


def admin_del_account(reply_chat, args):
    if not args:
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Format: <code>/delacc &lt;name&gt;</code>")
        return
    name = safe_name(args[0])
    with CONFIG_LOCK:
        accs = CONFIG.get("accounts") or []
        new_list = [a for a in accs if safe_name(a.get("name")) != name]
        if len(new_list) == len(accs):
            send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} Akun <code>{html.escape(name)}</code> tidak ditemukan.")
            return
        CONFIG["accounts"] = new_list
        save_config()
    ACCOUNTS[:] = load_accounts()
    with STATE_LOCK:
        STATES.pop(name, None)
    send_message(reply_chat, f"{pe('🗑', 'delete_icon', '🗑')} Akun <b>{html.escape(name)}</b> dihapus.")


def admin_list_accounts(reply_chat):
    if not ACCOUNTS:
        send_message(reply_chat, f"{pe('ℹ️', 'info_icon', 'ℹ️')} Belum ada akun aktif.")
        return
    gc_count = len(get_groups())
    lines = [f"{pe('👥', 'user_manage_btn', '👥')} <b>AKUN AKTIF</b> ({len(ACCOUNTS)})", ""]
    for i, a in enumerate(ACCOUNTS, 1):
        key_mask = a["api_key"][:10] + "…" + a["api_key"][-6:]
        lines.append(
            f"{i}. {pe('🛠', 'admin_badge', '🛠')} <b>{html.escape(a['name'])}</b> "
            f"[{html.escape(a['panel'])}]\n"
            f"   {pe('🔑', 'kunci_traffic', '🔑')} <code>{html.escape(key_mask)}</code>"
        )
    lines.append("")
    lines.append(f"{pe('📡', 'active_groups_icon', '📡')} Broadcast ke <b>{gc_count}</b> grup.")
    send_message(reply_chat, "\n".join(lines))


def reload_lang():
    global LANG_DATA, DEFAULT_LANG, KEYWORDS, SCRIPT_PATTERNS, EMOJI
    global NUMBER_BTN_EMOJI_ID, DISCUSSION_BTN_EMOJI_ID
    data = load_json(LANG_FILE, None)
    if isinstance(data, dict):
        LANG_DATA = data
        DEFAULT_LANG = str(data.get("default") or "EN").upper()
        KEYWORDS = data.get("keywords") or {}
        SCRIPT_PATTERNS = build_script_patterns()
    em = load_json(EMOJI_FILE, None)
    if isinstance(em, dict):
        EMOJI = em
        NUMBER_BTN_EMOJI_ID = e_or(NUMBER_BTN_KEY, NUMBER_BTN_EMOJI_FALLBACK)
        DISCUSSION_BTN_EMOJI_ID = e_or(DISCUSSION_BTN_KEY, DISCUSSION_BTN_EMOJI_FALLBACK)
    return isinstance(data, dict)


def admin_reload(reply_chat):
    global CONFIG, TG, TW, MSG, FILTER, ADMIN_IDS
    global NUMBER_URL, OWNER_URL, DISCUSSION_URL
    global POLL_SECONDS, REQUEST_TIMEOUT, PAGE_SIZE, FIRST_RUN_MODE, BLOCKED_LANGS

    new_cfg = load_json(CONFIG_FILE, None)
    if not isinstance(new_cfg, dict):
        send_message(reply_chat, f"{pe('⚠️', 'warning_info', '⚠️')} config.json invalid, reload dibatalkan.")
        return

    with CONFIG_LOCK:
        old_token = BOT_TOKEN
        CONFIG = new_cfg

    TG = CONFIG.get("telegram", {}) or {}
    TW = CONFIG.get("thirdwave", {}) or {}
    MSG = CONFIG.get("message", {}) or {}
    FILTER = CONFIG.get("filter", {}) or {}

    ADMIN_IDS = {str(x).strip() for x in (TG.get("admin_ids") or []) if str(x).strip()}
    NUMBER_URL = str(MSG.get("number_url", NUMBER_URL))
    OWNER_URL = str(MSG.get("owner_url", OWNER_URL))
    DISCUSSION_URL = str(MSG.get("discussion_url", OWNER_URL))

    POLL_SECONDS = max(3, _int_or(TW.get("poll_seconds"), POLL_SECONDS))
    REQUEST_TIMEOUT = max(5, _int_or(TW.get("request_timeout"), REQUEST_TIMEOUT))
    PAGE_SIZE = min(100, max(1, _int_or(TW.get("page_size"), PAGE_SIZE)))
    FIRST_RUN_MODE = str(TW.get("first_run_mode", FIRST_RUN_MODE)).lower()
    if FIRST_RUN_MODE not in ("mark_only", "send_all"):
        FIRST_RUN_MODE = "mark_only"

    BLOCKED_LANGS = {
        str(x).strip().upper()
        for x in (FILTER.get("blocked_langs") or DEFAULT_BLOCKED_LANGS)
        if str(x).strip()
    }

    new_token = str(TG.get("bot_token", "")).strip()
    token_warn = ""
    if new_token and new_token != old_token:
        token_warn = "\n⚠️ <b>bot_token berubah — berlaku otomatis via live-read.</b>"

    lang_ok = reload_lang()
    ACCOUNTS[:] = load_accounts()
    GROUPS[:] = get_groups()

    current = {a["name"] for a in ACCOUNTS}
    with STATE_LOCK:
        for name in list(STATES.keys()):
            if name not in current:
                STATES.pop(name, None)
        for name in current:
            if name in STATES:
                continue
            seen = load_seen(account_seen_file(name))
            first_run = seen is None
            STATES[name] = {"seen": seen or {}, "first_run": first_run}

    lang_status = f"{len(KEYWORDS)} bahasa" + ("" if lang_ok else " (GAGAL reload)")
    send_message(reply_chat,
        f"{pe('🔄', 'refresh_btn', '🔄')} Config direload.\n"
        f"• {len(ACCOUNTS)} akun\n"
        f"• {len(GROUPS)} grup\n"
        f"• {len(ADMIN_IDS)} admin\n"
        f"• Bahasa: {lang_status}\n"
        f"• Emoji premium: {len(EMOJI)} id\n"
        f"• Delay: burst={BURST_DELAY}s grup={GROUP_DELAY}s poll={POLL_SECONDS}s"
        f"{token_warn}")


# ============================================================
#  TELEGRAM POLLING
# ============================================================
def bot_commands():
    commands = [
        {"command": "start", "description": "Status monitor"},
        {"command": "stats", "description": "Status API & monitor"},
        {"command": "admin", "description": "Panel admin"},
        {"command": "whoami", "description": "Lihat user & chat id"},
    ]
    try:
        telegram_call("setMyCommands", {"commands": json.dumps(commands)})
    except Exception as exc:
        print(f"[WARN] set commands: {exc}")


def poll_commands(offset):
    try:
        updates = telegram_call("getUpdates", {
            "offset": offset,
            "timeout": 25,
            "allowed_updates": json.dumps(["message"]),
        }, timeout=40) or []
    except Exception as exc:
        print(f"[WARN] getUpdates: {exc}")
        return offset

    public_chats = {g["id"] for g in get_groups()}

    for update in updates:
        offset = max(offset, update.get("update_id", 0) + 1)
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        lowered = text.lower()
        chat_id = str((message.get("chat") or {}).get("id", ""))
        user_id = str((message.get("from") or {}).get("id", ""))
        if not chat_id:
            continue

        if lowered == "/whoami":
            is_adm = f"{pe('✅', 'success_icon', '✅')}" if is_admin(user_id) else f"{pe('❌', 'warning_info', '❌')}"
            send_message(
                chat_id,
                f"{pe('👤', 'id_icon', '👤')} User ID: <code>{html.escape(user_id)}</code>\n"
                f"{pe('💬', 'msg_icon', '💬')} Chat ID: <code>{html.escape(chat_id)}</code>\n"
                f"{pe('🛡', 'verification_shield_icon', '🛡')} Admin: {is_adm}",
            )
            continue

        parts = text.split()
        cmd = parts[0].lower() if parts else ""
        args = parts[1:]

        if is_admin(user_id):
            if cmd in ("/admin", "/help"):
                send_message(chat_id, admin_help_text()); continue
            if cmd == "/addgc":
                admin_add_gc(chat_id, args); continue
            if cmd == "/delgc":
                admin_del_gc(chat_id, args); continue
            if cmd == "/listgc":
                admin_list_gc(chat_id); continue
            if cmd == "/addacc":
                admin_add_account(chat_id, args); continue
            if cmd == "/delacc":
                admin_del_account(chat_id, args); continue
            if cmd == "/listacc":
                admin_list_accounts(chat_id); continue
            if cmd == "/reload":
                admin_reload(chat_id); continue

        if chat_id in public_chats and cmd in ("/start", "/stats"):
            lines = [
                f"{pe('👋', 'wave_hand', '👋')} <b>NEXOR MONITOR</b>",
                "",
                f"{pe('🟢', 'active_status_emoji', '🟢')} Monitor aktif • {len(ACCOUNTS)} akun",
                f"{pe('📡', 'active_groups_icon', '📡')} Broadcast ke {len(get_groups())} grup",
            ]
            send_message(chat_id, "\n".join(lines))
            continue

    return offset


def command_loop():
    offset = 0
    while True:
        try:
            offset = poll_commands(offset)
        except Exception as exc:
            print(f"[CMD ERROR] {exc}")
            time.sleep(3)


# ============================================================
#  LOOP MONITOR
# ============================================================
def process_account(account, state):
    raw = fetch_panel_with_retry(account["panel"], account["api_key"], account["name"])

    items = [normalize(x, account["panel"]) for x in raw]
    items = [x for x in items if x]

    with STATE_LOCK:
        new_items = [x for x in items if x["id"] not in state["seen"]]
        try:
            new_items.sort(key=lambda x: str(x.get("received_at") or ""))
        except Exception:
            pass

    if state["first_run"] and FIRST_RUN_MODE == "mark_only":
        with STATE_LOCK:
            for x in new_items:
                state["seen"][x["id"]] = True
            state["first_run"] = False
            save_seen(account["seen_file"], state["seen"])
        print(f"[FIRST_RUN][{account['name']}] {len(new_items)} SMS lama di-mark")
        return 0

    processed = 0
    any_success = False

    for sms in new_items:
        result = broadcast_sms(account, sms)
        processed += 1

        if result["all_ok"]:
            with STATE_LOCK:
                state["seen"][sms["id"]] = True
            any_success = True
            print(f"[SEND][{account['name']}] {sms['number'][:6]}*** → "
                  f"{result['ok']}/{result['total']} OK"
                  + (f" (skip permanen: {', '.join(result['permanent'])})"
                     if result["permanent"] else ""))
        else:
            print(f"[SEND-FAIL][{account['name']}] {sms['number'][:6]}*** → "
                  f"{len(result['failed'])}/{result['total']} GAGAL: "
                  f"{', '.join(result['failed'])}"
                  + (f" | skip permanen: {', '.join(result['permanent'])}"
                     if result["permanent"] else ""))

        time.sleep(BURST_DELAY)

    with STATE_LOCK:
        if any_success:
            state["first_run"] = False
        save_seen(account["seen_file"], state["seen"])
    return processed


def main():
    print("NEXOR MONITOR — MULTI-PANEL BROADCAST (v3)")
    print(f"Poll: {POLL_SECONDS}s | pageSize: {PAGE_SIZE} | RTO: {REQUEST_TIMEOUT}s | admin: {len(ADMIN_IDS)} user")
    print(f"First-run mode: {FIRST_RUN_MODE}")
    print(f"Filter bahasa aktif: {', '.join(sorted(BLOCKED_LANGS)) or '-'}")
    print(f"Broadcast: {BROADCAST_WORKERS} workers | grup_delay={GROUP_DELAY}s (rate-limiter)")
    print(f"Tombol NUMBER emoji: {NUMBER_BTN_EMOJI_ID} (key={NUMBER_BTN_KEY})")
    print(f"Tombol DISKUSI emoji: {DISCUSSION_BTN_EMOJI_ID} (key={DISCUSSION_BTN_KEY})")

    try:
        telegram_call("deleteWebhook", {"drop_pending_updates": "false"})
    except Exception as exc:
        print(f"[WARN] deleteWebhook: {exc}")

    bot_commands()

    with STATE_LOCK:
        for acc in ACCOUNTS:
            seen = load_seen(acc["seen_file"])
            first_run = seen is None
            if first_run:
                seen = {}
                print(f"[{acc['name']}] First run: SMS lama hanya dicatat")
            STATES[acc["name"]] = {"seen": seen, "first_run": first_run}

    threading.Thread(target=command_loop, daemon=True).start()

    backoff = POLL_SECONDS
    while True:
        any_ok = False
        for acc in list(ACCOUNTS):
            with STATE_LOCK:
                state = STATES.get(acc["name"])
                if state is None:
                    state = {"seen": {}, "first_run": True}
                    STATES[acc["name"]] = state
            try:
                process_account(acc, state)
                any_ok = True
            except Exception as exc:
                print(f"[MONITOR ERROR][{acc['name']}] {exc}")

        if not ACCOUNTS:
            time.sleep(POLL_SECONDS)
            continue

        if any_ok:
            backoff = POLL_SECONDS
        else:
            backoff = min(max(backoff * 2, POLL_SECONDS), MAX_BACKOFF)
            print(f"[BACKOFF] semua akun gagal, tunggu {backoff}s")

        time.sleep(backoff if not any_ok else POLL_SECONDS)


if __name__ == "__main__":
    main()