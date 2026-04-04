#!/usr/bin/env python3
import base64
import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

# =========================
# 🔧 EDIT THESE
# =========================
BOT_TOKEN = ""
CHAT_ID = "1014747952"
USERNAME = "Karthi"
FB_PASSWORD = ""
# =========================

ROOT = os.path.abspath("/storage/emulated/0")
PORT = "8080"
DATA_DIR = os.path.expanduser("~/fbdata")
DB_PATH = os.path.join(DATA_DIR, "filebrowser.db")

ARIA2_RPC_PORT = 6800
ARIA2_SECRET = os.environ.get("ARIA2_SECRET", uuid.uuid4().hex)

PAGE_SIZE = 8
PROGRESS_UPDATE_SECONDS = 1.0

LINK_REGEX = r"https://[a-zA-Z0-9.-]*trycloudflare\.com"
URL_REGEX = re.compile(r"(?i)\b(?:magnet:\?\S+|https?://\S+)")

fb_proc: Optional[subprocess.Popen] = None
cf_proc: Optional[subprocess.Popen] = None
aria2_proc: Optional[subprocess.Popen] = None

stop_event = threading.Event()
state_lock = threading.Lock()

last_announced_link: Optional[str] = None
startup_sent = False

pending_jobs: Dict[str, Dict[str, Any]] = {}
active_downloads: Dict[str, Dict[str, Any]] = {}


