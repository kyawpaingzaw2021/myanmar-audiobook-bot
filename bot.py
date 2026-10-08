"""
Myanmar TTS Audiobook Bot (v16 — Stable Release)
Multi-User + Edge-TTS + ffmpeg Concat + systemd-ready
"""

import os
import re
import json
import time
import uuid
import shutil
import logging
import asyncio
from io import BytesIO
from datetime import datetime
from logging.handlers import RotatingFileHandler

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from telegram.constants import ParseMode
from gtts import gTTS

# ==================== LOGGING ====================
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'),
    ]
)
logger = logging.getLogger(__name__)

try:
    import edge_tts
    EDGE_AVAILABLE = True
except ImportError:
    EDGE_AVAILABLE = False
    logger.warning("edge_tts not installed — using gTTS only")

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TCON

# ==================== CONFIG ====================
MAIN_ADMIN_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
if not MAIN_ADMIN_ID:
    raise ValueError("ADMIN_USER_ID မရှိပါ။")

DEFAULT_CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
MAX_FILE_SIZE = 10 * 1024 * 1024
CHARS_PER_SECOND = 12
PROCESS_DELAY = int(os.environ.get("PROCESS_DELAY", "6"))
STORAGE_BASE = os.environ.get("STORAGE_BASE", "/app/storage")
FAILED_DIR = os.path.join(STORAGE_BASE, "_failed")
BACKUP_DIR = os.path.join(STORAGE_BASE, "_backup")
SETTINGS_DIR = os.path.join(STORAGE_BASE, "_settings")
USERS_FILE = os.path.join(STORAGE_BASE, "_users.json")
AUTO_CLEAN_DAYS = int(os.environ.get("AUTO_CLEAN_DAYS", "7"))
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")

AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "48k")
AUDIO_SAMPLE_RATE = os.environ.get("AUDIO_SAMPLE_RATE", "24000")

FFMPEG_TIMEOUT = 180
MAX_FINALIZE_RETRY = 5
PENDING_TIMEOUT = 300
REJECT_TTL = 3600

SESSION_ID = str(uuid.uuid4())[:8]

os.makedirs(STORAGE_BASE, exist_ok=True)
os.makedirs(FAILED_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(SETTINGS_DIR, exist_ok=True)

# ==================== SANITIZE ====================
_MD_SPECIAL = re.compile(r'[*_`\[\]\\]')
_FN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def sanitize_markdown(text: str) -> str:
    if not text:
        return "Myanmar Audiobook"
    cleaned = _MD_SPECIAL.sub('', str(text)).strip()
    return cleaned or "Myanmar Audiobook"

def sanitize_filename(text: str) -> str:
    if not text:
        return "audiobook"
    cleaned = _FN_INVALID.sub('', str(text))
    cleaned = re.sub(r'\s+', '_', cleaned)
    cleaned = cleaned.strip('._')[:80]
    return cleaned or "audiobook"

def sanitize_book(book: str) -> tuple:
    return sanitize_markdown(book), sanitize_filename(book)

# ==================== JSON ====================
def save_json(path: str, data, create_dir: bool = True):
    try:
        dirname = os.path.dirname(path)
        if create_dir and dirname:
            os.makedirs(dirname, exist_ok=True)
        elif dirname and not os.path.isdir(dirname):
            return False
        tmp = path + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error(f"save_json {path}: {e}")
        return False

def load_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"load_json {path}: {e}")
        return None

# ==================== STATE ====================
DEFAULT_SETTINGS = {
    "engine": "edge",
    "voice": "my-MM-NilarNeural",
    "rate": "+0%",
    "pitch": "+0Hz",
    "volume": "+0%",
    "lang": "my",
    "tld": "com",
    "slow": False,
    "retry": 3,
    "chunk_size": 2000,
    "target_minutes": 45,
    "book_name": "Myanmar Audiobook",
    "auto_clean": True,
    "channel_id": DEFAULT_CHANNEL_ID,
}

_VALID_RATES = {"-10%", "-5%", "+0%", "+5%", "+10%"}
_VALID_PITCHES = {
    "-50Hz", "-45Hz", "-40Hz", "-35Hz", "-30Hz",
    "-25Hz", "-20Hz", "-15Hz", "-10Hz", "-5Hz",
    "+0Hz",
    "+5Hz", "+10Hz", "+15Hz", "+20Hz", "+25Hz",
    "+30Hz", "+35Hz", "+40Hz", "+45Hz", "+50Hz",
}
_VALID_VOLUMES = {"-50%", "-25%", "+0%", "+25%", "+50%"}
_VALID_ENGINES = {"edge", "gtts"}
_VALID_VOICES = {
    "my-MM-NilarNeural", "my-MM-ThihaNeural",
    "en-US-AriaNeural", "en-US-GuyNeural",
}

allowed_users: set = set()
user_settings: dict = {}
user_jobs: dict = {}
pending_input: dict = {}
_last_reject: dict = {}

# ==================== USERS ====================
def load_allowed_users():
    global allowed_users
    data = load_json(USERS_FILE) or {}
    users = data.get("allowed_users", [])
    allowed_users = set()
    for u in users:
        if isinstance(u, int) and u > 0:
            allowed_users.add(u)
    allowed_users.add(MAIN_ADMIN_ID)

def save_allowed_users():
    save_json(USERS_FILE, {
        "main_admin": MAIN_ADMIN_ID,
        "allowed_users": sorted(allowed_users),
    })

# ==================== SETTINGS ====================
def settings_path(uid: int) -> str:
    return os.path.join(SETTINGS_DIR, f"{uid}.json")

def get_user_settings(uid: int) -> dict:
    if uid not in user_settings:
        loaded = load_json(settings_path(uid))
        if loaded:
            merged = {**DEFAULT_SETTINGS, **loaded}
            changed = False
            if merged.get("rate") not in _VALID_RATES:
                merged["rate"] = "+0%"
                changed = True
            if merged.get("pitch") not in _VALID_PITCHES:
                merged["pitch"] = "+0Hz"
                changed = True
            if merged.get("volume") not in _VALID_VOLUMES:
                merged["volume"] = "+0%"
                changed = True
            if merged.get("engine") not in _VALID_ENGINES:
                merged["engine"] = "edge"
                changed = True
            if merged.get("voice") not in _VALID_VOICES:
                merged["voice"] = "my-MM-NilarNeural"
                changed = True
            try:
                cs = int(merged.get("chunk_size", 2000))
                if cs < 500 or cs > 5000:
                    merged["chunk_size"] = 2000
                    changed = True
            except (ValueError, TypeError):
                merged["chunk_size"] = 2000
                changed = True
            try:
                tm = int(merged.get("target_minutes", 45))
                if tm < 5 or tm > 120:
                    merged["target_minutes"] = 45
                    changed = True
            except (ValueError, TypeError):
                merged["target_minutes"] = 45
                changed = True
            try:
                rt = int(merged.get("retry", 3))
                if rt < 1 or rt > 10:
                    merged["retry"] = 3
                    changed = True
            except (ValueError, TypeError):
                merged["retry"] = 3
                changed = True
            user_settings[uid] = merged
            if changed:
                save_user_settings(uid)
        else:
            user_settings[uid] = dict(DEFAULT_SETTINGS)
    return user_settings[uid]

def save_user_settings(uid: int):
    save_json(settings_path(uid), user_settings.get(uid, {}))

# ==================== STORAGE ====================
def user_dir(uid: int, create: bool = True) -> str:
    p = os.path.join(STORAGE_BASE, str(uid))
    if create:
        os.makedirs(p, exist_ok=True)
    return p

def state_path(uid: int) -> str:
    return os.path.join(user_dir(uid, create=False), "state.json")

def chunks_path(uid: int) -> str:
    return os.path.join(user_dir(uid, create=False), "chunks.json")

def storage_usage_mb() -> float:
    total = 0
    for root, _, files in os.walk(STORAGE_BASE):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total / (1024 * 1024)

def get_storage_breakdown() -> dict:
    user_total = user_files = 0
    failed_total = failed_files = 0
    backup_total = backup_files = 0
    for root, _, files in os.walk(STORAGE_BASE):
        for f in files:
            try:
                size = os.path.getsize(os.path.join(root, f))
            except Exception:
                continue
            if "_failed" in root:
                failed_total += size; failed_files += 1
            elif "_backup" in root:
                backup_total += size; backup_files += 1
            elif "_settings" in root or "_users.json" in root:
                continue
            else:
                user_total += size; user_files += 1
    return {
        "user_mb": user_total / (1024 * 1024),
        "user_files": user_files,
        "failed_mb": failed_total / (1024 * 1024),
        "failed_files": failed_files,
        "backup_mb": backup_total / (1024 * 1024),
        "backup_files": backup_files,
        "total_mb": (user_total + failed_total + backup_total) / (1024 * 1024),
    }

def list_failed_files() -> list:
    if not os.path.isdir(FAILED_DIR):
        return []
    files = []
    for f in os.listdir(FAILED_DIR):
        fp = os.path.join(FAILED_DIR, f)
        if os.path.isfile(fp):
            try:
                files.append({"name": f, "size": os.path.getsize(fp), "mtime": os.path.getmtime(fp)})
            except Exception:
                pass
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files

def list_backup_files() -> list:
    if not os.path.isdir(BACKUP_DIR):
        return []
    files = []
    for f in os.listdir(BACKUP_DIR):
        fp = os.path.join(BACKUP_DIR, f)
        if os.path.isfile(fp):
            try:
                files.append({"name": f, "size": os.path.getsize(fp), "mtime": os.path.getmtime(fp)})
            except Exception:
                pass
    return files

def list_user_dirs() -> list:
    result = []
    if not os.path.isdir(STORAGE_BASE):
        return result
    for entry in os.listdir(STORAGE_BASE):
        p = os.path.join(STORAGE_BASE, entry)
        if os.path.isdir(p) and not entry.startswith("_") and entry.isdigit():
            try:
                size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(p) for f in fs)
                result.append({"uid": entry, "size": size, "path": p})
            except Exception:
                pass
    return result

# ==================== CLEANUP ====================
def cleanup_failed(older_than_days: int = 0) -> dict:
    deleted = 0; freed = 0
    cutoff = time.time() - (older_than_days * 86400) if older_than_days > 0 else 0
    for f in list_failed_files():
        if older_than_days > 0 and f["mtime"] > cutoff:
            continue
        try:
            fp = os.path.join(FAILED_DIR, f["name"])
            freed += os.path.getsize(fp)
            os.remove(fp)
            deleted += 1
        except Exception as e:
            logger.error(f"Delete failed {f['name']}: {e}")
    return {"deleted": deleted, "freed_mb": freed / (1024 * 1024)}

def cleanup_backup(older_than_days: int = 0) -> dict:
    deleted = 0; freed = 0
    cutoff = time.time() - (older_than_days * 86400) if older_than_days > 0 else 0
    for f in list_backup_files():
        if older_than_days > 0 and f["mtime"] > cutoff:
            continue
        try:
            fp = os.path.join(BACKUP_DIR, f["name"])
            freed += os.path.getsize(fp)
            os.remove(fp)
            deleted += 1
        except Exception as e:
            logger.error(f"Delete backup {f['name']}: {e}")
    return {"deleted": deleted, "freed_mb": freed / (1024 * 1024)}

def cleanup_user_dirs(active_uids: set) -> dict:
    deleted = 0; freed = 0
    for d in list_user_dirs():
        uid_int = int(d["uid"])
        if uid_int in active_uids:
            continue
        st = load_json(state_path(uid_int))
        if st:
            total = st.get("total_chunks", 0)
            idx = st.get("current_chunk", 0)
            if st.get("resumed") and uid_int not in active_uids:
                pass
            elif total > 0 and idx < total:
                continue
        try:
            freed += d["size"]
            shutil.rmtree(d["path"], ignore_errors=True)
            deleted += 1
        except Exception as e:
            logger.error(f"Delete user dir {d['uid']}: {e}")
    return {"deleted": deleted, "freed_mb": freed / (1024 * 1024)}

def cleanup_all_orphans() -> dict:
    f = cleanup_failed(0)
    b = cleanup_backup(0)
    u = cleanup_user_dirs(set(user_jobs.keys()))
    return {
        "failed_deleted": f["deleted"], "failed_mb": f["freed_mb"],
        "backup_deleted": b["deleted"], "backup_mb": b["freed_mb"],
        "user_deleted": u["deleted"], "user_mb": u["freed_mb"],
        "total_mb": f["freed_mb"] + b["freed_mb"] + u["freed_mb"],
    }

def auto_clean_old_files() -> dict:
    try:
        result = cleanup_failed(AUTO_CLEAN_DAYS)
        if result["deleted"]:
            logger.info(f"Auto-clean: {result['deleted']} files, {result['freed_mb']:.2f} MB")
        return result
    except Exception as e:
        logger.error(f"auto_clean: {e}")
        return {"deleted": 0, "freed_mb": 0.0}

def cleanup_reject_cache():
    now = time.time()
    stale = [k for k, ts in _last_reject.items() if now - ts > REJECT_TTL]
    for k in stale:
        _last_reject.pop(k, None)

# ==================== TEXT ====================
def clean_text(text: str) -> str:
    text = text.replace('\u200b', '').replace('\ufeff', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def split_text(text: str, max_chars: int = 2000) -> list:
    text = clean_text(text)
    if not text:
        return []
    paragraphs = re.split(r'\n\s*\n', text)
    chunks, current = [], ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            flush()
            sentences = re.split(r'(?<=[။!?\.])\s+', para)
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                if len(current) + len(sent) + 1 <= max_chars:
                    current += (" " if current else "") + sent
                else:
                    flush()
                    if len(sent) > max_chars:
                        words = sent.split()
                        temp = ""
                        for w in words:
                            if len(temp) + len(w) + 1 <= max_chars:
                                temp += (" " if temp else "") + w
                            else:
                                if temp:
                                    chunks.append(temp.strip())
                                temp = w
                        current = temp
                    else:
                        current = sent
        else:
            if len(current) + len(para) + 2 <= max_chars:
                current += ("\n\n" if current else "") + para
            else:
                flush()
                current = para
    flush()
    return [c for c in chunks if c.strip()]

def compute_chunks_per_batch(settings: dict) -> int:
    chunk_size = max(500, int(settings.get("chunk_size", 2000)))
    minutes = max(5, int(settings.get("target_minutes", 45)))
    return max(1, (minutes * 60 * CHARS_PER_SECOND) // chunk_size)

# ==================== AUDIO ====================
def _edge_tts_sync(text: str, settings: dict) -> bytes:
    async def _gen():
        communicate = edge_tts.Communicate(
            text=text,
            voice=settings.get("voice", "my-MM-NilarNeural"),
            rate=settings.get("rate", "+0%"),
            pitch=settings.get("pitch", "+0Hz"),
            volume=settings.get("volume", "+0%"),
        )
        buf = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_gen())
    finally:
        loop.close()

def _gtts_sync(text: str, settings: dict) -> bytes:
    tts = gTTS(
        text=text,
        lang=settings.get("lang", "my"),
        tld=settings.get("tld", "com"),
        slow=settings.get("slow", False),
        lang_check=False,
        timeout=30,
    )
    buf = BytesIO()
    tts.write_to_fp(buf)
    return buf.getvalue()

def generate_mp3_bytes(text: str, settings: dict) -> bytes:
    engine = settings.get("engine", "edge")
    if engine == "edge" and EDGE_AVAILABLE:
        try:
            return _edge_tts_sync(text, settings)
        except Exception as e:
            logger.warning(f"edge-tts fail → gTTS: {e}")
    return _gtts_sync(text, settings)

def add_id3_tags(path: str, title: str, track: int, total: int, album: str):
    try:
        audio = MP3(path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        if audio.tags is None:
            return
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text="Myanmar TTS Bot"))
        audio.tags.add(TALB(encoding=3, text=album))
        audio.tags.add(TRCK(encoding=3, text=f"{track}/{total}"))
        audio.tags.add(TCON(encoding=3, text="Audiobook"))
        audio.save()
    except Exception as e:
        logger.warning(f"ID3 fail: {e}")

def make_state_snapshot(state: dict) -> dict:
    return {
        "current_chunk": state["current_chunk"],
        "batch_index": state["batch_index"],
        "batch_count": state["batch_count"],
        "output_count": state["output_count"],
        "part_counter": state.get("part_counter", 0),
        "started": state["started"],
        "total_chunks": len(state["chunks"]),
        "per_batch": state["per_batch"],
        "chat_id": state["chat_id"],
        "session_id": SESSION_ID,
        "resumed": state.get("resumed", False),
        "pending_finalize": state.get("pending_finalize", False),
        "finalize_retry": state.get("finalize_retry", 0),
    }

def _save_failed_chunk(uid: int, chunk_idx: int, text: str, part_num: int):
    try:
        os.makedirs(FAILED_DIR, exist_ok=True)
        fname = f"uid{uid}_chunk{chunk_idx:05d}_part{part_num:03d}.txt"
        with open(os.path.join(FAILED_DIR, fname), 'w', encoding='utf-8') as f:
            f.write(text)
    except Exception as e:
        logger.error(f"_save_failed_chunk: {e}")

def _move_parts_to_failed(uid: int, parts_dir: str) -> bool:
    if not parts_dir or not os.path.isdir(parts_dir):
        return False
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dst = os.path.join(FAILED_DIR, f"uid{uid}_parts_{ts}")
        shutil.move(parts_dir, dst)
        logger.warning(f"Moved failed parts to {dst}")
        return True
    except Exception as e:
        logger.error(f"_move_parts_to_failed: {e}")
        return False

# ==================== FFMPEG ====================
async def _run_ffmpeg(cmd: list) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return False
        if proc.returncode == 0:
            return True
        logger.warning(f"ffmpeg rc={proc.returncode}: {stderr.decode()[:200]}")
        return False
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.error(f"_run_ffmpeg: {e}")
        return False