# -------------------------
# Telegram API
# -------------------------
def _encode_params(params: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in params.items():
        if v is None:
            continue
        if k == "reply_markup" and isinstance(v, (dict, list)):
            out[k] = json.dumps(v, ensure_ascii=False)
        else:
            out[k] = str(v)
    return out


def telegram_api(method: str, params: Optional[Dict[str, Any]] = None, timeout: int = 35) -> Dict[str, Any]:
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN is empty"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = None
    headers = {}

    if params is not None:
        data = urllib.parse.urlencode(_encode_params(params)).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return json.loads(raw)
    except Exception as e:
        return {"ok": False, "description": str(e)}


def send_message(chat_id: str, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    params: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    res = telegram_api("sendMessage", params)
    if res.get("ok"):
        try:
            return int(res["result"]["message_id"])
        except Exception:
            return None
    return None


def edit_message(chat_id: str, message_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
    params: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    res = telegram_api("editMessageText", params)
    return bool(res.get("ok"))


def answer_callback(callback_id: str, text: str = "", show_alert: bool = False) -> None:
    params: Dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        params["text"] = text
    if show_alert:
        params["show_alert"] = True
    telegram_api("answerCallbackQuery", params, timeout=20)


# -------------------------
# Helpers
# -------------------------
def escape_text(text: str) -> str:
    return (text or "").replace("\r", "").strip()


def is_authorized(chat_id: str) -> bool:
    return str(chat_id) == str(CHAT_ID)


def extract_url(text: str) -> Optional[str]:
    match = URL_REGEX.search(escape_text(text))
    if not match:
        return None
    return match.group(0).rstrip(").,]\"'")


def ensure_root_safe(path: str) -> str:
    path = os.path.abspath(path)
    root = os.path.abspath(ROOT)
    if os.path.commonpath([path, root]) != root:
        return root
    return path


def safe_join(base: str, *parts: str) -> str:
    path = os.path.abspath(os.path.join(base, *parts))
    if os.path.commonpath([path, ROOT]) != ROOT:
        raise ValueError("Path escapes ROOT")
    return path


def list_subfolders(path: str) -> List[str]:
    try:
        entries = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                entries.append(name)
        return sorted(entries, key=lambda s: s.lower())
    except Exception:
        return []


def short_path(path: str) -> str:
    rel = os.path.relpath(path, ROOT)
    if rel == ".":
        return "/"
    return "/" + rel


def make_bar(percent: int, width: int = 12) -> str:
    percent = max(0, min(100, percent))
    filled = int((percent / 100) * width)
    return "█" * filled + "░" * (width - filled)


def human_size(num: float) -> str:
    num = max(0.0, float(num))
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if num < 1024.0 or unit == "PB":
            return f"{int(num)} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def human_speed(num: float) -> str:
    if num <= 0:
        return "0 B/s"
    return f"{human_size(num)}/s"


def format_eta(seconds: Optional[int]) -> str:
    if seconds is None:
        return "..."
    try:
        seconds = max(0, int(seconds))
    except Exception:
        return "..."
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def classify_source(url: str) -> str:
    u = url.lower().strip()
    if u.startswith("magnet:?"):
        return "magnet"
    parsed = urllib.parse.urlparse(url)
    if parsed.path.lower().endswith(".torrent"):
        return "torrent"
    return "http"


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# -------------------------
# aria2 RPC
# -------------------------
def aria2_rpc(method: str, params: Optional[List[Any]] = None, timeout: int = 15) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": "karthi",
        "method": f"aria2.{method}",
        "params": [f"token:{ARIA2_SECRET}"],
    }
    if params:
        payload["params"].extend(params)

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{ARIA2_RPC_PORT}/jsonrpc",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)


def ensure_aria2_daemon() -> bool:
    global aria2_proc
    if shutil.which("aria2c") is None:
        return False

    if aria2_proc is not None and aria2_proc.poll() is None:
        try:
            aria2_rpc("getGlobalStat", timeout=5)
            return True
        except Exception:
            pass

    cmd = [
        "aria2c",
        "--enable-rpc=true",
        f"--rpc-listen-port={ARIA2_RPC_PORT}",
        "--rpc-listen-all=false",
        f"--rpc-secret={ARIA2_SECRET}",
        "--file-allocation=none",
        "--continue=true",
        "--check-integrity=false",
        "--disable-ipv6=true",
        "--console-log-level=warn",
        "--log-level=warn",
        "--summary-interval=0",
        "--max-concurrent-downloads=1",
        "--dir", ROOT,
    ]
    aria2_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            aria2_rpc("getGlobalStat", timeout=3)
            return True
        except Exception:
            time.sleep(0.25)

    try:
        if aria2_proc and aria2_proc.poll() is None:
            aria2_proc.terminate()
    except Exception:
        pass
    aria2_proc = None
    return False


def aria2_add_http(url: str, dest: str) -> str:
    options = {
        "dir": dest,
        "split": "16",
        "max-connection-per-server": "16",
        "min-split-size": "1M",
        "file-allocation": "none",
        "check-integrity": "false",
        "retry-wait": "2",
        "max-tries": "0",
        "timeout": "60",
        "reuse-uri": "true",
        "stream-piece-selector": "geom",
        "uri-selector": "feedback",
        "header": ["User-Agent: Mozilla/5.0"],
    }
    res = aria2_rpc("addUri", [[url], options], timeout=20)
    if "result" not in res:
        raise RuntimeError(res.get("error", {}).get("message", "aria2 addUri failed"))
    return str(res["result"])


def aria2_add_magnet(url: str, dest: str) -> str:
    options = {
        "dir": dest,
        "bt-max-peers": "80",
        "seed-time": "0",
        "seed-ratio": "0.0",
        "bt-save-metadata": "true",
        "bt-request-peer-speed-limit": "0",
        "bt-max-open-files": "100",
        "check-integrity": "false",
        "file-allocation": "none",
        "max-tries": "0",
        "retry-wait": "2",
    }
    res = aria2_rpc("addUri", [[url], options], timeout=20)
    if "result" not in res:
        raise RuntimeError(res.get("error", {}).get("message", "aria2 magnet add failed"))
    return str(res["result"])


def aria2_add_torrent_from_url(url: str, dest: str) -> str:
    torrent_bytes = fetch_bytes(url, timeout=90)
    torrent_b64 = base64.b64encode(torrent_bytes).decode("ascii")
    options = {
        "dir": dest,
        "bt-max-peers": "80",
        "seed-time": "0",
        "seed-ratio": "0.0",
        "bt-save-metadata": "true",
        "bt-request-peer-speed-limit": "0",
        "check-integrity": "false",
        "file-allocation": "none",
        "max-tries": "0",
        "retry-wait": "2",
    }
    res = aria2_rpc("addTorrent", [torrent_b64, [], options], timeout=20)
    if "result" not in res:
        raise RuntimeError(res.get("error", {}).get("message", "aria2 addTorrent failed"))
    return str(res["result"])


def aria2_status(gid: str) -> Dict[str, Any]:
    fields = [
        "gid", "status", "totalLength", "completedLength", "downloadSpeed",
        "eta", "files", "errorMessage", "numSeeds", "connections"
    ]
    res = aria2_rpc("tellStatus", [gid, fields], timeout=10)
    if "result" not in res:
        raise RuntimeError(res.get("error", {}).get("message", "aria2 tellStatus failed"))
    return res["result"]


def aria2_remove(gid: str) -> None:
    try:
        aria2_rpc("remove", [gid], timeout=10)
    except Exception:
        pass


def aria2_shutdown() -> None:
    try:
        aria2_rpc("shutdown", timeout=5)
    except Exception:
        pass


# -------------------------
# Folder picker
# -------------------------
def get_job(chat_id: str) -> Optional[Dict[str, Any]]:
    with state_lock:
        return pending_jobs.get(str(chat_id))


def set_job(chat_id: str, job: Dict[str, Any]) -> None:
    with state_lock:
        pending_jobs[str(chat_id)] = job


def clear_job(chat_id: str) -> None:
    with state_lock:
        pending_jobs.pop(str(chat_id), None)


def build_keyboard(session_id: str, entries: List[str], page: int, can_go_up: bool) -> dict:
    keyboard: List[List[Dict[str, str]]] = []
    start = page * PAGE_SIZE
    visible = entries[start:start + PAGE_SIZE]

    for idx, name in enumerate(visible):
        keyboard.append([{"text": f"📁 {name}", "callback_data": f"nav:{session_id}:{idx}"}])

    nav_row: List[Dict[str, str]] = []
    if page > 0:
        nav_row.append({"text": "⬅️ Prev", "callback_data": f"prev:{session_id}"})
    if start + PAGE_SIZE < len(entries):
        nav_row.append({"text": "Next ➡️", "callback_data": f"next:{session_id}"})
    if nav_row:
        keyboard.append(nav_row)

    bottom: List[Dict[str, str]] = []
    if can_go_up:
        bottom.append({"text": "⬆️ Up", "callback_data": f"up:{session_id}"})
    bottom.append({"text": "✅ Save here", "callback_data": f"save:{session_id}"})
    bottom.append({"text": "❌ Cancel", "callback_data": f"cancel:{session_id}"})
    keyboard.append(bottom)

    return {"inline_keyboard": keyboard}


def format_picker_text(url: str, current_path: str) -> str:
    return (
        "📥 Download link received\n"
        f"🔗 URL:\n{url}\n\n"
        f"📁 Current folder:\n{short_path(current_path)}\n\n"
        "Choose a folder, then tap ✅ Save here."
    )


def render_picker(chat_id: str, session_id: str, edit_existing: bool = True) -> None:
    job = get_job(chat_id)
    if not job or job.get("session_id") != session_id:
        return

    current_path = ensure_root_safe(job["current_path"])
    entries = list_subfolders(current_path)
    page = max(0, int(job.get("page", 0)))
    max_page = max(0, (len(entries) - 1) // PAGE_SIZE)
    page = min(page, max_page)
    can_go_up = os.path.abspath(current_path) != os.path.abspath(ROOT)

    job["current_path"] = current_path
    job["entries"] = entries
    job["page"] = page
    set_job(chat_id, job)

    text = format_picker_text(job["url"], current_path)
    markup = build_keyboard(session_id, entries, page, can_go_up)
    message_id = job.get("message_id")

    if edit_existing and message_id:
        if not edit_message(chat_id, int(message_id), text, markup):
            new_id = send_message(chat_id, text, markup)
            if new_id:
                job["message_id"] = new_id
                set_job(chat_id, job)
    else:
        new_id = send_message(chat_id, text, markup)
        if new_id:
            job["message_id"] = new_id
            set_job(chat_id, job)


def start_folder_picker(chat_id: str, url: str) -> None:
    session_id = uuid.uuid4().hex[:8]
    current_path = ensure_root_safe(ROOT)
    entries = list_subfolders(current_path)

    message_id = send_message(
        chat_id,
        format_picker_text(url, current_path),
        build_keyboard(session_id, entries, 0, can_go_up=False),
    )
    if not message_id:
        return

    job = {
        "session_id": session_id,
        "url": url,
        "kind": classify_source(url),
        "current_path": current_path,
        "entries": entries,
        "page": 0,
        "message_id": message_id,
    }
    set_job(chat_id, job)


# -------------------------
# Progress renderer
# -------------------------
def render_progress(job: Dict[str, Any], st: Dict[str, Any]) -> str:
    kind = job.get("kind", "http")
    dest = job.get("current_path", ROOT)

    status = str(st.get("status", ""))
    total = int(st.get("totalLength", "0") or 0)
    done = int(st.get("completedLength", "0") or 0)
    speed_bps = int(st.get("downloadSpeed", "0") or 0)
    eta_raw = st.get("eta")
    eta = format_eta(int(eta_raw) if str(eta_raw).lstrip("-").isdigit() else None)
    percent = int((done * 100) / total) if total > 0 else 0

    files = st.get("files") or []
    file_name = None
    if isinstance(files, list) and files:
        try:
            file_name = os.path.basename(str(files[0].get("path", ""))) or None
        except Exception:
            file_name = None

    seeds = st.get("numSeeds")
    conns = st.get("connections")
    speed = human_speed(speed_bps)

    if total <= 0 and status in ("waiting", "active"):
        headline = "🧲 Fetching metadata..." if kind in ("magnet", "torrent") else "⬇️ Download in progress"
    else:
        headline = "⬇️ Download in progress"

    total_line = f"📦 Done: {human_size(done)} / {human_size(total)}" if total > 0 else f"📦 Done: {human_size(done)}"
    file_line = f"📄 File: {file_name}\n" if file_name else ""

    seed_line = ""
    if kind in ("magnet", "torrent"):
        parts = []
        if seeds is not None:
            parts.append(f"Seeds: {seeds}")
        if conns is not None:
            parts.append(f"Connections: {conns}")
        if parts:
            seed_line = "🔁 " + " | ".join(parts) + "\n"

    extra = ""
    if total <= 0 and kind in ("magnet", "torrent"):
        extra = "\n⏳ Waiting for torrent metadata / peers"

    return (
        f"{headline}\n"
        f"{file_line}"
        f"📁 {short_path(dest)}\n\n"
        f"[{make_bar(percent)}] {percent}%\n"
        f"{total_line}\n"
        f"{seed_line}\n"
        f"⚡ Speed: {speed}\n"
        f"⏳ ETA: {eta}"
        f"{extra}"
    )


def done_text(dest: str, file_name: Optional[str] = None) -> str:
    fline = f"📄 File:\n{file_name}\n\n" if file_name else ""
    return (
        "✅ Download completed\n"
        f"{fline}"
        f"📁 Saved to:\n{short_path(dest)}\n\n"
        f"[{make_bar(100)}] 100%"
    )


# -------------------------
# Download engine
# -------------------------
def run_download(chat_id: str, job: Dict[str, Any]) -> None:
    def worker() -> None:
        chat_key = str(chat_id)
        with state_lock:
            if chat_key in active_downloads:
                send_message(chat_id, "⚠️ A download is already running.")
                return
            active_downloads[chat_key] = {"kind": "queued"}

        if not ensure_aria2_daemon():
            with state_lock:
                active_downloads.pop(chat_key, None)
            send_message(chat_id, "❌ aria2c is not installed or not available in PATH.")
            return

        url = job["url"]
        kind = job["kind"]
        dest = job["current_path"]
        os.makedirs(dest, exist_ok=True)

        progress_msg_id = send_message(
            chat_id,
            (
                "⬇️ Download started\n"
                f"📁 {short_path(dest)}\n\n"
                f"[{make_bar(0)}] 0%\n"
                f"⚡ Speed: ...\n"
                f"⏳ ETA: ..."
            ),
        )

        gid = None
        try:
            if kind == "magnet":
                gid = aria2_add_magnet(url, dest)
            elif kind == "torrent":
                gid = aria2_add_torrent_from_url(url, dest)
            else:
                gid = aria2_add_http(url, dest)

            with state_lock:
                active_downloads[chat_key] = {"kind": kind, "gid": gid}

            last_text = ""
            last_update = 0.0

            while True:
                if stop_event.is_set():
                    raise RuntimeError("Stopped")

                st = aria2_status(gid)
                status = str(st.get("status", ""))
                total = int(st.get("totalLength", "0") or 0)
                done = int(st.get("completedLength", "0") or 0)

                # Finish as soon as payload is complete. seed-time=0 keeps torrent jobs from lingering.
                if total > 0 and done >= total and status in ("complete", "active", "waiting"):
                    file_name = None
                    files = st.get("files") or []
                    if isinstance(files, list) and files:
                        try:
                            file_name = os.path.basename(str(files[0].get("path", ""))) or None
                        except Exception:
                            file_name = None

                    if progress_msg_id:
                        edit_message(chat_id, progress_msg_id, done_text(dest, file_name))
                    else:
                        send_message(chat_id, done_text(dest, file_name))

                    aria2_remove(gid)
                    return

                text = render_progress(job, st)
                now = time.time()
                if text != last_text and (now - last_update) >= PROGRESS_UPDATE_SECONDS:
                    last_text = text
                    last_update = now
                    if progress_msg_id:
                        if not edit_message(chat_id, progress_msg_id, text):
                            progress_msg_id = send_message(chat_id, text)
                    else:
                        progress_msg_id = send_message(chat_id, text)

                if status == "error":
                    err = st.get("errorMessage") or "aria2 failed"
                    raise RuntimeError(str(err))

                if status == "complete":
                    file_name = None
                    files = st.get("files") or []
                    if isinstance(files, list) and files:
                        try:
                            file_name = os.path.basename(str(files[0].get("path", ""))) or None
                        except Exception:
                            file_name = None

                    if progress_msg_id:
                        edit_message(chat_id, progress_msg_id, done_text(dest, file_name))
                    else:
                        send_message(chat_id, done_text(dest, file_name))
                    aria2_remove(gid)
                    return

                time.sleep(1.0)

        except Exception as e:
            err_msg = f"❌ Download failed\n\n{e}"
            if progress_msg_id:
                if not edit_message(chat_id, progress_msg_id, err_msg):
                    send_message(chat_id, err_msg)
            else:
                send_message(chat_id, err_msg)

        finally:
            with state_lock:
                active_downloads.pop(chat_key, None)

    threading.Thread(target=worker, daemon=True).start()


# -------------------------
# Telegram handlers
# -------------------------
def handle_text_message(message: Dict[str, Any]) -> None:
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))

    if not is_authorized(chat_id):
        return

    text = escape_text(message.get("text", ""))
    if not text:
        return

    lower = text.lower()

    if lower in ("/start", "/help"):
        send_message(
            chat_id,
            "Send a direct download link.\n"
            "HTTP/HTTPS, magnet, or .torrent URLs are supported.\n"
            "I will show the folder picker, then tap ✅ Save here.\n\n"
            "Commands:\n"
            "/cancel - cancel the current selection"
        )
        return

    if lower == "/cancel":
        clear_job(chat_id)
        send_message(chat_id, "🛑 Current selection cleared.")
        return

    if lower.startswith("/download"):
        url = extract_url(text)
        if not url:
            send_message(chat_id, "Send /download followed by a valid http(s), magnet, or .torrent link.")
            return
        start_folder_picker(chat_id, url)
        return

    url = extract_url(text)
    if url:
        start_folder_picker(chat_id, url)


def handle_callback(callback: Dict[str, Any]) -> None:
    callback_id = callback.get("id", "")
    data = callback.get("data", "")
    from_user = callback.get("from", {})
    chat_id = str(from_user.get("id", ""))

    if not is_authorized(chat_id):
        answer_callback(callback_id, "Unauthorized", True)
        return

    if not data:
        answer_callback(callback_id, "Empty action", False)
        return

    parts = data.split(":")
    action = parts[0]
    session_id = parts[1] if len(parts) > 1 else ""

    job = get_job(chat_id)
    if not job or job.get("session_id") != session_id:
        answer_callback(callback_id, "Selection expired", False)
        return

    current_path = ensure_root_safe(job["current_path"])
    entries = list_subfolders(current_path)
    page = max(0, int(job.get("page", 0)))
    max_page = max(0, (len(entries) - 1) // PAGE_SIZE)
    page = min(page, max_page)

    if action == "nav":
        try:
            idx = int(parts[2])
            start = page * PAGE_SIZE
            visible = entries[start:start + PAGE_SIZE]
            if idx < 0 or idx >= len(visible):
                answer_callback(callback_id, "Folder not found", True)
                return
            new_path = safe_join(current_path, visible[idx])
            job["current_path"] = new_path
            job["page"] = 0
            set_job(chat_id, job)
            render_picker(chat_id, session_id, edit_existing=True)
            answer_callback(callback_id, f"Opened {visible[idx]}", False)
        except Exception:
            answer_callback(callback_id, "Folder unavailable", True)
        return

    if action == "next":
        job["page"] = min(max_page, page + 1)
        set_job(chat_id, job)
        render_picker(chat_id, session_id, edit_existing=True)
        answer_callback(callback_id, "Next page", False)
        return

    if action == "prev":
        job["page"] = max(0, page - 1)
        set_job(chat_id, job)
        render_picker(chat_id, session_id, edit_existing=True)
        answer_callback(callback_id, "Previous page", False)
        return

    if action == "up":
        if os.path.abspath(current_path) == os.path.abspath(ROOT):
            answer_callback(callback_id, "Already at root", False)
            return
        parent = ensure_root_safe(os.path.dirname(current_path.rstrip(os.sep)))
        job["current_path"] = parent
        job["page"] = 0
        set_job(chat_id, job)
        render_picker(chat_id, session_id, edit_existing=True)
        answer_callback(callback_id, "Moved up", False)
        return

    if action == "save":
        answer_callback(callback_id, "Starting download", False)
        url = job["url"]
        dest = current_path
        message_id = job["message_id"]
        try:
            edit_message(chat_id, message_id, f"✅ Download queued\n📁 Folder:\n{short_path(dest)}\n\n🔗 URL:\n{url}")
        except Exception:
            pass

        clear_job(chat_id)
        run_download(chat_id, job)
        return

    if action == "cancel":
        clear_job(chat_id)
        try:
            edit_message(chat_id, job["message_id"], "🛑 Selection cancelled.")
        except Exception:
            pass
        answer_callback(callback_id, "Cancelled", False)
        return

    answer_callback(callback_id, "Unknown action", False)


# -------------------------
# Startup / services
# -------------------------
def setup_filebrowser() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH):
        subprocess.run(["filebrowser", "config", "init"], cwd=DATA_DIR, check=False)
        fb_password = FB_PASSWORD.strip() or "admin123"
        subprocess.run(
            ["filebrowser", "users", "add", USERNAME, fb_password, "--perm.admin"],
            cwd=DATA_DIR,
            check=False,
        )


def start_filebrowser() -> None:
    global fb_proc
    fb_proc = subprocess.Popen(
        ["filebrowser", "-r", ROOT, "-p", PORT],
        cwd=DATA_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def filebrowser_watchdog() -> None:
    while not stop_event.is_set():
        try:
            if fb_proc is None or fb_proc.poll() is not None:
                start_filebrowser()
        except Exception:
            pass
        time.sleep(5)


def tunnel_worker() -> None:
    global cf_proc, last_announced_link, startup_sent

    while not stop_event.is_set():
        try:
            cf_proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{PORT}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            tunnel_link = None
            if cf_proc.stdout:
                for line in cf_proc.stdout:
                    if stop_event.is_set():
                        break
                    match = re.search(LINK_REGEX, line)
                    if match:
                        tunnel_link = match.group(0)
                        break

            if tunnel_link and tunnel_link != last_announced_link:
                last_announced_link = tunnel_link
                if not startup_sent:
                    send_message(CHAT_ID, "🚀 Server Started")
                    startup_sent = True
                send_message(CHAT_ID, f"📂 File Server:\n{tunnel_link}")

            if cf_proc and cf_proc.poll() is None:
                cf_proc.wait()

        except Exception:
            time.sleep(3)

        if not stop_event.is_set():
            time.sleep(3)


def terminate_process(proc: Optional[subprocess.Popen]) -> None:
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            time.sleep(1)
            if proc.poll() is None:
                proc.kill()
    except Exception:
        pass


def stop(*_args: Any) -> None:
    stop_event.set()

    try:
        aria2_shutdown()
    except Exception:
        pass

    terminate_process(fb_proc)
    terminate_process(cf_proc)
    terminate_process(aria2_proc)
    raise SystemExit(0)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def bot_loop() -> None:
    offset: Optional[int] = None
    while not stop_event.is_set():
        try:
            params: Dict[str, Any] = {
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            }
            if offset is not None:
                params["offset"] = offset

            res = telegram_api("getUpdates", params, timeout=35)
            if not res.get("ok"):
                time.sleep(2)
                continue

            for upd in res.get("result", []):
                offset = int(upd["update_id"]) + 1
                if "message" in upd:
                    handle_text_message(upd["message"])
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])

        except Exception:
            time.sleep(2)


def main() -> None:
    subprocess.run(["termux-wake-lock"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    setup_filebrowser()
    start_filebrowser()
    time.sleep(2)

    threads = [
        threading.Thread(target=filebrowser_watchdog, daemon=True),
        threading.Thread(target=tunnel_worker, daemon=True),
        threading.Thread(target=bot_loop, daemon=True),
    ]
    for t in threads:
        t.start()

    while not stop_event.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()