async def _ffmpeg_concat(parts_dir: str, out_path: str) -> bool:
    if not parts_dir or not os.path.isdir(parts_dir):
        return False
    part_files = sorted([
        os.path.join(parts_dir, f)
        for f in os.listdir(parts_dir)
        if f.endswith(".mp3")
    ])
    if not part_files:
        return False

    filelist = os.path.join(parts_dir, "_filelist.txt")
    try:
        with open(filelist, 'w', encoding='utf-8') as fl:
            for pf in part_files:
                escaped = pf.replace("'", "'\\''")
                fl.write(f"file '{escaped}'\n")

        cmd = [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", filelist,
            "-map_metadata", "-1",
            "-c:a", "libmp3lame",
            "-b:a", AUDIO_BITRATE,
            "-ar", AUDIO_SAMPLE_RATE,
            "-ac", "1",
            "-id3v2_version", "3",
            out_path
        ]
        if await _run_ffmpeg(cmd):
            logger.info(f"ffmpeg concat OK ({len(part_files)} parts)")
            return True

        cmd2 = [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", filelist,
            "-map_metadata", "-1",
            "-c", "copy",
            "-id3v2_version", "3",
            out_path
        ]
        if await _run_ffmpeg(cmd2):
            return True
        return False
    except Exception as e:
        logger.error(f"ffmpeg concat error: {e}")
        return False
    finally:
        try:
            if os.path.exists(filelist):
                os.remove(filelist)
        except Exception:
            pass

def _byte_concat_fallback(parts_dir: str, out_path: str) -> bool:
    try:
        part_files = sorted([
            os.path.join(parts_dir, f)
            for f in os.listdir(parts_dir)
            if f.endswith(".mp3")
        ])
        if not part_files:
            return False
        with open(out_path, 'wb') as out:
            for pf in part_files:
                with open(pf, 'rb') as inp:
                    shutil.copyfileobj(inp, out, length=64 * 1024)
        return True
    except Exception as e:
        logger.error(f"byte-concat failed: {e}")
        return False

# ==================== PENDING ====================
def set_pending(uid: int, action: str):
    pending_input[uid] = {"action": action, "ts": time.time()}

def pop_pending(uid: int, expected: str = None):
    entry = pending_input.get(uid)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > PENDING_TIMEOUT:
        pending_input.pop(uid, None)
        return None
    if expected and entry.get("action") != expected:
        return None
    pending_input.pop(uid, None)
    return entry.get("action")

def clear_pending(uid: int):
    pending_input.pop(uid, None)

# ==================== JOB PROCESSOR ====================
async def process_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    uid = job.data["user_id"]
    chat_id = job.data["chat_id"]

    if uid not in allowed_users:
        job.schedule_removal()
        await cleanup_user(uid, delete_dir=True)
        return

    state = user_jobs.get(uid)
    if not state or state.get("status") != "processing":
        job.schedule_removal()
        return

    chunks = state["chunks"]
    idx = state["current_chunk"]
    total = len(chunks)

    if state.get("pending_finalize"):
        settings = get_user_settings(uid)
        parts_dir = state.get("parts_dir") or os.path.join(user_dir(uid), f"parts_{state.get('batch_index', 0)}")
        batch_path = state.get("batch_path") or os.path.join(user_dir(uid), f"batch_{state.get('batch_index', 0)}.mp3")

        ok = await finalize_batch(context, uid, chat_id, batch_path, state, settings, parts_dir)
        retry = state.get("finalize_retry", 0)

        if ok:
            new_idx = state["batch_index"] + 1
            state["batch_path"] = os.path.join(user_dir(uid), f"batch_{new_idx}.mp3")
            state["parts_dir"] = os.path.join(user_dir(uid), f"parts_{new_idx}")
            state["batch_count"] = 0
            state["batch_index"] = new_idx
            state["pending_finalize"] = False
            state["finalize_retry"] = 0
            save_json(state_path(uid), make_state_snapshot(state), create_dir=False)
        else:
            retry += 1
            state["finalize_retry"] = retry
            save_json(state_path(uid), make_state_snapshot(state), create_dir=False)

            if retry >= MAX_FINALIZE_RETRY:
                _move_parts_to_failed(uid, parts_dir)
                new_idx = state["batch_index"] + 1
                state["batch_path"] = os.path.join(user_dir(uid), f"batch_{new_idx}.mp3")
                state["parts_dir"] = os.path.join(user_dir(uid), f"parts_{new_idx}")
                state["batch_count"] = 0
                state["batch_index"] = new_idx
                state["pending_finalize"] = False
                state["finalize_retry"] = 0
                save_json(state_path(uid), make_state_snapshot(state), create_dir=False)
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⚠️ Batch finalize မအောင်မြင် — `_failed/` ထဲ သိမ်းထားသည်",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception:
                    pass
            else:
                return

        idx = state["current_chunk"]

    if idx >= total:
        settings = get_user_settings(uid)
        batch_count = state["batch_count"]

        if batch_count > 0 and not state.get("pending_finalize"):
            parts_dir = state.get("parts_dir") or os.path.join(user_dir(uid), f"parts_{state.get('batch_index', 0)}")
            batch_path = state.get("batch_path") or os.path.join(user_dir(uid), f"batch_{state.get('batch_index', 0)}.mp3")
            ok = await finalize_batch(context, uid, chat_id, batch_path, state, settings, parts_dir)
            if not ok:
                _move_parts_to_failed(uid, parts_dir)

        try:
            elapsed = int((datetime.now() - datetime.fromisoformat(state["started"])).total_seconds())
            elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"
        except Exception:
            elapsed_str = "-"

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ *အားလုံး ပြီးပါပြီ*\n\n"
                    f"📄 Chunks: {total:,}\n"
                    f"🎵 ပို့ပြီး: {state['output_count']} ဖိုင်\n"
                    f"⏱️ ကြာချိန်: {elapsed_str}\n"
                    f"💾 Storage: {storage_usage_mb():.1f} MB"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

        await cleanup_user(uid)
        job.schedule_removal()
        return

    chunk = chunks[idx]
    settings = get_user_settings(uid)
    per_batch = state["per_batch"]
    parts_dir = state.get("parts_dir") or os.path.join(user_dir(uid), f"parts_{state.get('batch_index', 0)}")
    msg_id = state.get("status_msg_id")

    try:
        if msg_id and idx % 3 == 0:
            progress = min(100, int((idx / total) * 100)) if total else 0
            filled = min(20, progress // 5)
            bar = "█" * filled + "░" * (20 - filled)
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=(
                        f"⏳ *ထုတ်လုပ်နေသည်...*\n\n"
                        f"`{bar}` {progress}%\n"
                        f"📄 Chunk: {idx + 1}/{total:,}\n"
                        f"🎵 ပို့ပြီး: {state['output_count']}\n"
                        f"📦 Batch: {state['batch_count']}/{per_batch}"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            except (BadRequest, Exception):
                pass

        retries = max(1, int(settings.get("retry", 3)))
        mp3_bytes = None
        for attempt in range(retries):
            try:
                mp3_bytes = await asyncio.to_thread(generate_mp3_bytes, chunk, settings)
                break
            except Exception as e:
                logger.warning(f"Chunk {idx} attempt {attempt+1}: {e}")
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(2)

        if mp3_bytes is None:
            raise RuntimeError(f"No data for chunk {idx}")

        os.makedirs(parts_dir, exist_ok=True)
        chunk_file = os.path.join(parts_dir, f"chunk_{state['batch_count']:04d}.mp3")
        with open(chunk_file, 'wb') as cf:
            cf.write(mp3_bytes)

        state["batch_count"] += 1
        state["current_chunk"] = idx + 1

        if state["batch_count"] >= per_batch:
            state["pending_finalize"] = True
            state["finalize_retry"] = 0
            save_json(state_path(uid), make_state_snapshot(state), create_dir=False)
            return

        save_json(state_path(uid), make_state_snapshot(state), create_dir=False)

    except Exception as e:
        logger.error(f"Chunk {idx} FAIL: {e}")
        _save_failed_chunk(uid, idx, chunk, state.get("part_counter", 0))
        state["current_chunk"] = idx + 1
        save_json(state_path(uid), make_state_snapshot(state), create_dir=False)

async def finalize_batch(context, uid, chat_id, batch_path, state, settings, parts_dir=None) -> bool:
    parts_exists = bool(parts_dir and os.path.isdir(parts_dir))
    batch_exists = os.path.exists(batch_path)

    if not parts_exists and not batch_exists:
        return True

    concat_ok = False
    if parts_exists:
        concat_ok = await _ffmpeg_concat(parts_dir, batch_path)
        if not concat_ok:
            concat_ok = _byte_concat_fallback(parts_dir, batch_path)

    output_valid = (
        concat_ok and
        os.path.exists(batch_path) and
        os.path.getsize(batch_path) >= 1024
    )

    if not output_valid:
        try:
            if os.path.exists(batch_path):
                os.remove(batch_path)
        except Exception:
            pass
        return False

    if parts_dir and os.path.isdir(parts_dir):
        try:
            shutil.rmtree(parts_dir, ignore_errors=True)
        except Exception:
            pass

    file_size = os.path.getsize(batch_path)
    state["part_counter"] = state.get("part_counter", 0) + 1
    out_num = state["part_counter"]
    total_batches = state.get("est_batches", 0)

    raw_book = settings.get("book_name", "Myanmar Audiobook")
    book_md, book_fn = sanitize_book(raw_book)

    channel_id = settings.get("channel_id", "") or DEFAULT_CHANNEL_ID
    posted = False

    if channel_id:
        await asyncio.to_thread(
            add_id3_tags, batch_path,
            f"{book_md} - Part {out_num:03d}", out_num, total_batches, book_md
        )
        target_min = settings.get("target_minutes", 45)
        duration_sec = target_min * 60

        for attempt in range(3):
            try:
                with open(batch_path, 'rb') as f:
                    msg = await context.bot.send_audio(
                        chat_id=channel_id,
                        audio=f,
                        filename=f"{book_fn}_part_{out_num:03d}.mp3",
                        title=f"{book_md} - Part {out_num:03d}",
                        performer="Myanmar TTS Bot",
                        duration=duration_sec,
                        caption=(
                            f"📚 *{book_md}*\n"
                            f"━━━━━━━━━━━━━━\n"
                            f"🎵 Part {out_num:03d}"
                            + (f" / {total_batches}" if total_batches else "") + "\n"
                            f"⏱️ ~{target_min} မိနစ်\n"
                            f"📦 {file_size / (1024 * 1024):.2f} MB\n"
                            f"━━━━━━━━━━━━━━\n"
                            f"#Audiobook #Myanmar #Part{out_num:03d}"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                        read_timeout=180, write_timeout=180
                    )
                if msg and msg.message_id:
                    posted = True
                    break
            except Exception as e:
                logger.warning(f"Post attempt {attempt+1}: {e}")
                await asyncio.sleep(3)

    if posted:
        try:
            os.remove(batch_path)
        except Exception:
            pass
        state["output_count"] = state.get("output_count", 0) + 1
        save_json(state_path(uid), make_state_snapshot(state), create_dir=False)
        return True

    try:
        os.makedirs(FAILED_DIR, exist_ok=True)
        failed_name = f"uid{uid}_part{out_num:03d}_{os.path.basename(batch_path)}"
        failed_path = os.path.join(FAILED_DIR, failed_name)
        if os.path.exists(batch_path):
            shutil.move(batch_path, failed_path)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Part {out_num:03d} Channel ပို့မရ\n📁 `_failed/{failed_name}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Move failed: {e}")

    return True

async def cleanup_user(uid: int, delete_dir: bool = True):
    user_jobs.pop(uid, None)
    if delete_dir:
        try:
            shutil.rmtree(user_dir(uid, create=False), ignore_errors=True)
        except Exception:
            pass

# ==================== MENU BUILDERS ====================
def main_menu_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📖 အသုံးပြုနည်း", callback_data="m_howto")],
        [InlineKeyboardButton("⚙️ Voice Settings", callback_data="m_voice")],
        [InlineKeyboardButton("📊 Job Status", callback_data="m_status")],
        [InlineKeyboardButton("💾 Storage", callback_data="m_storage")],
    ]
    if uid == MAIN_ADMIN_ID:
        rows.append([InlineKeyboardButton("👥 Users Management", callback_data="m_users")])
        rows.append([InlineKeyboardButton("🧹 Admin Cleanup", callback_data="m_clean")])
    rows.append([InlineKeyboardButton("❓ Help", callback_data="m_help")])
    return InlineKeyboardMarkup(rows)

def users_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    for u in sorted(allowed_users):
        if u == MAIN_ADMIN_ID:
            rows.append([InlineKeyboardButton(f"👑 {u} (Main Admin)", callback_data="noop")])
        else:
            rows.append([
                InlineKeyboardButton(f"👤 {u}", callback_data="noop"),
                InlineKeyboardButton("❌ Remove", callback_data=f"um_rm_{u}")
            ])
    rows.append([InlineKeyboardButton("➕ Add User", callback_data="um_add")])
    rows.append([InlineKeyboardButton("🔙 Main Menu", callback_data="m_main")])
    return InlineKeyboardMarkup(rows)

def confirm_remove_kb(target_uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Remove", callback_data=f"um_do_rm_{target_uid}"),
         InlineKeyboardButton("❌ Cancel", callback_data="m_users")],
    ])

def voice_menu_kb(s: dict) -> InlineKeyboardMarkup:
    voice = s.get("voice", "my-MM-NilarNeural")
    if "Nilar" in voice:
        v_icon = "👩 Nilar (F)"
    elif "Thiha" in voice:
        v_icon = "👨 Thiha (M)"
    else:
        v_icon = f"🎙️ {voice[:18]}"
    engine = s.get("engine", "edge").upper()
    ch = s.get("channel_id") or "(none)"
    ac = "ON" if s.get("auto_clean", True) else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎙️ Engine: {engine}", callback_data="v_engine")],
        [InlineKeyboardButton(f"🎤 Voice: {v_icon}", callback_data="v_voice")],
        [InlineKeyboardButton(f"⚡ Speed: {s.get('rate', '+0%')}", callback_data="v_rate")],
        [InlineKeyboardButton(f"🎵 Pitch: {s.get('pitch', '+0Hz')}", callback_data="v_pitch")],
        [InlineKeyboardButton(f"🔊 Volume: {s.get('volume', '+0%')}", callback_data="v_volume")],
        [InlineKeyboardButton(f"📡 Channel: {ch[:20]}", callback_data="v_channel")],
        [InlineKeyboardButton(f"✂️ Chunk: {s['chunk_size']}", callback_data="v_chunk")],
        [InlineKeyboardButton(f"⏱️ Batch: {s['target_minutes']} min", callback_data="v_batch")],
        [InlineKeyboardButton(f"🔁 Retry: {s['retry']}", callback_data="v_retry")],
        [InlineKeyboardButton(f"🧹 Auto-clean: {ac}", callback_data="v_autoclean")],
        [InlineKeyboardButton("📚 Book Name", callback_data="v_book")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="m_main")],
    ])

def clean_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Failed ဖိုင်များ", callback_data="c_failed")],
        [InlineKeyboardButton("🗑️ Backup ဖိုင်များ", callback_data="c_backup")],
        [InlineKeyboardButton("🗑️ Orphan User folders", callback_data="c_user")],
        [InlineKeyboardButton("🧨 Full Clean", callback_data="c_all_confirm")],
        [InlineKeyboardButton("📊 Breakdown", callback_data="c_breakdown")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="m_main")],
    ])

def confirm_clean_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data=f"c_do_{action}"),
         InlineKeyboardButton("❌ Cancel", callback_data="m_clean")],
    ])

def engine_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Edge-TTS (High Quality)", callback_data="set_engine_edge")],
        [InlineKeyboardButton("🔊 gTTS (Fallback)", callback_data="set_engine_gtts")],
        [InlineKeyboardButton("🔙 Back", callback_data="m_voice")],
    ])

def voice_picker_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👩 Nilar (Female)", callback_data="set_voice_my-MM-NilarNeural")],
        [InlineKeyboardButton("👨 Thiha (Male)", callback_data="set_voice_my-MM-ThihaNeural")],
        [InlineKeyboardButton("🇬🇧 English (Aria)", callback_data="set_voice_en-US-AriaNeural")],
        [InlineKeyboardButton("🇬🇧 English (Guy)", callback_data="set_voice_en-US-GuyNeural")],
        [InlineKeyboardButton("🔙 Back", callback_data="m_voice")],
    ])

def rate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐢 -10%", callback_data="set_rate_-10%"),
         InlineKeyboardButton("🐢 -5%", callback_data="set_rate_-5%"),
         InlineKeyboardButton("➖ 0%", callback_data="set_rate_+0%")],
        [InlineKeyboardButton("🚶 +5%", callback_data="set_rate_+5%"),
         InlineKeyboardButton("🚶 +10%", callback_data="set_rate_+10%")],
        [InlineKeyboardButton("🔙 Back", callback_data="m_voice")],
    ])

def pitch_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ -50Hz", callback_data="set_pitch_-50Hz"),
         InlineKeyboardButton("⬇️ -45Hz", callback_data="set_pitch_-45Hz"),
         InlineKeyboardButton("⬇️ -40Hz", callback_data="set_pitch_-40Hz"),
         InlineKeyboardButton("⬇️ -35Hz", callback_data="set_pitch_-35Hz"),
         InlineKeyboardButton("⬇️ -30Hz", callback_data="set_pitch_-30Hz")],
        [InlineKeyboardButton("⬇️ -25Hz", callback_data="set_pitch_-25Hz"),
         InlineKeyboardButton("⬇️ -20Hz", callback_data="set_pitch_-20Hz"),
         InlineKeyboardButton("⬇️ -15Hz", callback_data="set_pitch_-15Hz"),
         InlineKeyboardButton("⬇️ -10Hz", callback_data="set_pitch_-10Hz"),
         InlineKeyboardButton("⬇️ -5Hz",  callback_data="set_pitch_-5Hz")],
        [InlineKeyboardButton("➡️ +0Hz (Normal)", callback_data="set_pitch_+0Hz")],
        [InlineKeyboardButton("⬆️ +5Hz",  callback_data="set_pitch_+5Hz"),
         InlineKeyboardButton("⬆️ +10Hz", callback_data="set_pitch_+10Hz"),
         InlineKeyboardButton("⬆️ +15Hz", callback_data="set_pitch_+15Hz"),
         InlineKeyboardButton("⬆️ +20Hz", callback_data="set_pitch_+20Hz"),
         InlineKeyboardButton("⬆️ +25Hz", callback_data="set_pitch_+25Hz")],
        [InlineKeyboardButton("⬆️ +30Hz", callback_data="set_pitch_+30Hz"),
         InlineKeyboardButton("⬆️ +35Hz", callback_data="set_pitch_+35Hz"),
         InlineKeyboardButton("⬆️ +40Hz", callback_data="set_pitch_+40Hz"),
         InlineKeyboardButton("⬆️ +45Hz", callback_data="set_pitch_+45Hz"),
         InlineKeyboardButton("⬆️ +50Hz", callback_data="set_pitch_+50Hz")],
        [InlineKeyboardButton("🔙 Back", callback_data="m_voice")],
    ])

def volume_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔈 -50%", callback_data="set_volume_-50%"),
         InlineKeyboardButton("🔉 -25%", callback_data="set_volume_-25%")],
        [InlineKeyboardButton("🔊 +0%", callback_data="set_volume_+0%")],
        [InlineKeyboardButton("📢 +25%", callback_data="set_volume_+25%"),
         InlineKeyboardButton("📣 +50%", callback_data="set_volume_+50%")],
        [InlineKeyboardButton("🔙 Back", callback_data="m_voice")],
    ])

def chunk_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1000", callback_data="set_chunk_1000"),
         InlineKeyboardButton("1500", callback_data="set_chunk_1500"),
         InlineKeyboardButton("2000", callback_data="set_chunk_2000")],
        [InlineKeyboardButton("2500", callback_data="set_chunk_2500"),
         InlineKeyboardButton("3000", callback_data="set_chunk_3000")],
        [InlineKeyboardButton("🔙 Back", callback_data="m_voice")],
    ])

def batch_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("15 min", callback_data="set_batch_15"),
         InlineKeyboardButton("30 min", callback_data="set_batch_30")],
        [InlineKeyboardButton("45 min", callback_data="set_batch_45"),
         InlineKeyboardButton("60 min", callback_data="set_batch_60")],
        [InlineKeyboardButton("🔙 Back", callback_data="m_voice")],
    ])

def retry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="set_retry_1"),
         InlineKeyboardButton("2", callback_data="set_retry_2"),
         InlineKeyboardButton("3", callback_data="set_retry_3"),
         InlineKeyboardButton("5", callback_data="set_retry_5")],
        [InlineKeyboardButton("🔙 Back", callback_data="m_voice")],
    ])

# ==================== GUARD ====================
def is_allowed(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in allowed_users)

def is_main_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == MAIN_ADMIN_ID)

async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_allowed(update):
        return True
    u = update.effective_user
    if not u:
        return False
    now = time.time()
    if now - _last_reject.get(u.id, 0) < 60:
        try:
            if update.callback_query:
                await update.callback_query.answer("⛔ Not authorized", show_alert=False)
        except Exception:
            pass
        return False
    _last_reject[u.id] = now
    if len(_last_reject) > 100:
        cleanup_reject_cache()
    try:
        text = (
            f"⛔ *Bot အသုံးပြုခွင့် မရှိပါ*\n\n"
            f"🆔 သင့် Telegram ID: `{u.id}`\n\n"
            f"Admin ကို ဒီ ID ပေးပြီး ခွင့်တောင်းပါ။"
        )
        if update.callback_query:
            await update.callback_query.answer("⛔ Not authorized", show_alert=True)
        elif update.message:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    return False

async def guard_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await guard(update, context):
        return False
    if not is_main_admin(update):
        try:
            if update.callback_query:
                await update.callback_query.answer("⛔ Main admin only", show_alert=True)
            elif update.message:
                await update.message.reply_text("⛔ Main admin only။")
        except Exception:
            pass
        return False
    return True

# ==================== TEXT BUILDERS ====================
def build_status_text(uid: int) -> str:
    if uid not in user_jobs:
        return f"📊 Job မရှိပါ။\n💾 Storage: {storage_usage_mb():.1f} MB"
    state = user_jobs[uid]
    idx = state["current_chunk"]; total = len(state["chunks"])
    progress = min(100, int((idx / total) * 100)) if total else 0
    filled = min(20, progress // 5)
    bar = "█" * filled + "░" * (20 - filled)
    try:
        elapsed = int((datetime.now() - datetime.fromisoformat(state["started"])).total_seconds())
        elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"
    except Exception:
        elapsed_str = "-"
    pending = " ⏳" if state.get("pending_finalize") else ""
    return (
        f"📊 *Status*\n\n`{bar}` {progress}%\n"
        f"📄 Chunk: {idx:,}/{total:,}\n"
        f"🎵 ပို့ပြီး: {state['output_count']}\n"
        f"📊 Part: {state.get('part_counter', 0)}{pending}\n"
        f"📦 Batch: {state['batch_count']}/{state['per_batch']}\n"
        f"⏱️ ကြာချိန်: {elapsed_str}\n"
        f"💾 Storage: {storage_usage_mb():.1f} MB"
    )

def build_storage_text() -> str:
    bd = get_storage_breakdown()
    return (
        f"💾 *Storage Overview*\n\n"
        f"*Total:* {bd['total_mb']:.2f} MB\n\n"
        f"📁 User Files: {bd['user_files']} — {bd['user_mb']:.2f} MB\n"
        f"⚠️ Failed: {bd['failed_files']} — {bd['failed_mb']:.2f} MB\n"
        f"📦 Backup: {bd['backup_files']} — {bd['backup_mb']:.2f} MB"
    )

def build_clean_text() -> str:
    bd = get_storage_breakdown()
    return (
        f"🧹 *Admin Cleanup*\n\n"
        f"📁 User: {bd['user_mb']:.2f} MB ({bd['user_files']})\n"
        f"⚠️ Failed: {bd['failed_mb']:.2f} MB ({bd['failed_files']})\n"
        f"📦 Backup: {bd['backup_mb']:.2f} MB ({bd['backup_files']})\n"
        f"━━━━━━━━━━━━━\n"
        f"💾 *Total:* {bd['total_mb']:.2f} MB\n\n"
        f"⚡ Active: {len(user_jobs)}\n"
        f"🔖 Session: `{SESSION_ID}`"
    )

def build_users_text() -> str:
    return (
        f"👥 *Users Management*\n\n"
        f"📊 စုစုပေါင်း: *{len(allowed_users)}* user\n"
        f"👑 Main Admin: `{MAIN_ADMIN_ID}`\n\n"
        f"👇 User ရွေးပါ (Add / Remove)"
    )

# ==================== COMMANDS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    uid = update.effective_user.id
    clear_pending(uid)
    s = get_user_settings(uid)
    warn = ""
    if not s.get("channel_id"):
        warn = "\n\n⚠️ *Channel မသတ်မှတ်ရသေးပါ* — `/setchannel -100...`"
    await update.message.reply_text(
        "🎙️ *Myanmar TTS Audiobook Bot*\n\n"
        "🎤 Edge-TTS Neural Voices\n"
        "👩 Nilar / 👨 Thiha\n\n"
        "📄 TXT ဖိုင် (10MB အထိ) ပို့ပါ"
        + warn,
        reply_markup=main_menu_kb(uid),
        parse_mode=ParseMode.MARKDOWN
    )

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    uid = update.effective_user.id
    clear_pending(uid)
    await update.message.reply_text("🎙️ *Main Menu*",
                                    reply_markup=main_menu_kb(uid),
                                    parse_mode=ParseMode.MARKDOWN)

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u:
        await update.message.reply_text(
            f"🆔 သင့် Telegram ID: `{u.id}`\n"
            f"👤 Name: {u.full_name}\n"
            f"📛 Username: @{u.username or '(none)'}",
            parse_mode=ParseMode.MARKDOWN
        )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    uid = update.effective_user.id
    await update.message.reply_text(build_status_text(uid), parse_mode=ParseMode.MARKDOWN)

async def storage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    await update.message.reply_text(build_storage_text(), parse_mode=ParseMode.MARKDOWN)

async def cleaning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, context):
        return
    await update.message.reply_text(build_clean_text(),
                                    reply_markup=clean_menu_kb(),
                                    parse_mode=ParseMode.MARKDOWN)

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    uid = update.effective_user.id
    clear_pending(uid)
    if uid not in user_jobs:
        await update.message.reply_text("📊 Job မရှိပါ။")
        return
    for job in context.job_queue.get_jobs_by_name(f"tts_{uid}"):
        job.schedule_removal()
    state = user_jobs[uid]
    done = state["current_chunk"]; total = len(state["chunks"])
    out = state.get("output_count", 0)
    part = state.get("part_counter", 0)
    await cleanup_user(uid)
    await update.message.reply_text(
        f"🛑 *ရပ်လိုက်ပါပြီ*\n\n"
        f"📄 ပြီးခဲ့သည်: {done:,}/{total:,}\n"
        f"🎵 ပို့ခဲ့သည်: {out} ဖိုင်\n"
        f"📊 Part counter: {part}",
        parse_mode=ParseMode.MARKDOWN
    )

async def setbook_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    uid = update.effective_user.id
    s = get_user_settings(uid)
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("📚 `/setbook <name>`", parse_mode=ParseMode.MARKDOWN)
        return
    s["book_name"] = name
    save_user_settings(uid)
    md_safe, fn_safe = sanitize_book(name)
    await update.message.reply_text(
        f"✅ Book name: *{md_safe}*\n"
        f"📁 Filename: `{fn_safe}_part_001.mp3`",
        parse_mode=ParseMode.MARKDOWN
    )

async def setchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    uid = update.effective_user.id
    s = get_user_settings(uid)
    args = context.args
    if not args:
        current = s.get("channel_id") or "(none)"
        await update.message.reply_text(
            f"📡 *Channel*\n\nလက်ရှိ: `{current}`\n\n"
            f"`/setchannel -1001234567890`\n`/setchannel clear`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    val = args[0].strip()
    if val.lower() in ("clear", "none", "-"):
        s["channel_id"] = ""
    else:
        s["channel_id"] = val
    save_user_settings(uid)
    await update.message.reply_text(f"✅ Channel: `{s['channel_id'] or '(none)'}`",
                                    parse_mode=ParseMode.MARKDOWN)

async def failed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    files = list_failed_files()
    if not files:
        await update.message.reply_text("✅ Failed file မရှိပါ။")
        return
    total_mb = sum(f["size"] for f in files) / (1024 * 1024)
    lines = [f"⚠️ *Failed Files* ({len(files)} | {total_mb:.2f} MB)\n"]
    for f in files[:20]:
        mb = f["size"] / (1024 * 1024)
        lines.append(f"• `{f['name']}` — {mb:.2f} MB")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def voices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    await update.message.reply_text(
        "🎤 *Available Voices*\n\n"
        "👩 *Nilar (Female)*\n`my-MM-NilarNeural`\n\n"
        "👨 *Thiha (Male)*\n`my-MM-ThihaNeural`\n\n"
        "📌 `/menu` → Voice Settings",
        parse_mode=ParseMode.MARKDOWN
    )

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, context):
        return
    clear_pending(update.effective_user.id)
    await update.message.reply_text(build_users_text(),
                                    reply_markup=users_menu_kb(),
                                    parse_mode=ParseMode.MARKDOWN)

# ==================== MESSAGE HANDLERS ====================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    uid = update.effective_user.id
    text = update.message.text.strip()
    if not text:
        return

    if uid == MAIN_ADMIN_ID:
        action = pop_pending(uid, expected="add_user")
        if action == "add_user":
            await handle_add_user_input(update, context, text)
            return

    s = get_user_settings(uid)
    max_chars = s.get("chunk_size", 2000)
    if len(text) > max_chars:
        await update.message.reply_text(
            f"⚠️ {len(text):,} လုံး — များလွန်းသည်။ TXT ဖိုင်အဖြစ် ပို့ပါ။"
        )
        return

    await update.message.chat.send_action(action="record_voice")
    msg = await update.message.reply_text("⏳ ထုတ်လုပ်နေသည်...")
    try:
        mp3 = await asyncio.to_thread(generate_mp3_bytes, text, s)
        duration = max(1, int(len(text) / CHARS_PER_SECOND))
        await update.message.reply_audio(
            audio=BytesIO(mp3),
            filename="myanmar_tts.mp3",
            title="Myanmar TTS",
            performer="Myanmar TTS Bot",
            duration=duration,
            caption=f"✅ {len(text):,} စာလုံး | {len(mp3) // 1024} KB"
        )
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Text handler: {e}", exc_info=True)
        try:
            await msg.edit_text(f"⚠️ အမှားဖြစ်သည်။\n`{str(e)[:150]}`", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

async def handle_add_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    uid = update.effective_user.id
    try:
        new_id = int(text)
    except ValueError:
        set_pending(uid, "add_user")
        await update.message.reply_text(
            "⚠️ နံပါတ်သာ ပို့ပါ။ ဥပမာ: `123456789`\n"
            "ထပ်ကြိုးစားပါ (၅ မိနစ်အတွင်း)။",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    if new_id <= 0:
        set_pending(uid, "add_user")
        await update.message.reply_text("⚠️ ID သည် positive number ဖြစ်ရမည်။")
        return
    if new_id in allowed_users:
        await update.message.reply_text(f"⚠️ `{new_id}` ရှိပြီးသား။",
                                        reply_markup=users_menu_kb(),
                                        parse_mode=ParseMode.MARKDOWN)
        return

    allowed_users.add(new_id)
    save_allowed_users()
    get_user_settings(new_id)
    save_user_settings(new_id)

    await update.message.reply_text(
        f"✅ *User ထည့်ပြီး:* `{new_id}`\n\n" + build_users_text(),
        reply_markup=users_menu_kb(),
        parse_mode=ParseMode.MARKDOWN
    )
    try:
        await context.bot.send_message(
            chat_id=new_id,
            text=(
                "🎉 *Myanmar TTS Bot ကို အသုံးပြုခွင့် ရပါပြီ!*\n\n"
                "📌 `/start` — စတင်\n"
                "📡 `/setchannel <channel_id>` — Channel\n"
                "🎤 `/menu` — Voice Settings"
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning(f"Notify new user: {e}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    uid = update.effective_user.id
    clear_pending(uid)
    chat_id = update.effective_chat.id
    doc = update.message.document

    if not doc.file_name or not doc.file_name.lower().endswith('.txt'):
        await update.message.reply_text("⚠️ *.txt* ဖိုင်သာ လက်ခံသည်။")
        return
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"⚠️ ဖိုင် ကြီးလွန်း ({doc.file_size // (1024*1024)} MB)\nMax: 10 MB"
        )
        return
    if uid in user_jobs and user_jobs[uid].get("status") == "processing":
        await update.message.reply_text("⚠️ Job ရှိပြီးသား။ /cancel ဖြင့် ရပ်။")
        return

    msg = await update.message.reply_text("📥 ဖိုင် ရယူနေသည်...")
    udir = user_dir(uid)
    text_path = os.path.join(udir, "source.txt")

    try:
        file = await doc.get_file()
        try:
            await file.download_to_drive(text_path)
        except Exception:
            if os.path.exists(text_path):
                try: os.remove(text_path)
                except Exception: pass
            raise

        with open(text_path, 'r', encoding='utf-8', errors='ignore') as f:
            raw = f.read()
        if not raw.strip():
            await msg.edit_text("⚠️ ဖိုင်ထဲ စာသား မရှိပါ။")
            return

        await msg.edit_text("✂️ အပိုင်းခွဲနေသည်...")
        s = get_user_settings(uid)
        chunks = split_text(raw, s["chunk_size"])
        if not chunks:
            await msg.edit_text("⚠️ ခွဲလို့ မရပါ။")
            return

        save_json(chunks_path(uid), {"chunks": chunks})

        total = len(chunks)
        total_chars = sum(len(c) for c in chunks)
        per_batch = compute_chunks_per_batch(s)
        est_batches = (total + per_batch - 1) // per_batch
        est_sec = int(total * (PROCESS_DELAY + 1.5))
        est_str = f"{est_sec // 3600}h {(est_sec % 3600) // 60}m"

        batch_path = os.path.join(udir, "batch_0.mp3")
        parts_dir = os.path.join(udir, "parts_0")
        os.makedirs(parts_dir, exist_ok=True)

        state = {
            "status": "processing",
            "chunks": chunks,
            "current_chunk": 0,
            "batch_path": batch_path,
            "parts_dir": parts_dir,
            "batch_count": 0,
            "batch_index": 0,
            "output_count": 0,
            "part_counter": 0,
            "pending_finalize": False,
            "finalize_retry": 0,
            "status_msg_id": msg.message_id,
            "started": datetime.now().isoformat(),
            "chat_id": chat_id,
            "est_batches": est_batches,
            "per_batch": per_batch,
            "resumed": False,
        }
        user_jobs[uid] = state
        save_json(state_path(uid), make_state_snapshot(state))

        engine = s.get("engine", "edge").upper()
        voice = s.get("voice", "my-MM-NilarNeural")
        ch = s.get("channel_id") or "(none)"

        await msg.edit_text(
            f"📊 *ဖိုင်ခွဲပြီးပါပြီ*\n\n"
            f"🎙️ Engine: *{engine}*\n"
            f"🎤 Voice: `{voice}`\n"
            f"⚡ Rate: `{s.get('rate', '+0%')}`\n"
            f"📡 Channel: `{ch[:30]}`\n\n"
            f"📄 Chunks: *{total:,}*\n"
            f"📝 စာလုံးရေ: *{total_chars:,}*\n"
            f"🎵 Output: ~*{est_batches}*\n"
            f"⏱️ ခန့်မှန်းချိန်: ~*{est_str}*\n\n"
            f"⏳ စတင်နေသည်... (/cancel ဖြင့် ရပ်)",
            parse_mode=ParseMode.MARKDOWN
        )

        context.job_queue.run_repeating(
            process_job,
            interval=PROCESS_DELAY,
            first=2,
            data={"user_id": uid, "chat_id": chat_id},
            name=f"tts_{uid}"
        )
    except Exception as e:
        logger.error(f"Doc: {e}", exc_info=True)
        try:
            await msg.edit_text(f"⚠️ အမှားဖြစ်သည်။\n`{str(e)[:150]}`", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
        await cleanup_user(uid)

# ==================== CALLBACK ====================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await _callback_impl(update, context)
    except BadRequest as e:
        msg = str(e).lower()
        if "not modified" in msg or "query is too old" in msg or "query id is invalid" in msg:
            return
        logger.error(f"Callback BadRequest: {e}")
    except Exception as e:
        logger.error(f"Callback error: {e}", exc_info=True)

async def _callback_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    answered = {"done": False}
    async def ans(text=None, show_alert=False):
        if answered["done"]:
            return
        answered["done"] = True
        try:
            await q.answer(text, show_alert=show_alert)
        except Exception:
            pass

    if data == "noop":
        await ans()
        return
    if not await guard(update, context):
        return
    await ans()

    uid = update.effective_user.id
    if data not in ("um_add", "um_cancel_add"):
        clear_pending(uid)
    s = get_user_settings(uid)

    if data == "m_main":
        await q.edit_message_text("🎙️ *Main Menu*", reply_markup=main_menu_kb(uid),
                                  parse_mode=ParseMode.MARKDOWN)
    elif data == "m_howto":
        await q.edit_message_text(
            "📖 *အသုံးပြုနည်း*\n\n"
            "1️⃣ TXT ဖိုင် (10MB အထိ) ပို့\n"
            f"2️⃣ {s['chunk_size']:,} လုံးစီ ခွဲ\n"
            f"3️⃣ {PROCESS_DELAY} စက္ကန့်ခြား MP3 ထုတ်\n"
            f"4️⃣ ~{s['target_minutes']} မိနစ်စာ Channel သို့ ပို့\n\n"
            "📌 `/menu /status /cancel /voices /myid`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_main")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "m_voice":
        await q.edit_message_text("⚙️ *Voice Settings*", reply_markup=voice_menu_kb(s),
                                  parse_mode=ParseMode.MARKDOWN)
    elif data == "m_status":
        await q.edit_message_text(
            build_status_text(uid),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_main")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "m_storage":
        await q.edit_message_text(
            build_storage_text(),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_main")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "m_users":
        if not is_main_admin(update):
            return
        await q.edit_message_text(build_users_text(), reply_markup=users_menu_kb(),
                                  parse_mode=ParseMode.MARKDOWN)
    elif data == "m_clean":
        if not is_main_admin(update):
            return
        await q.edit_message_text(build_clean_text(), reply_markup=clean_menu_kb(),
                                  parse_mode=ParseMode.MARKDOWN)
    elif data == "m_help":
        await q.edit_message_text(
            "❓ *Help*\n\n"
            "• Edge-TTS Neural voices\n"
            "• TXT 10MB အထိ\n"
            "• ffmpeg concat (48k bitrate)\n"
            "• ပို့မရတဲ့ batch → `_failed/`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_main")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "um_add":
        if not is_main_admin(update):
            return
        set_pending(uid, "add_user")
        await q.edit_message_text(
            "➕ *Add User*\n\nUser ရဲ့ *Telegram ID* ကို ပို့ပါ။\n"
            "ဥပမာ: `123456789`\n\n"
            f"⏱️ {PENDING_TIMEOUT // 60} မိနစ်အတွင်း ပို့ပါ",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="um_cancel_add")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "um_cancel_add":
        clear_pending(uid)
        await q.edit_message_text(build_users_text(), reply_markup=users_menu_kb(),
                                  parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("um_rm_"):
        if not is_main_admin(update):
            return
        try:
            target = int(data.replace("um_rm_", ""))
        except ValueError:
            return
        if target == MAIN_ADMIN_ID:
            await ans("👑 Main admin ကို မဖျက်နိုင်ပါ", show_alert=True)
            return
        await q.edit_message_text(
            f"⚠️ *Remove User?*\n\n🆔 `{target}`\n\nသေချာပြီလား?",
            reply_markup=confirm_remove_kb(target),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data.startswith("um_do_rm_"):
        if not is_main_admin(update):
            return
        try:
            target = int(data.replace("um_do_rm_", ""))
        except ValueError:
            return
        if target == MAIN_ADMIN_ID:
            await ans("Main admin ကို မဖျက်နိုင်", show_alert=True)
            return
        for job in context.job_queue.get_jobs_by_name(f"tts_{target}"):
            job.schedule_removal()
        user_jobs.pop(target, None)
        allowed_users.discard(target)
        save_allowed_users()
        try:
            shutil.rmtree(user_dir(target, create=False), ignore_errors=True)
        except Exception:
            pass
        try:
            os.remove(settings_path(target))
        except Exception:
            pass
        user_settings.pop(target, None)
        pending_input.pop(target, None)
        await q.edit_message_text(
            f"✅ Removed: `{target}`\n\n" + build_users_text(),
            reply_markup=users_menu_kb(),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "v_engine":
        await q.edit_message_text(f"🎙️ Engine (current: `{s.get('engine')}`)",
                                  reply_markup=engine_kb(), parse_mode=ParseMode.MARKDOWN)
    elif data == "v_voice":
        await q.edit_message_text(f"🎤 Voice (current: `{s.get('voice')}`)",
                                  reply_markup=voice_picker_kb(), parse_mode=ParseMode.MARKDOWN)
    elif data == "v_rate":
        await q.edit_message_text(f"⚡ Speed (current: `{s.get('rate')}`)",
                                  reply_markup=rate_kb(), parse_mode=ParseMode.MARKDOWN)
    elif data == "v_pitch":
        await q.edit_message_text(f"🎵 Pitch (current: `{s.get('pitch')}`)",
                                  reply_markup=pitch_kb(), parse_mode=ParseMode.MARKDOWN)
    elif data == "v_volume":
        await q.edit_message_text(f"🔊 Volume (current: `{s.get('volume')}`)",
                                  reply_markup=volume_kb(), parse_mode=ParseMode.MARKDOWN)
    elif data == "v_channel":
        ch = s.get("channel_id") or "(none)"
        await q.edit_message_text(
            f"📡 *Channel*\n\nလက်ရှိ: `{ch}`\n\n"
            f"`/setchannel -1001234567890`\n`/setchannel clear`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_voice")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "v_chunk":
        await q.edit_message_text(f"✂️ Chunk (current: `{s['chunk_size']}`)",
                                  reply_markup=chunk_kb(), parse_mode=ParseMode.MARKDOWN)
    elif data == "v_batch":
        await q.edit_message_text(f"⏱️ Batch (current: `{s['target_minutes']}` min)",
                                  reply_markup=batch_kb(), parse_mode=ParseMode.MARKDOWN)
    elif data == "v_retry":
        await q.edit_message_text(f"🔁 Retry (current: `{s['retry']}`)",
                                  reply_markup=retry_kb(), parse_mode=ParseMode.MARKDOWN)
    elif data == "v_autoclean":
        s["auto_clean"] = not s.get("auto_clean", True)
        save_user_settings(uid)
        state_str = "ON ✅" if s["auto_clean"] else "OFF ❌"
        await q.edit_message_text(
            f"🧹 *Auto-clean: {state_str}*\n\n"
            f"ON — Startup မှာ {AUTO_CLEAN_DAYS} ရက်ကျော် Failed ဖိုင်များ ဖျက်မည်။",
            reply_markup=voice_menu_kb(s), parse_mode=ParseMode.MARKDOWN
        )
    elif data == "v_book":
        md_safe, fn_safe = sanitize_book(s.get("book_name", ""))
        await q.edit_message_text(
            f"📚 Book Name\n\nလက်ရှိ: *{md_safe}*\n"
            f"📁 Filename: `{fn_safe}_part_001.mp3`\n\n"
            f"ပြောင်းရန်: `/setbook <name>`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_voice")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data.startswith("set_engine_"):
        s["engine"] = data.replace("set_engine_", "")
        save_user_settings(uid)
        await q.edit_message_text(f"✅ Engine: `{s['engine']}`",
                                  reply_markup=voice_menu_kb(s), parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("set_voice_"):
        s["voice"] = data.replace("set_voice_", "")
        save_user_settings(uid)
        await q.edit_message_text(f"✅ Voice: `{s['voice']}`",
                                  reply_markup=voice_menu_kb(s), parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("set_rate_"):
        new_rate = data.replace("set_rate_", "")
        if new_rate not in _VALID_RATES:
            await ans("⚠️ Invalid rate", show_alert=True)
            return
        s["rate"] = new_rate
        save_user_settings(uid)
        await q.edit_message_text(f"✅ Speed: `{s['rate']}`",
                                  reply_markup=voice_menu_kb(s), parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("set_pitch_"):
        new_pitch = data.replace("set_pitch_", "")
        if new_pitch not in _VALID_PITCHES:
            await ans("⚠️ Invalid pitch", show_alert=True)
            return
        s["pitch"] = new_pitch
        save_user_settings(uid)
        await q.edit_message_text(f"✅ Pitch: `{s['pitch']}`",
                                  reply_markup=voice_menu_kb(s), parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("set_volume_"):
        new_vol = data.replace("set_volume_", "")
        if new_vol not in _VALID_VOLUMES:
            await ans("⚠️ Invalid volume", show_alert=True)
            return
        s["volume"] = new_vol
        save_user_settings(uid)
        await q.edit_message_text(f"✅ Volume: `{s['volume']}`",
                                  reply_markup=voice_menu_kb(s), parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("set_chunk_"):
        try:
            cs = int(data.replace("set_chunk_", ""))
            if 500 <= cs <= 5000:
                s["chunk_size"] = cs
                save_user_settings(uid)
        except ValueError:
            pass
        await q.edit_message_text(f"✅ Chunk: `{s['chunk_size']}`",
                                  reply_markup=voice_menu_kb(s), parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("set_batch_"):
        try:
            bm = int(data.replace("set_batch_", ""))
            if 5 <= bm <= 120:
                s["target_minutes"] = bm
                save_user_settings(uid)
        except ValueError:
            pass
        await q.edit_message_text(f"✅ Batch: `{s['target_minutes']}` min",
                                  reply_markup=voice_menu_kb(s), parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("set_retry_"):
        try:
            rt = int(data.replace("set_retry_", ""))
            if 1 <= rt <= 10:
                s["retry"] = rt
                save_user_settings(uid)
        except ValueError:
            pass
        await q.edit_message_text(f"✅ Retry: `{s['retry']}`",
                                  reply_markup=voice_menu_kb(s), parse_mode=ParseMode.MARKDOWN)
    elif data == "c_breakdown":
        if not is_main_admin(update):
            return
        bd = get_storage_breakdown()
        await q.edit_message_text(
            f"📊 *Storage Breakdown*\n\n"
            f"📁 User: {bd['user_mb']:.2f} MB ({bd['user_files']})\n"
            f"⚠️ Failed: {bd['failed_mb']:.2f} MB ({bd['failed_files']})\n"
            f"📦 Backup: {bd['backup_mb']:.2f} MB ({bd['backup_files']})\n"
            f"━━━━━━━━━━━━━\n"
            f"💾 Total: *{bd['total_mb']:.2f} MB*\n"
            f"⚡ Active: {len(user_jobs)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_clean")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "c_failed":
        if not is_main_admin(update):
            return
        result = await asyncio.to_thread(cleanup_failed, 0)
        await q.edit_message_text(
            f"🗑️ *Failed Cleanup*\n\n✅ {result['deleted']} ဖိုင်\n💾 {result['freed_mb']:.2f} MB",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_clean")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "c_backup":
        if not is_main_admin(update):
            return
        result = await asyncio.to_thread(cleanup_backup, 0)
        await q.edit_message_text(
            f"🗑️ *Backup Cleanup*\n\n✅ {result['deleted']} ဖိုင်\n💾 {result['freed_mb']:.2f} MB",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_clean")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "c_user":
        if not is_main_admin(update):
            return
        result = await asyncio.to_thread(cleanup_user_dirs, set(user_jobs.keys()))
        await q.edit_message_text(
            f"🗑️ *Orphan Cleanup*\n\n✅ {result['deleted']} folder\n💾 {result['freed_mb']:.2f} MB",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_clean")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "c_all_confirm":
        if not is_main_admin(update):
            return
        await q.edit_message_text(
            "🧨 *Full Clean — အတည်ပြုပါ*\n\n"
            "• Failed\n• Backup\n• Orphan folders\n\n❓ သေချာပြီလား?",
            reply_markup=confirm_clean_kb("all"),
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "c_do_all":
        if not is_main_admin(update):
            return
        result = await asyncio.to_thread(cleanup_all_orphans)
        await q.edit_message_text(
            f"🧨 *Full Cleanup Done*\n\n"
            f"⚠️ Failed: {result['failed_deleted']} ({result['failed_mb']:.2f} MB)\n"
            f"📦 Backup: {result['backup_deleted']} ({result['backup_mb']:.2f} MB)\n"
            f"📁 User: {result['user_deleted']} ({result['user_mb']:.2f} MB)\n"
            f"━━━━━━━━━━━━━\n"
            f"💾 *Total:* {result['total_mb']:.2f} MB",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m_clean")]]),
            parse_mode=ParseMode.MARKDOWN
        )

# ==================== RESUME ====================
async def resume_jobs(app: Application):
    if not os.path.isdir(STORAGE_BASE):
        return
    resumed = 0
    for entry in os.listdir(STORAGE_BASE):
        if not entry.isdigit():
            continue
        uid = int(entry)
        if uid not in allowed_users:
            shutil.rmtree(user_dir(uid, create=False), ignore_errors=True)
            continue
        st = load_json(state_path(uid))
        if st and st.get("session_id") == SESSION_ID:
            continue
        cd = load_json(chunks_path(uid))
        if not st or not cd or not cd.get("chunks"):
            shutil.rmtree(user_dir(uid, create=False), ignore_errors=True)
            continue
        chunks = cd["chunks"]
        total = len(chunks)
        idx = st.get("current_chunk", 0)
        if (idx >= total
                and not st.get("pending_finalize")
                and st.get("batch_count", 0) == 0):
            shutil.rmtree(user_dir(uid, create=False), ignore_errors=True)
            continue

        udir = user_dir(uid)
        batch_index = st.get("batch_index", 0)
        for d in os.listdir(udir):
            if d.startswith("parts_"):
                try:
                    n = int(d.replace("parts_", ""))
                    if n != batch_index:
                        shutil.rmtree(os.path.join(udir, d), ignore_errors=True)
                except Exception:
                    pass

        batch_path = os.path.join(udir, f"batch_{batch_index}.mp3")
        parts_dir = os.path.join(udir, f"parts_{batch_index}")
        os.makedirs(parts_dir, exist_ok=True)

        s = get_user_settings(uid)
        per_batch = st.get("per_batch") or compute_chunks_per_batch(s)

        state = {
            "status": "processing",
            "chunks": chunks,
            "current_chunk": idx,
            "batch_path": batch_path,
            "parts_dir": parts_dir,
            "batch_count": st.get("batch_count", 0),
            "batch_index": batch_index,
            "output_count": st.get("output_count", 0),
            "part_counter": st.get("part_counter", 0),
            "pending_finalize": st.get("pending_finalize", False),
            "finalize_retry": st.get("finalize_retry", 0),
            "status_msg_id": None,
            "started": st.get("started", datetime.now().isoformat()),
            "chat_id": st.get("chat_id", uid),
            "est_batches": (total + per_batch - 1) // per_batch,
            "per_batch": per_batch,
            "resumed": True,
        }
        user_jobs[uid] = state
        save_json(state_path(uid), make_state_snapshot(state))

        app.job_queue.run_repeating(
            process_job, interval=PROCESS_DELAY, first=5,
            data={"user_id": uid, "chat_id": state["chat_id"]},
            name=f"tts_{uid}"
        )
        resumed += 1
        try:
            await app.bot.send_message(
                chat_id=state["chat_id"],
                text=f"🔄 *Job ပြန်စတင်နေသည်*\n📄 {idx:,}/{total:,}",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass
    if resumed:
        logger.info(f"Resumed {resumed} job(s)")

# ==================== DAILY CLEAN ====================
async def daily_auto_clean(context: ContextTypes.DEFAULT_TYPE):
    try:
        cleanup_reject_cache()
        admin_s = get_user_settings(MAIN_ADMIN_ID)
        if not admin_s.get("auto_clean", True):
            return
        await asyncio.to_thread(auto_clean_old_files)
    except Exception as e:
        logger.error(f"Daily clean: {e}")

# ==================== POST-INIT ====================
async def post_init(app: Application):
    load_allowed_users()
    logger.info(f"=== Session {SESSION_ID} | Allowed: {sorted(allowed_users)} ===")

    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_BIN, "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            ver_line = out.decode().splitlines()[0] if out else "?"
            logger.info(f"ffmpeg OK: {ver_line}")
        else:
            logger.warning(f"ffmpeg exit code {proc.returncode}")
    except FileNotFoundError:
        logger.warning(f"⚠️ ffmpeg not found at '{FFMPEG_BIN}'")
    except Exception as e:
        logger.warning(f"ffmpeg check failed: {e}")

    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Bot စတင်"),
            BotCommand("menu", "Main Menu"),
            BotCommand("status", "Job Status"),
            BotCommand("cancel", "Job ရပ်"),
            BotCommand("storage", "Storage"),
            BotCommand("failed", "Failed files"),
            BotCommand("voices", "Voice list"),
            BotCommand("setbook", "Book Name"),
            BotCommand("setchannel", "Channel Setting"),
            BotCommand("myid", "သင့် ID ကြည့်"),
            BotCommand("users", "Users Management"),
            BotCommand("cleaning", "Cleanup Panel"),
        ])
    except Exception as e:
        logger.warning(f"set_my_commands: {e}")

    try:
        admin_s = get_user_settings(MAIN_ADMIN_ID)
        if admin_s.get("auto_clean", True):
            await asyncio.to_thread(auto_clean_old_files)
    except Exception:
        pass

    try:
        app.job_queue.run_repeating(daily_auto_clean, interval=86400, first=3600, name="daily_clean")
    except Exception as e:
        logger.error(f"Schedule: {e}")

    try:
        await resume_jobs(app)
    except Exception as e:
        logger.error(f"resume_jobs: {e}", exc_info=True)

# ==================== MAIN ====================
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN မရှိပါ။")

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("storage", storage_cmd))
    app.add_handler(CommandHandler("failed", failed_cmd))
    app.add_handler(CommandHandler("cleaning", cleaning_cmd))
    app.add_handler(CommandHandler("voices", voices_cmd))
    app.add_handler(CommandHandler("setbook", setbook_cmd))
    app.add_handler(CommandHandler("setchannel", setchannel_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print(f"Bot running... (session={SESSION_ID})")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
