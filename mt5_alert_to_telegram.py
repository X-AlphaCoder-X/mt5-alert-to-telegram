"""
MT5 Alert to Telegram: forwards MetaTrader 5 Alert() messages to Telegram chats. Windows only.

Install:  pip install -r requirements.txt
Run:      run.bat, or  python mt5_alert_to_telegram.py

WHAT IT DOES
  * SOURCES  : one or more MT5 terminals (each is watched on its own)
  * CHANNELS : one or more Telegram chats (each may use its own bot token)
  * ROUTES   : which source feeds which channel (one source to several channels,
               several sources into one channel, and so on)
  * CAPTURE  : popup grabber (fast, about 0.2s) and/or Experts-log reader.
               Both ON = the log is a SAFETY NET: when an alert shows up in the
               log (MT5 writes its log in delayed bursts, often 10-50s late) and
               it is not older than the recovery window (default 60s) and the same
               text was not already delivered around that time, it is sent.

Alert text from the log is whatever follows "Alert:", without the time or the EA name.
The tuning switches near the top of this file are explained where they are set.

LOGGING: ./logs/log<year>-<month>-<day>.log next to this script (also on screen).
Files 2+ days old are deleted automatically. Bot tokens are hidden in logs.
Settings: mt5_alert_to_telegram_settings.json next to this script. Everything in it,
bot tokens included, is stored as plain text.
"""
import atexit
import codecs
import collections
import copy
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, namedtuple
from datetime import date, datetime

import psutil
import requests
from pywinauto import Desktop

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "mt5_alert_to_telegram_settings.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
LOG_KEEP_DAYS = 2                  # log files this many days old (or older) are deleted
LOG_NAME_RE = re.compile(r"^log(\d{4})-(\d{1,2})-(\d{1,2})\.log$")
HEARTBEAT_SECONDS = 30

MISS_LIMIT = 20                    # polls a popup may be unreadable before we forget its handle
LIST_TYPES = {"List", "DataGrid", "Table"}      # the history list under the latest alert
TEXT_TYPES = {"Text", "Edit", "Document"}       # controls that can hold alert text
LIST_CLASS_HINTS = ("listview", "listbox", "syslistview")
TS_RE = re.compile(r"^(?:\d{4}\.\d{2}\.\d{2} )?\d{2}:\d{2}:\d{2}")
ALERT_RE = re.compile(r"\tAlert:[ \t]?")
TG_MAX_TEXT = 4096                 # Telegram hard limit per message
TG_MIN_GAP = 1.1                   # Telegram: ~1 message per second per chat
LOG_DIR_RECHECK = 15.0            # seconds between checks that MT5 still writes its Experts log where we read it
# ======================================================================================================
# TUNING SWITCHES - edit the value, save the file, restart the script.
# ======================================================================================================
#
# (1) POPUP CAME BACK WITH THE SAME TEXT: is it a NEW alert or the one already sent?
#     This happens when the MT5 Alert window is unreadable for a moment (MT5 busy, UI Automation hiccup) or is
#     not detected for a moment, and then shows the alert that was ALREADY forwarded. Old versions sent it again.
#
#     RESEND_GUARD = True    protection ON  (recommended): same text back = same alert, NOT sent again.
#     RESEND_GUARD = False   protection OFF = old behaviour: sent again every time the popup comes back.
#
#     LOST_GRACE (seconds) only matters while RESEND_GUARD is True, and only for a popup that was really CLOSED
#     / not detected at all: back with the same text within LOST_GRACE seconds = same alert (not sent), later =
#     a new alert (sent). So LOST_GRACE = 0 means "a popup that was closed always counts as new".
#     A popup that stays OPEN but unreadable is NOT governed by LOST_GRACE: it is protected whenever
#     RESEND_GUARD is True. (So RESEND_GUARD = False is NOT the same as LOST_GRACE = 0: it switches off
#     everything, including that case.)
#
#       RESEND_GUARD  LOST_GRACE | closed, back in <= LOST_GRACE | closed, back later | open but unreadable
#       True          10.0       | NOT sent                      | sent                | NOT sent    <- default
#       True          0          | sent                          | sent                | NOT sent
#       False         (ignored)  | sent                          | sent                | sent        <- old behaviour
#
#     New text, or a longer alert list, is ALWAYS sent in every setting. If the guard ever skips a real repeat,
#     the Experts-log safety net still recovers it (as long as log capture is ON).
RESEND_GUARD = True
LOST_GRACE = 0.0

# (2) MIDNIGHT. MT5 writes its Experts log in delayed bursts, so lines written just before midnight can land in
#     yesterday's file AFTER midnight. For ROLLOVER_GRACE seconds after midnight the script keeps reading
#     yesterday's file as well as today's (every line is still read exactly once). 0 = old behaviour (jump to
#     the new file at once and never look back).
ROLLOVER_GRACE = 600.0

# (3) MULTI-LINE ALERTS. Alert("line1\nline2") shows as ONE alert in MT5 but is written to the log as separate
#     lines with the same time and source. Only the first one carries "Alert:". With MERGE_ALERT_LINES = True the
#     lines that follow it (same time + source, no "Alert:" of their own) are joined back into one message.
#     A line that only repeats a line the alert already has (the usual Alert(msg) then Print(msg) pair) is not
#     joined, and at most MAX_ALERT_LINES lines are joined. False = old behaviour (first line only).
#     Known limit: a Print() written in the very same millisecond with different text cannot be told apart
#     from a real 2nd line, so it may end up as an extra line of a log-recovered alert (never lost, never doubled).
MERGE_ALERT_LINES = True
MAX_ALERT_LINES = 12
CONT_HOLD = 2.2        # wait this long after an Alert line for its 2nd+ lines (must be > IDLE_FLUSH)
IDLE_FLUSH = 1.5       # silence after which the LAST log entry counts as complete (MT5 may flush a 2-line alert in two bursts)

# (4) "ALREADY SENT?" CHECK of the log safety net. A log alert counts as delivered if the popup sent the same
#     alert from -DUP_BEFORE to +DUP_AFTER seconds around the time MT5 wrote it. Texts count as the same alert
#     when one is the leading lines of the other (the popup shows the whole message, the log may hold only part
#     of it). ONE popup send covers only ONE log alert, so identical alerts from several charts in a moment are
#     each recovered if the popup could not read them all.
DUP_BEFORE = 5.0
DUP_AFTER = 30.0
CREDIT_SECONDS = 6.0   # a list row may show up this long after its alert text already did (same alert, not a new one)
# CONT_TIME_TOL / SHAPE_WINDOW: which log lines belong to ONE multi-line Alert() message.
#     MT5 writes such a message as separate log lines that carry the same time or are one millisecond apart
#     (06:52:44.182 + 06:52:44.183 in a 22-chart start-up burst. 10:39:11.791 + 10:39:11.791 elsewhere). The EA's own
#     Print() calls right after it ("Version 6.166", "Start with signal #...") are separate calls with a later time,
#     sometimes only a few ms later (one landed inside 50 ms and was glued onto an alert). Time alone cannot decide, so:
#       * KNOWN message - the popup has shown this alert before, and a popup shows the WHOLE message: the log lines
#         are matched by TEXT. The next line is joined only if it is the line the popup showed (within SHAPE_WINDOW
#         seconds), and nothing more is joined once the message is complete. This is exact.
#       * UNKNOWN message (never seen in a popup yet, e.g. right after the script started): lines up to
#         CONT_TIME_TOL seconds after the Alert are joined. 0.002 = 2 ms: covers the 1 ms gap seen. (0.05 glued
#         "Version 6.166" onto an alert. 0.0 left a real 1 ms-late 2nd line out.) Lower = fewer wrong joins,
#         higher = fewer cut-off messages.
CONT_TIME_TOL = 0.002
SHAPE_WINDOW = 1.0

# (5) BURSTS OF IDENTICAL ALERTS. When MT5 starts, every EA on every chart can raise the SAME alert at once (22 times in
#     a quarter of a second with 15 charts). The text does not say which chart it came from, so the channel would get
#     22 identical messages. IDENTICAL_COOLDOWN = 10 sends such an alert ONCE: while an alert with exactly the same
#     text was already delivered to that channel (same source) within +-10 seconds, further identical ones are held
#     back and written to the log as "identical alert suppressed". The same text again LATER than that is sent as
#     usual, and different text is never held back. 0 = off (the default): every alert MT5 raises is sent, however
#     identical, and none is ever held back. Use 10 if you would rather get a startup burst once.
IDENTICAL_COOLDOWN = 0.0

# (6) TELEGRAM FLOOD LIMITS. Telegram lets a bot send only about 20 messages per MINUTE into one group/channel (and
#     about 1 per second). A 22-alert startup burst went over it: the 21st message was rejected with "429 retry after 33".
#     TG_BURST_LIMIT = 19 makes the script itself pause when 19 messages already went into a chat within the last
#     TG_BURST_WINDOW seconds, so nothing is rejected. The waiting alerts go out in order as soon as the window allows.
#     0 = off. If Telegram DOES answer 429 anyway, the script now waits the full time Telegram asks for.
TG_BURST_LIMIT = 19
TG_BURST_WINDOW = 60.0
TG_RETRY_AFTER_CAP = 300     # longest 429 wait we are willing to sit out (seconds)
UNREADABLE_WARN_AFTER = 3.0  # a popup that is being rebuilt reads empty for a moment: warn only if it stays empty this long

DEFAULTS = {
    "bot_token": "",               # default bot, used by channels without their own token
    "channels": [],                # [{"name", "chat_id", "bot_token" (optional)}]
    "sources": [],                 # [{"name", "exe", "log_dir" (optional manual Experts log folder)}]
    "routes": [],                  # [{"source": name, "channel": name}]
    "popup_enabled": True,
    "log_enabled": False,
    "recover_seconds": 60,         # both ON: log alerts older than this are ignored
    "window_title": "Alert",
    "global_mode": True,           # True = forward ALL alerts, no keyword needed
    "keyword": "",                 # keyword mode only. Blank = no keyword
    "require_text": "",            # popup + keyword mode only: text that marks the top block vs list rows
    "poll_seconds": 0.15,
    "send_on_update": True,
    "send_existing_on_start": False,
}

STATS = Counter()


# ------------------------------------------------------------ logging
_log_lock = threading.Lock()
_log_day = None
_redact = []
_once = set()


def log_file_for(d):
    return os.path.join(LOG_DIR, f"log{d.year}-{d.month}-{d.day}.log")


def cleanup_old_logs(keep_days=LOG_KEEP_DAYS, today=None):
    """Delete log<Y>-<M>-<D>.log files that are keep_days days old or older."""
    today = today or date.today()
    removed = []
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return removed
    for n in names:
        m = LOG_NAME_RE.match(n)
        if not m:
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if (today - d).days >= keep_days:
            try:
                os.remove(os.path.join(LOG_DIR, n))
                removed.append(n)
            except OSError:
                pass
    return removed


def set_redact(cfg):
    toks = [cfg.get("bot_token", "")] + [c.get("bot_token", "") for c in cfg.get("channels", [])]
    _redact[:] = [t for t in toks if t]


_console_q = queue.Queue(maxsize=2000)
_console_lock = threading.Lock()
_console_state = {"thread": None, "dropped": 0}


def _console_print(line):
    try:
        print(line)
    except UnicodeEncodeError:               # console / redirect that can't show emoji, Kurdish, ...
        try:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(line.encode(enc, "replace").decode(enc, "replace"))
        except Exception:
            pass
    except Exception:
        pass                                 # a broken console must never stop alert delivery


def _console_writer():
    """Only this thread ever waits on the console. Windows freezes a console's output while text is selected
    in it (QuickEdit mode) until a key is pressed. If the alert threads printed directly they would freeze
    too, and the Telegram send would wait for you to press a key."""
    while True:
        line = _console_q.get()
        try:
            if _console_state["dropped"]:
                n, _console_state["dropped"] = _console_state["dropped"], 0
                _console_print(f"(console was paused: {n} line(s) not shown here, they are all in the log file)")
            _console_print(line)
        except Exception:
            pass
        finally:
            _console_q.task_done()


def _console_wait(seconds):
    end = time.time() + seconds
    while not _console_q.empty() and time.time() < end:
        time.sleep(0.01)


def _console_put(line):
    t = _console_state["thread"]
    if t is None or not t.is_alive():
        with _console_lock:
            t = _console_state["thread"]
            if t is None or not t.is_alive():
                t = threading.Thread(target=_console_writer, daemon=True, name="console-writer")
                _console_state["thread"] = t
                t.start()
    try:
        _console_q.put_nowait(line)
    except queue.Full:
        _console_state["dropped"] += 1


atexit.register(_console_wait, 1.5)          # let the last lines reach the screen when the script ends


def log(msg, level="INFO", console=True):
    """Write to the daily log file and (optionally) the screen. Never raises."""
    global _log_day
    now = datetime.now()
    msg = str(msg)
    for secret in _redact:
        msg = msg.replace(secret, "***")
    line = f"{now.strftime('%Y-%m-%d %H:%M:%S')}.{now.microsecond // 1000:03d} [{level}] {msg}"
    if console and level != "DEBUG":              # DEBUG (e.g. every MT5 window title) is for the file only
        if threading.current_thread() is threading.main_thread():
            _console_wait(0.2)                   # the menu: keep its lines in order, it is not on the alert path
            _console_print(line)
        else:
            _console_put(line)                   # alert threads never wait for the screen
    with _log_lock:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            removed = []
            if _log_day != now.date():
                _log_day = now.date()
                removed = cleanup_old_logs(today=now.date())
            with open(log_file_for(now), "a", encoding="utf-8") as f:
                f.write(line + "\n")
                if removed:
                    f.write(f"{now.strftime('%Y-%m-%d %H:%M:%S')}.000 [INFO] deleted old log files: {', '.join(removed)}\n")
        except OSError as e:
            if console:
                print(f"(could not write log file: {e})")


def log_once(key, msg, level="INFO"):
    if key not in _once:
        _once.add(key)
        log(msg, level)


def log_exception(where):
    log(f"EXCEPTION in {where}:\n{traceback.format_exc()}", "ERROR")


# ------------------------------------------------------------ settings
LEGACY_KEYS = ("chat_id", "terminal_exe", "log_mode", "log_dir", "send_on_update_legacy")


def migrate(cfg, raw):
    """Upgrade settings saved by older versions (single terminal / single chat)."""
    changed = False
    if raw.get("chat_id") and not cfg["channels"]:
        cfg["channels"].append({"name": "default", "chat_id": str(raw["chat_id"]), "bot_token": ""})
        changed = True
    if raw.get("terminal_exe") and not cfg["sources"]:
        exe = raw["terminal_exe"]
        name = os.path.basename(os.path.dirname(exe)) or "MT5"
        cfg["sources"].append({"name": name, "exe": exe, "log_dir": raw.get("log_dir", "")})
        changed = True
    if changed and not cfg["routes"] and len(cfg["sources"]) == 1 and len(cfg["channels"]) == 1:
        cfg["routes"].append({"source": cfg["sources"][0]["name"], "channel": cfg["channels"][0]["name"]})
    if "popup_enabled" not in raw and raw.get("log_mode"):
        cfg["popup_enabled"], cfg["log_enabled"] = False, True
        changed = True
    return changed


def load_settings():
    cfg = copy.deepcopy(DEFAULTS)
    raw = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg.update({k: v for k, v in raw.items() if k not in LEGACY_KEYS})
        except Exception as e:
            bad = f"{SETTINGS_FILE}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                shutil.copy2(SETTINGS_FILE, bad)
                print(f"Could not read settings ({e}). A copy of that file was kept as {bad}. Using defaults.")
            except OSError:
                print(f"Could not read settings ({e}), using defaults.")
    if migrate(cfg, raw):
        save_settings(cfg)
        log("settings migrated to the multi-source / multi-channel format", console=False)
    return cfg


def mask(token):
    if not token:
        return "(not set)"
    return token[:6] + "..." + token[-4:] if len(token) > 12 else "***"


def save_settings(cfg):
    tmp = SETTINGS_FILE + ".tmp"            # write the new file completely, then swap: a crash can't leave half a file
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(5):
        try:
            os.replace(tmp, SETTINGS_FILE)
            break
        except PermissionError:              # Windows: antivirus / indexer may hold the file for a moment
            if attempt == 4:                 # last resort = the old way, so saving is never worse than before
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            else:
                time.sleep(0.1 * (attempt + 1))
    set_redact(cfg)
    shown = copy.deepcopy(cfg)
    shown["bot_token"] = mask(cfg.get("bot_token", ""))
    for c in shown["channels"]:
        c["bot_token"] = mask(c.get("bot_token", "")) if c.get("bot_token") else ""
    log(f"settings saved: {json.dumps(shown, ensure_ascii=False)}", console=False)


# ------------------------------------------------------------ sources / channels / routes helpers
def source_by_name(cfg, name):
    return next((s for s in cfg["sources"] if s["name"] == name), None)


def channel_by_name(cfg, name):
    return next((c for c in cfg["channels"] if c["name"] == name), None)


def channels_for(cfg, source_name):
    """Channels that receive alerts from this source (route order, no duplicates)."""
    out, seen = [], set()
    for r in cfg["routes"]:
        if r["source"] == source_name and r["channel"] not in seen:
            ch = channel_by_name(cfg, r["channel"])
            if ch:
                out.append(ch)
                seen.add(r["channel"])
    return out


def token_for(cfg, ch):
    return ch.get("bot_token") or cfg.get("bot_token", "")


def unique_name(items, base):
    names = {i["name"] for i in items}
    if base not in names:
        return base
    n = 2
    while f"{base} {n}" in names:
        n += 1
    return f"{base} {n}"


# ------------------------------------------------------------ terminals
TERMINAL_NAMES = ("terminal64.exe", "terminal.exe")


def _terminals_fast():
    found = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if (p.info["name"] or "").lower() in TERMINAL_NAMES:
                found.append((p.info["pid"], p.info["exe"] or "unknown path"))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found


def _terminals_slow():
    """One process at a time: a single odd system/elevated process can't break the whole scan."""
    found = []
    for pid in psutil.pids():
        try:
            p = psutil.Process(pid)
            if (p.name() or "").lower() not in TERMINAL_NAMES:
                continue
            try:
                exe = p.exe()
            except Exception:
                exe = ""
            found.append((pid, exe or "unknown path"))
        except Exception:
            continue
    return found


def list_terminals():
    try:
        return _terminals_fast()
    except Exception as e:                       # psutil can raise on some Windows processes
        log_once(("psutil-fast", repr(e)), f"psutil process scan failed ({e!r}), using the per-process scan", "WARN")
        try:
            return _terminals_slow()
        except Exception as e2:
            log_once(("psutil-slow", repr(e2)), f"process scan failed: {e2!r}", "ERROR")
            return []


def get_cmdline(pid):
    """Command line of a process, or None if it can't be read (e.g. elevated)."""
    try:
        return psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def is_portable_cmdline(cl):
    return bool(cl) and any(a.strip('"').lower() == "/portable" for a in cl)


def pick_terminal():
    """Returns (pid, exe) or None if the user backs out."""
    while True:
        terms = list_terminals()
        if not terms:
            ans = input("No running MT5 terminal found. Start MT5 then press Enter to rescan (b = back): ")
            if ans.strip().lower() == "b":
                return None
            continue
        print("\nRunning MT5 terminals:")
        for i, (pid, exe) in enumerate(terms, 1):
            tag = " [portable]" if is_portable_cmdline(get_cmdline(pid)) else ""
            print(f"  {i}. PID {pid}  {exe}{tag}")
        choice = input("Pick a terminal number (r = rescan, b = back): ").strip().lower()
        if choice == "b":
            return None
        if choice == "r":
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(terms):
            return terms[int(choice) - 1]
        print("Invalid choice.")


def resolve_source_pid(src):
    """PID of the running terminal for this source (matched by exe path), or None."""
    exe = os.path.normcase(src["exe"])
    unreadable = False
    for pid, e in list_terminals():
        if os.path.normcase(e) == exe:
            return pid
        unreadable = unreadable or e == "unknown path"
    if unreadable:
        log_once(("unknownexe", src["name"]),
                 f"[{src['name']}] a terminal process is running but its file path can't be read "
                 "(usually MT5 was started as Administrator): run this script as Administrator too", "WARN")
    return None


# ------------------------------------------------------------ data folder / portable
def read_text_file(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace").strip()
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace").strip()
    if len(raw) > 1 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le", errors="replace").strip()
    return raw.decode("utf-8", errors="replace").strip()


def newest_log_mtime(logs_dir):
    try:
        return max(os.path.getmtime(os.path.join(logs_dir, n)) for n in os.listdir(logs_dir))
    except (OSError, ValueError):
        return 0


def find_data_dir(exe, cmdline):
    """
    Returns (data_dir, portable) for a terminal.
    Portable  : data folder = the MT5 install folder (terminal started with /portable).
    Normal    : %APPDATA%\\MetaQuotes\\Terminal\\<hash>, matched through origin.txt.
    cmdline None (unreadable) -> pick whichever candidate has the freshest Experts log.
    """
    install = os.path.dirname(exe)
    portable_dir = install if os.path.isdir(os.path.join(install, "MQL5")) else None

    appdata_dir, best_m = None, -1
    base = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal")
    if os.path.isdir(base):
        want = os.path.normcase(os.path.normpath(install))
        for name in os.listdir(base):
            d = os.path.join(base, name)
            origin = os.path.join(d, "origin.txt")
            if os.path.isfile(origin):
                got = os.path.normcase(os.path.normpath(read_text_file(origin)))
                if got == want:
                    m = newest_log_mtime(os.path.join(d, "MQL5", "Logs"))
                    if m > best_m:
                        appdata_dir, best_m = d, m

    if cmdline is not None:
        if is_portable_cmdline(cmdline):
            return install, True
        if appdata_dir:
            return appdata_dir, False
        return (portable_dir, True) if portable_dir else (None, False)

    cands = []
    if portable_dir:
        cands.append((newest_log_mtime(os.path.join(portable_dir, "MQL5", "Logs")), portable_dir, True))
    if appdata_dir:
        cands.append((newest_log_mtime(os.path.join(appdata_dir, "MQL5", "Logs")), appdata_dir, False))
    if not cands:
        return None, False
    cands.sort(reverse=True)
    return cands[0][1], cands[0][2]


def resolve_log_dir_for(src):
    """Returns (logs_dir, portable_or_None, note) for a source."""
    if src.get("log_dir"):
        return src["log_dir"], None, "manual folder"
    pid = resolve_source_pid(src)
    cl = get_cmdline(pid) if pid else None
    data_dir, portable = find_data_dir(src["exe"], cl)
    if not data_dir:
        return None, None, "could not locate the MT5 data folder (set the log folder manually)"
    return os.path.join(data_dir, "MQL5", "Logs"), portable, "portable" if portable else "normal install"


# ------------------------------------------------------------ raw Win32 helpers (no pywinauto)
def win32_top_windows(pid):
    """[(hwnd, title, visible)] of every top-level window owned by this process."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        out = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lp):
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid:
                n = u.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 2)
                u.GetWindowTextW(hwnd, buf, n + 2)
                out.append((int(hwnd), buf.value, bool(u.IsWindowVisible(hwnd))))
            return True

        u.EnumWindows(enum_proc(cb), 0)
        return out
    except Exception as e:
        log_once(("w32top", repr(e)), f"win32 window enumeration failed: {e!r}", "WARN")
        return []


def _setup_sendmessage(u):
    import ctypes
    from ctypes import wintypes
    u.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    u.SendMessageTimeoutW.restype = ctypes.c_ssize_t


def win32_children(hwnd):
    """[(class_name, text, top_y)] for every child control, text fetched with WM_GETTEXT."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        _setup_sendmessage(u)
        out = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(child, _lp):
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(child, cls, 256)
            buf = ctypes.create_unicode_buffer(8192)
            res = ctypes.c_size_t(0)
            u.SendMessageTimeoutW(child, 0x000D, 8192, buf, 2, 150, ctypes.byref(res))  # WM_GETTEXT, SMTO_ABORTIFHUNG
            rect = wintypes.RECT()
            u.GetWindowRect(child, ctypes.byref(rect))
            out.append((cls.value, buf.value, rect.top))
            return True

        u.EnumChildWindows(hwnd, enum_proc(cb), 0)
        return out
    except Exception as e:
        log_once(("w32child", repr(e)), f"win32 child enumeration failed: {e!r}", "WARN")
        return []


def win32_list_count(hwnd):
    """Number of rows in the popup's alert list (SysListView32), or None if unavailable."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        _setup_sendmessage(u)
        found = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(child, _lp):
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(child, cls, 256)
            if "listview" in cls.value.lower():
                found.append(child)
                return False
            return True

        u.EnumChildWindows(hwnd, enum_proc(cb), 0)
        if not found:
            return None
        res = ctypes.c_size_t(0)
        ok = u.SendMessageTimeoutW(found[0], 0x1004, 0, None, 2, 150, ctypes.byref(res))  # LVM_GETITEMCOUNT
        return int(res.value) if ok else None
    except Exception as e:
        log_once(("w32count", repr(e)), f"win32 list row count failed: {e!r}", "WARN")
        return None


# ------------------------------------------------------------ popup reading
def clean(text):
    lines = [" ".join(line.split()) for line in text.replace("\r", "").split("\n")]
    return "\n".join(l for l in lines if l)


def norm_key(text):
    """Comparison key for 'is this the same alert text' (whitespace-insensitive)."""
    return " ".join(text.split())


def pick_alert_text(texts, list_tops, cfg):
    """
    Choose the latest-alert text from a popup.
    texts: [(top_y, text)] for every text control, and list_tops: [top_y] of list-like controls.
    Global mode: everything above the history list (or just the topmost text if no
    list is found). Keyword mode: topmost block containing the required text + keyword.
    """
    texts = sorted(((top, clean(t)) for top, t in texts if clean(t)), key=lambda x: x[0])
    if not texts:
        return None
    if cfg["global_mode"]:
        if list_tops:
            cutoff = min(list_tops)
            above = [t for top, t in texts if top < cutoff]
            # list found but nothing above it = popup is being (re)built: no alert text yet
            return "\n".join(above) if above else None
        return texts[0][1]
    req, kw = cfg["require_text"].lower(), cfg["keyword"].lower()
    for _, t in texts:
        low = t.lower()
        if (not req or req in low) and (not kw or kw in low):
            return t
    return None


def find_popups(get_desktop, pid, title):
    """UIA top-level windows of this terminal whose title contains `title` (case-insensitive)."""
    try:
        wins = get_desktop().windows(process=pid)
    except Exception as e:
        log_once(("enum", repr(e)), f"UIA could not list windows of PID {pid}: {e!r}", "WARN")
        return []
    want = title.strip().casefold()
    out = []
    for w in wins:
        try:
            if want and want in w.window_text().strip().casefold():
                out.append(w)
        except Exception:
            continue
    return out


def uia_read(w, cfg):
    texts, list_tops = [], []
    for c in w.descendants():
        ctype = c.element_info.control_type
        if ctype in TEXT_TYPES:
            t = c.window_text() or c.element_info.name or ""
            texts.append((c.rectangle().top, t))
        elif ctype in LIST_TYPES:
            list_tops.append(c.rectangle().top)
    return pick_alert_text(texts, list_tops, cfg), len(texts)


def win32_read(hwnd, cfg):
    kids = win32_children(hwnd)
    # the history list is named "Alerts", its header and the Close button have captions too: none of them is alert text
    skip = ("button", "sysheader") + LIST_CLASS_HINTS
    texts = [(top, t) for cls, t, top in kids if not any(cls.lower().startswith(k) or k in cls.lower() for k in skip)]
    list_tops = [top for cls, _t, top in kids if any(h in cls.lower() for h in LIST_CLASS_HINTS)]
    return pick_alert_text(texts, list_tops, cfg)


_snapped = set()
_unreadable_since = {}             # hwnd -> when its text first could not be read


def snapshot_popup(hwnd, uia_win=None):
    """Log everything we can see inside a popup, once per window handle."""
    if hwnd in _snapped:
        return
    _snapped.add(hwnd)
    lines = [f"POPUP SNAPSHOT hwnd={hwnd}"]
    if uia_win is not None:
        try:
            rows = []
            for c in uia_win.descendants():
                rows.append(
                    f"   UIA {c.element_info.control_type} name={c.element_info.name!r:.120} "
                    f"text={c.window_text()!r:.120} top={c.rectangle().top}"
                )
            lines += rows[:40]
            if len(rows) > 40:
                lines.append(f"   ... {len(rows) - 40} more UIA controls")
            if not rows:
                lines.append("   UIA: no child controls exposed")
        except Exception as e:
            lines.append(f"   UIA enumeration error: {e!r}")
    else:
        lines.append("   UIA: not needed/used (plain Win32 read was enough)")
    kids = win32_children(hwnd)
    for cls, txt, top in kids[:40]:
        lines.append(f"   WIN32 class={cls} text={txt!r:.120} top={top}")
    if len(kids) > 40:
        lines.append(f"   ... {len(kids) - 40} more Win32 controls")
    if not kids:
        lines.append("   WIN32: no child controls")
    log("\n".join(lines))


ReadResult = namedtuple("ReadResult", "texts popups n_top counts")


def read_windows(get_desktop, pid, cfg):
    """
    Plain Win32 first (fast), pywinauto/UIA only when Win32 can't read a popup.
    texts : {hwnd: latest alert text} for popups we could read
    popups: set of every popup hwnd that exists (readable or not)
    n_top : number of top-level windows the terminal owns
    counts: {hwnd: alert list row count}
    """
    texts_out, popups, counts = {}, set(), {}
    want = cfg["window_title"].strip().casefold()

    top = win32_top_windows(pid)
    for hwnd, t, vis in top:
        if t.strip():
            log_once(("title", pid, t), f"[PID {pid}] window seen: '{t}' (hwnd {hwnd}, visible={vis})", "DEBUG")
        if want and want in t.strip().casefold():
            popups.add(hwnd)

    uia_map = None

    def uia_windows():
        nonlocal uia_map
        if uia_map is None:
            uia_map = {}
            for w in find_popups(get_desktop, pid, cfg["window_title"]):
                try:
                    uia_map[w.handle] = w
                except Exception:
                    pass
        return uia_map

    if not top:                                  # Win32 enumeration gave nothing: ask UIA what exists
        popups |= set(uia_windows())

    for hwnd in popups:
        picked, n_uia, w = win32_read(hwnd, cfg), 0, None
        if not picked:
            w = uia_windows().get(hwnd)
            if w is not None:
                try:
                    picked, n_uia = uia_read(w, cfg)
                except Exception as e:
                    log_once(
                        ("read", repr(e)),
                        f"UIA read error: {e!r}. If MT5 runs as Administrator, run this script as Administrator too.",
                        "WARN",
                    )
        snapshot_popup(hwnd, w)
        if picked:
            texts_out[hwnd] = picked
            counts[hwnd] = win32_list_count(hwnd)
            _unreadable_since.pop(hwnd, None)
        else:
            since = _unreadable_since.setdefault(hwnd, time.time())
            if time.time() - since >= UNREADABLE_WARN_AFTER:       # an MT5 popup reads empty for an instant while it is rebuilt
                log_once(
                    ("empty", hwnd),
                    f"popup hwnd {hwnd} has had no usable text for {UNREADABLE_WARN_AFTER:g}s ({n_uia} UIA text controls, "
                    "none matched the filter). See the POPUP SNAPSHOT above, or rely on the log safety net.",
                    "WARN",
                )
    return ReadResult(texts_out, popups, len(top), counts)


def window_titles(pid):
    try:
        return sorted({t for _h, t, _v in win32_top_windows(pid) if t.strip()})
    except Exception:
        return []


# ------------------------------------------------------------ log reading
def is_entry_start(line):
    """
    MT5 log entries are tab-separated fields (whatever the exact timestamp layout is:
    'HH:MM:SS.mmm', 'YYYY.MM.DD HH:MM:SS.mmm', with or without leading code columns).
    Only the 2nd+ lines of a multi-line message have no tab, so no tab = continuation.
    """
    return "\t" in line or bool(TS_RE.match(line))


def alert_text_from_entry(entry):
    """Text after the 'Alert:' field in a log entry, or None if the entry is not an Alert()."""
    first, _, rest = entry.partition("\n")
    m = ALERT_RE.search(first)               # ...<TAB>source<TAB>Alert: message
    if not m:
        m = re.search(r"\bAlert:\s?", first)   # tabs lost somewhere: any 'Alert:' on the line
        if not m:
            return None
    text = first[m.end():]
    if rest:
        text += "\n" + rest
    text = clean(text)
    return text or None


TIME_COL_RE = re.compile(r"^(?:\d{4}\.\d{2}\.\d{2} )?(\d{2}):(\d{2}):(\d{2})(?:\.(\d{3}))?$")


def log_columns(line):
    """
    (seconds since midnight, source, message) of one Experts-log line, or None.
    MT5 puts a 2-letter code and a number in front of the time, and the CODE IS DIFFERENT ON EVERY LINE (even for
    the lines of one message), so only the time, source and message columns can be compared.
    """
    parts = line.split("\t")
    for i in range(min(3, len(parts) - 2)):
        m = TIME_COL_RE.match(parts[i].strip())
        if m:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4) or 0) / 1000.0
            return secs, parts[i + 1], "\t".join(parts[i + 2:])
    return None


def _ms(seconds):
    return int(round(seconds * 1000))


_shapes = {}                       # (source, first line) -> set of line-tuples the POPUP really showed


def shape_mark(src_name, text):
    """Learn what a message really looks like from the popup (it shows the whole message, all of its lines)."""
    lines = _norm_lines(text)
    if not lines or not lines[0]:
        return
    with _hist_lock:
        v = _shapes.setdefault((src_name, lines[0]), set())
        if lines not in v:
            if len(v) >= 8:
                v.clear()
            v.add(lines)


def shape_expect(src_name, have):
    """
    For the lines `have` of an alert found in the log: None if no popup has shown a message that starts like it,
    else (the set of lines a popup showed next, True if `have` is a complete message the popup showed).
    """
    with _hist_lock:
        variants = _shapes.get((src_name, have[0]))
        if not variants or not any(v[:len(have)] == have for v in variants):
            return None
        return {v[len(have)] for v in variants if len(v) > len(have) and v[:len(have)] == have}, have in variants


def alert_continuation(alert_entry, entry, src_name=None):
    """
    Alert("line1\\nline2") is written to the Experts log as SEPARATE lines from the same source. Only the first
    carries 'Alert:'. Decides whether `entry` is the next line of that message (the code columns in front are
    ignored). Returns the line's text, or None. See CONT_TIME_TOL / SHAPE_WINDOW: a message the popup has shown
    before is followed by TEXT (exact). An unknown one by a tiny time window. A line that only repeats a line the
    alert already has (the usual Alert(msg) then Print(msg) pair) is never joined, and at most MAX_ALERT_LINES lines.
    Set MERGE_ALERT_LINES = False to switch the merging off completely.
    """
    if not MERGE_ALERT_LINES or alert_text_from_entry(entry) is not None:
        return None
    a = log_columns(alert_entry.partition("\n")[0])
    efirst, _, erest = entry.partition("\n")
    c = log_columns(efirst)
    if not a or not c or a[1] != c[1]:
        return None
    gap = abs(_ms(a[0]) - _ms(c[0]))
    msg = c[2] + (("\n" + erest) if erest else "")
    have = (alert_text_from_entry(alert_entry) or "").splitlines()
    if len(have) >= MAX_ALERT_LINES:
        return None
    key = norm_key(msg.split("\n")[0])
    if key in {norm_key(l) for l in have}:
        return None
    shape = shape_expect(src_name, _norm_lines("\n".join(have))) if src_name else None
    if shape is not None:
        nexts, complete = shape
        if nexts:                                    # a popup showed this message with more lines: follow its TEXT
            return msg if (key in nexts and gap <= _ms(SHAPE_WINDOW)) else None
        if complete:                                 # ...and showed it ending here: nothing else belongs to it
            return None
    return msg if gap <= _ms(CONT_TIME_TOL) else None


class AlertCollector:
    """
    Gathers Alert() lines and joins the extra lines of a multi-line alert back on (see alert_continuation).
    Many alerts can be open at the same time, so the lines of different EAs may be interleaved in the log.
    release() hands out finished alerts in the order they were found, CONT_HOLD seconds after they arrived.
    """

    def __init__(self, src_name=None):
        self.src_name = src_name
        self.held = []                 # [entry, arrival time]
        self.diag = 0

    def feed(self, entry, now):
        if alert_text_from_entry(entry):
            self.held.append([entry, now])
            return
        for h in reversed(self.held):
            extra = alert_continuation(h[0], entry, self.src_name)
            if extra is not None:
                h[0] += "\n" + extra
                return
        if self.diag < 3 and MERGE_ALERT_LINES:        # visible hint if a real 2nd line ever fails to join
            c = log_columns(entry.partition("\n")[0])
            if c:
                for h in self.held:
                    a = log_columns(h[0].partition("\n")[0])
                    # only lines a few ms away are worth a hint (a real 2nd line that missed by a millisecond).
                    # the EA's normal Print() lines 0.1-0.3 s later are expected and not reported
                    if (a and a[1] == c[1] and _ms(CONT_TIME_TOL) < abs(_ms(a[0]) - _ms(c[0])) <= _ms(CONT_TIME_TOL) + 10
                            and not (self.src_name and shape_expect(self.src_name, _norm_lines(alert_text_from_entry(h[0]) or "")))):
                        self.diag += 1
                        log(f"2nd-line check: a line from the same EA came {abs(a[0] - c[0]):.3f}s after its Alert and was "
                            f"NOT joined (CONT_TIME_TOL={CONT_TIME_TOL}): {entry!r:.200}")
                        break

    def release(self, now):
        out = []
        while self.held and (now - self.held[0][1] >= CONT_HOLD or len(self.held) > 500):
            out.append(self.held.pop(0)[0])
        return out


def entries_from_text(text):
    """Split a whole log text into entries (continuation lines join the previous entry)."""
    entries, cur = [], None
    for line in text.replace("\r", "").split("\n"):
        if is_entry_start(line):
            if cur is not None:
                entries.append(cur)
            cur = line
        elif cur is not None:
            cur += "\n" + line
    if cur is not None:
        entries.append(cur)
    return entries


def encoding_from_head(head):
    if head[:3] == b"\xef\xbb\xbf":
        return "utf-8"
    if head[:2] == b"\xff\xfe" or (len(head) > 1 and head[1:2] == b"\x00") or not head:
        return "utf-16-le"
    return "utf-8"


def detect_encoding(path):
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return "utf-16-le"
    return encoding_from_head(head)


def true_size(path):
    """
    Real current size, asked through an open handle. os.path.getsize() reads directory
    metadata that Windows updates lazily while another process (MT5) holds the file
    open for writing, so it can lag behind by seconds.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        return f.tell()


def entry_lag(entry, now=None):
    """(clock_text, seconds between the time written in the log line and now), or (None, None)."""
    m = re.search(r"\b(\d{2}):(\d{2}):(\d{2})(?:\.(\d{3}))?", entry.partition("\n")[0])
    if not m:
        return None, None
    now = now or datetime.now()
    try:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3)),
                        microsecond=int(m.group(4) or 0) * 1000)
    except ValueError:
        return None, None
    lag = (now - t).total_seconds()
    if lag < -60:                      # crossed midnight
        lag += 86400
    return m.group(0), lag


class LogTailer:
    """Follows today's Experts log and yields complete entries as they are written."""

    def __init__(self, logs_dir, tag=""):
        self.logs_dir = logs_dir
        self.tag = tag
        self.path = None
        self.offset = 0
        self.enc = "utf-16-le"
        self.dec = None
        self.buf = ""
        self.pending = None
        self.last_data = 0.0
        self.lines_seen = 0           # complete lines read since start
        self.unmatched = 0            # non-blank lines that belonged to no entry
        self.samples = []             # first few of those, for the diagnostics
        self.fixed = None             # helper tailer that follows one fixed (previous day) file
        self.prev = None              # that helper, running for a while after midnight
        self.prev_until = 0.0
        self._new_decoder()

    def _new_decoder(self):
        # incremental: a character (e.g. an emoji = 2 UTF-16 units) split across two reads still decodes
        self.dec = codecs.getincrementaldecoder(self.enc)(errors="replace")

    def today_path(self):
        return os.path.join(self.logs_dir, time.strftime("%Y%m%d") + ".log")

    def start(self):
        """Baseline at end of file so old alerts are never re-sent."""
        self.path = self.today_path()
        self.buf = ""
        if os.path.exists(self.path):
            self.enc = detect_encoding(self.path)
            try:
                size = true_size(self.path)
            except OSError:
                size = 0
            self.offset = size - (size % 2) if self.enc == "utf-16-le" else size
        else:
            self.offset = 0
        self._new_decoder()

    def _flush(self, out):
        if self.pending is not None:
            out.append(self.pending)
            self.pending = None

    def poll(self):
        out = []
        if self.prev is not None:                # yesterday's file: MT5 may still be flushing its last lines into it
            if time.time() > self.prev_until:
                self.prev._flush(out)
                self.prev = None
            else:
                out.extend(self.prev.poll())
        p = self.fixed or self.today_path()
        if p != self.path:                       # midnight rollover -> new file from the top
            log(f"{self.tag} Experts log rolled over to {os.path.basename(p)} "
                f"(still reading {os.path.basename(self.path)} for {ROLLOVER_GRACE:.0f}s: MT5 flushes late)")
            if self.prev is not None:
                self.prev._flush(out)
            old = LogTailer(self.logs_dir, self.tag + " (previous day)")
            old.fixed = old.path = self.path
            old.offset, old.enc, old.dec = self.offset, self.enc, self.dec     # decoder state travels with it
            old.buf, old.pending, old.last_data = self.buf, self.pending, self.last_data
            self.prev, self.prev_until = old, time.time() + ROLLOVER_GRACE
            self.pending = None
            self.path, self.offset, self.buf = p, 0, ""
            self._new_decoder()
        data = b""
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()                  # real size from the handle, not stale metadata
                if size < self.offset:           # truncated / recreated
                    log(f"{self.tag} Experts log shrank ({size} < {self.offset}), re-reading from the start", "WARN")
                    self.offset, self.buf = 0, ""
                    self._new_decoder()
                if size > self.offset:
                    f.seek(self.offset)
                    data = f.read(size - self.offset)
        except OSError:
            self._idle_flush(out)
            return out
        if data:
            start = self.offset
            self.offset = start + len(data)
            if start == 0:
                self.enc = encoding_from_head(data[:4])
                self._new_decoder()
                if self.enc == "utf-16-le" and data[:2] == b"\xff\xfe":
                    data = data[2:]
                elif data[:3] == b"\xef\xbb\xbf":
                    data = data[3:]
            self.buf += self.dec.decode(data)
            lines = self.buf.split("\n")
            self.buf = lines.pop()               # incomplete last line waits for its newline
            for line in lines:
                line = line.rstrip("\r")
                self.lines_seen += 1
                if is_entry_start(line):
                    self._flush(out)
                    self.pending = line
                elif self.pending is not None:
                    self.pending += "\n" + line
                elif line.strip():
                    self.unmatched += 1
                    if len(self.samples) < 3:
                        self.samples.append(line[:200])
            self.last_data = time.time()
        self._idle_flush(out)
        return out

    def _idle_flush(self, out):
        if self.pending is not None and not self.buf and time.time() - self.last_data > IDLE_FLUSH:
            self._flush(out)


def tail_sample(path, n=3):
    """Last n non-empty lines already in the file (decoded), to show the real line format."""
    try:
        enc = detect_encoding(path)
        size = true_size(path)
        start = max(0, size - 8192)
        if enc == "utf-16-le":
            start -= start % 2
        with open(path, "rb") as f:
            f.seek(start)
            raw = f.read()
        if enc == "utf-16-le":
            raw = raw[: len(raw) - (len(raw) % 2)]
        text = raw.decode(enc, errors="replace").lstrip("\ufeff")
        lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
        return enc, lines[-n:]
    except OSError as e:
        return None, [f"(could not read: {e})"]


# ------------------------------------------------------------ delivery history (duplicate check)
_hist_lock = threading.Lock()
_sent_hist = {}                    # (source name, channel name, normalized text) -> [epoch of each send]
HIST_KEEP = 900                    # seconds of history kept


_sent_raw = collections.deque(maxlen=60)   # (epoch, source, channel, raw text) for diagnostics only


def _norm_lines(text):
    lines = tuple(k for k in (norm_key(ln) for ln in text.splitlines()) if k)
    return lines or (norm_key(text),)


def _same_alert(a, b):
    """Same alert if one text is the other's leading lines: the popup shows the whole message, the log may
    hold only its first line (or one extra Print line too many). Different 2nd lines = different alerts."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def hist_mark(src_name, ch_name, text, epoch, claimed=False):
    lines = _norm_lines(text)
    with _hist_lock:
        _sent_raw.append((epoch, src_name, ch_name, text))
        cutoff = time.time() - HIST_KEEP
        lst = _sent_hist.setdefault((src_name, ch_name, lines[0]), [])
        lst.append([epoch, lines, claimed])             # [when sent, its lines, already claimed by a log alert]
        lst[:] = [x for x in lst if x[0] >= cutoff]
        if len(_sent_hist) > 300:
            for k in [k for k, v in _sent_hist.items() if not v or v[-1][0] < cutoff]:
                del _sent_hist[k]


def hist_unmark(src_name, ch_name, text, epoch):
    lines = _norm_lines(text)
    with _hist_lock:
        lst = _sent_hist.get((src_name, ch_name, lines[0]), [])
        for x in list(lst):
            if x[0] == epoch and x[1] == lines:
                lst.remove(x)
                break


def short_near(items, keep=3):
    """The few sends closest in time, plus how many more there were (keeps the log line short during a burst)."""
    items = sorted(items, key=lambda x: abs(x[0]))
    more = len(items) - keep
    return repr(items[:keep]) + (f" (+{more} more)" if more > 0 else "")


def hist_recent_texts(src_name, ch_name, around_epoch, span=60):
    """What this source really sent to this channel near a moment (shown when a log alert is NOT matched)."""
    with _hist_lock:
        return [(round(e - around_epoch, 1), t) for e, sn, cn, t in _sent_raw
                if sn == src_name and cn == ch_name and abs(e - around_epoch) <= span]


def _claim(lst, ok_time, lines, ref_epoch):
    """Use up the closest unclaimed send that matches. ONE send covers ONE log alert: if the same EA raises
    the same alert on 3 charts within a moment and the popup could only read one of them, the other two
    must still be recovered from the log."""
    best = None
    for x in lst:
        if not x[2] and ok_time(x[0]) and _same_alert(x[1], lines):
            if best is None or abs(x[0] - ref_epoch) < abs(best[0] - ref_epoch):
                best = x
    if best is None:
        return False
    best[2] = True
    return True


def hist_claim_near(src_name, ch_name, text, alert_epoch, before=DUP_BEFORE, after=DUP_AFTER):
    """Was this log alert already delivered (one unclaimed send around the moment it was logged)? Claims it."""
    lines = _norm_lines(text)
    with _hist_lock:
        return _claim(_sent_hist.get((src_name, ch_name, lines[0]), []),
                      lambda e: alert_epoch - before <= e <= alert_epoch + after, lines, alert_epoch)


def hist_claim_since(src_name, ch_name, text, since_epoch):
    lines = _norm_lines(text)
    with _hist_lock:
        return _claim(_sent_hist.get((src_name, ch_name, lines[0]), []),
                      lambda e: e >= since_epoch, lines, since_epoch)


_cover = {}                        # (source, channel, exact text) -> alert times (epoch) already delivered


def cover_mark(src_name, ch_name, text, epoch):
    key = (src_name, ch_name, norm_key(text))
    with _hist_lock:
        lst = _cover.setdefault(key, [])
        lst.append(epoch)
        cutoff = time.time() - 300
        lst[:] = [e for e in lst if e >= cutoff]
        if len(_cover) > 300:
            for k in [k for k, v in _cover.items() if not v or v[-1] < cutoff]:
                del _cover[k]


def cover_claim(src_name, ch_name, text, epoch, span):
    """
    Atomic 'check and mark': False if an alert with exactly this text was already delivered within +-span seconds
    of `epoch` (then nothing is marked), otherwise marks this one as delivered and returns True. Being one step
    under one lock, two threads handling the same alert at the same instant cannot both get True.
    """
    key = (src_name, ch_name, norm_key(text))
    with _hist_lock:
        lst = _cover.setdefault(key, [])
        if span > 0 and any(abs(e - epoch) <= span for e in lst):
            return False
        lst.append(epoch)
        cutoff = time.time() - 300
        lst[:] = [e for e in lst if e >= cutoff]
        if len(_cover) > 300:
            for k in [k for k, v in _cover.items() if not v or v[-1] < cutoff]:
                del _cover[k]
        return True


def cover_unmark(src_name, ch_name, text, epoch):
    key = (src_name, ch_name, norm_key(text))
    with _hist_lock:
        lst = _cover.get(key, [])
        if epoch in lst:
            lst.remove(epoch)


def cover_near(src_name, ch_name, text, epoch, span):
    """Was an alert with exactly this text already delivered to this channel within +-span seconds of `epoch`?"""
    key = (src_name, ch_name, norm_key(text))
    with _hist_lock:
        return any(abs(e - epoch) <= span for e in _cover.get(key, []))


# ------------------------------------------------------------ telegram
_tg = {"s": None}


def tg_session():
    """One shared session = connection (DNS + TLS) stays open and warm between alerts."""
    if _tg["s"] is None:
        _tg["s"] = requests.Session()
    return _tg["s"]


def _ping_loop(token, stop):
    url = f"https://api.telegram.org/bot{token}/getMe"
    first = True
    while not stop.is_set():
        t0 = time.perf_counter()
        try:
            r = tg_session().get(url, timeout=10)
            dt = time.perf_counter() - t0
            if not r.ok:
                log(f"telegram ping ({mask(token)}): HTTP {r.status_code} {r.text[:200]}", "WARN")
            elif first:
                log(f"telegram connection warmed up for bot {mask(token)}, round trip {dt:.2f}s")
            elif dt > 1.5:
                log(f"telegram ping slow: {dt:.2f}s (this is the network/Telegram, not the script)", "WARN")
        except requests.RequestException as e:
            log(f"telegram ping failed: {e!r}", "WARN")
        first = False
        stop.wait(20)


def send_telegram(token, chat_id, text, label="", t_detect=None):
    if len(text) > TG_MAX_TEXT:
        log(f"{label} alert is {len(text)} chars, cutting to Telegram's {TG_MAX_TEXT} limit", "WARN")
        text = text[: TG_MAX_TEXT - 12] + "\n[cut]"
    first = text.splitlines()[0] if text else ""
    log(f"{label} SENDING to chat {chat_id} ({len(text)} chars):\n{text}", console=False)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    attempt, rate_waits = 0, 0
    while attempt < 3:
        attempt += 1
        t0 = time.perf_counter()
        try:
            r = tg_session().post(url, data={"chat_id": chat_id, "text": text}, timeout=(5, 10))
            http = time.perf_counter() - t0
            if r.ok:
                try:
                    mid = r.json().get("result", {}).get("message_id")
                except Exception:
                    mid = "?"
                STATS["sent_ok"] += 1
                since = f", {time.perf_counter() - t_detect:.2f}s after detection" if t_detect else ""
                log(f"{label} SEND OK (attempt {attempt}, HTTP {r.status_code}, message_id {mid}, "
                    f"telegram took {http:.2f}s{since}): {first}")
                return True
            log(f"{label} SEND FAILED attempt {attempt}/3: HTTP {r.status_code} {r.text[:300]}", "WARN")
            if r.status_code == 429 and rate_waits < 3:
                # Telegram rate limit (1 msg/s per chat, 20 msg/min per group): wait as told, does not use up an attempt
                rate_waits += 1
                attempt -= 1
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    wait = 1
                asked = max(wait, 1)
                wait = min(asked + 1, TG_RETRY_AFTER_CAP)        # the full time Telegram asked for, plus a second
                log(f"{label} rate limited by Telegram (it asked for {asked}s), waiting {wait}s then retrying ({rate_waits}/3)", "WARN")
                time.sleep(wait)
                continue
            if r.status_code in (400, 401, 403, 404):
                break                              # retrying will not help (bad token / chat / text)
        except requests.RequestException as e:
            log(f"{label} SEND FAILED attempt {attempt}/3 after {time.perf_counter() - t0:.2f}s: network error {e!r}", "WARN")
        time.sleep(0.2 if attempt == 1 else 1)
    STATS["sent_failed"] += 1
    log(f"{label} SEND GAVE UP, alert NOT delivered: {first}", "ERROR")
    return False


_chan_workers = {}
_chan_lock = threading.Lock()


def _chan_worker(q):
    """One worker per channel: alerts go out in order, >= TG_MIN_GAP apart (Telegram per-chat limit)."""
    last = 0.0
    recent = collections.deque()       # when this channel's latest messages went out (Telegram: ~20 per minute per chat)
    while True:
        cfg, ch, text, src_name, origin, t_detect, epoch, ae = q.get()
        label = f"[{src_name}->{ch['name']}]"
        try:
            wait = TG_MIN_GAP - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            if TG_BURST_LIMIT > 0:
                announced = False
                while True:
                    t_now = time.time()
                    while recent and t_now - recent[0] > TG_BURST_WINDOW:
                        recent.popleft()
                    if len(recent) < TG_BURST_LIMIT:
                        break
                    pause = recent[0] + TG_BURST_WINDOW + 0.5 - t_now
                    if not announced:
                        announced = True
                        log(f"{label} Telegram allows about 20 messages a minute into one chat and {TG_BURST_LIMIT} already went "
                            f"out: waiting {max(pause, 0):.0f}s so the next alert is not rejected ({q.qsize() + 1} waiting)", "WARN")
                    time.sleep(min(max(pause, 0.05), 1.0))
            if q.qsize() > 0:
                log(f"{label} {q.qsize()} more alert(s) waiting in this channel's send queue")
            ok = send_telegram(token_for(cfg, ch), ch["chat_id"], text, label, t_detect)
            recent.append(time.time())
            if not ok:
                hist_unmark(src_name, ch["name"], text, epoch)     # lets the log safety net retry it
                cover_unmark(src_name, ch["name"], text, ae)
        except Exception:
            STATS["sent_failed"] += 1
            hist_unmark(src_name, ch["name"], text, epoch)
            cover_unmark(src_name, ch["name"], text, ae)
            log_exception(f"send worker {label}")
        finally:
            last = time.time()
            q.task_done()


def dispatch_channel(cfg, ch, text, src_name, origin="popup", alert_epoch=None):
    """Queue one alert for one channel. Returns False if it was held back as an identical repeat (IDENTICAL_COOLDOWN)."""
    t_detect, epoch = time.perf_counter(), time.time()
    ae = epoch if alert_epoch is None else alert_epoch          # when the alert HAPPENED (a log alert can be old)
    if not cover_claim(src_name, ch["name"], text, ae, IDENTICAL_COOLDOWN):
        STATS["suppressed"] += 1
        log(f"[{src_name}->{ch['name']}] identical alert suppressed (same text already delivered within "
            f"{IDENTICAL_COOLDOWN:g}s): {text.splitlines()[0]}")
        return False
    # a send made FOR a log alert is used up at once, otherwise the next identical log alert would "claim" it
    hist_mark(src_name, ch["name"], text, epoch, claimed=(origin == "log"))
    with _chan_lock:
        w = _chan_workers.get(ch["name"])
        if w is None or not w["t"].is_alive():
            q = queue.Queue()
            t = threading.Thread(target=_chan_worker, args=(q,), daemon=True)
            w = {"q": q, "t": t}
            _chan_workers[ch["name"]] = w
            t.start()
    w["q"].put((cfg, ch, text, src_name, origin, t_detect, epoch, ae))
    return True


def dispatch(cfg, src_name, text, origin="popup", alert_epoch=None):
    """Send an alert from a source to every channel it is routed to."""
    chans = channels_for(cfg, src_name)
    if not chans:
        log_once(("noroute", src_name), f"[{src_name}] alert detected but this source is not routed to any channel", "WARN")
        return
    for ch in chans:
        dispatch_channel(cfg, ch, text, src_name, origin, alert_epoch)


def test_telegram(cfg):
    if not cfg["channels"]:
        print("Add a channel first.")
        return
    for ch in cfg["channels"]:
        token, label = token_for(cfg, ch), f"[test->{ch['name']}]"
        if not token or not ch["chat_id"]:
            log(f"{label} missing bot token or chat ID", "ERROR")
            continue
        try:
            r = tg_session().get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            if not r.ok:
                log(f"{label} token rejected by Telegram (HTTP {r.status_code}): {r.text[:300]}", "ERROR")
                continue
            log(f"{label} token OK, bot @{r.json()['result'].get('username')}")
        except requests.RequestException as e:
            log(f"{label} network error {e!r}", "ERROR")
            continue
        if send_telegram(token, ch["chat_id"], f"Forwarder test message for channel '{ch['name']}'", label):
            log(f"{label} message delivered, check the chat.")


# ------------------------------------------------------------ watchers (one thread per source and capture type)
def filter_text(cfg):
    return "GLOBAL (all alerts)" if cfg["global_mode"] else f"keyword '{cfg['keyword'] or 'none'}'"


def _com_init():
    """
    pywinauto/UIA needs COM initialised in every thread that uses it. Microsoft recommends the multithreaded
    apartment (MTA) for UI Automation clients and pywinauto sets the main thread up that way, so the watcher
    threads use MTA too (comtypes.CoInitialize() would make them single-threaded apartments).
    Returns True only if THIS call initialised COM, so that _com_uninit() stays balanced.
    """
    try:
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        return True
    except Exception:                            # e.g. the thread was already set up in another mode: keep that
        return False


def _com_uninit():
    try:
        import comtypes
        comtypes.CoUninitialize()
    except Exception:
        pass


def popup_worker(cfg, src, stop):
    tag = f"[{src['name']}/popup]"
    com = _com_init()
    holder = {"d": None}

    def get_desktop():
        if holder["d"] is None:
            holder["d"] = Desktop(backend="uia")
        return holder["d"]

    name = src["name"]
    pid, known, known_counts, prev_popups, misses = None, {}, {}, set(), {}
    credits = {}                       # hwnd -> (rows still expected, expiry): see "late row" below
    lost = {}                          # hwnd -> (text, rows, when, closed?) of a popup we lost sight of. Pruned after 10 min
    attached_before, ever_waited = False, False
    waiting_logged = False
    next_beat = time.time() + HEARTBEAT_SECONDS
    log(f"{tag} started | title contains '{cfg['window_title']}' | {filter_text(cfg)} | every {cfg['poll_seconds']}s | "
        f"send on old-window update: {'YES' if cfg['send_on_update'] else 'NO'}")
    try:
        while not stop.is_set():
            try:
                if pid is None or not psutil.pid_exists(pid):
                    if pid is not None:
                        log(f"{tag} terminal PID {pid} is gone, waiting for it to start again", "WARN")
                        pid = None
                    pid = resolve_source_pid(src)
                    if pid is None:
                        ever_waited = True
                        if not waiting_logged:
                            log(f"{tag} MT5 is not running ({src['exe']}), waiting for it to start...", "WARN")
                            waiting_logged = True
                        stop.wait(2)
                        continue
                    waiting_logged = False
                    new_session = attached_before or ever_waited      # MT5 (re)started while we were watching
                    attached_before = True
                    if new_session:
                        # a freshly started MT5 cannot have an old popup: whatever is open now is a NEW alert
                        known, known_counts, prev_popups, misses, credits = {}, {}, set(), {}, {}
                        lost.clear()
                        log(f"{tag} MT5 is running again (PID {pid}), tracking resumed automatically. Popups already open "
                            "belong to the new session and WILL be sent")
                    else:
                        first = read_windows(get_desktop, pid, cfg)
                        known, known_counts = dict(first.texts), dict(first.counts)
                        prev_popups, misses, credits = set(first.popups), {}, {}
                        for t in known.values():
                            shape_mark(name, t)          # what this terminal's alerts look like (all their lines)
                        log(f"{tag} attached to PID {pid}: terminal owns {first.n_top} top-level window(s), "
                            f"{len(first.popups)} popup(s) open now, {len(known)} readable (treated as OLD)")
                        for h, t in known.items():
                            log(f"{tag}    OLD popup hwnd {h}: {t.splitlines()[0]} (alert list rows: {known_counts.get(h)})")
                            if cfg["send_existing_on_start"]:
                                log(f"{tag}    sending it anyway (setting 'send already-open popup on start' is ON)")
                                dispatch(cfg, name, t, "popup")

                rr = read_windows(get_desktop, pid, cfg)
                current = rr.texts

                for h in rr.popups - prev_popups:
                    log(f"{tag} popup window APPEARED (hwnd {h})")
                for h in prev_popups - rr.popups:
                    log(f"{tag} popup window CLOSED (hwnd {h})")
                    if h in known:
                        lost[h] = (known[h], known_counts.get(h), time.time(), True)
                    known.pop(h, None)           # a window handle can be reused later: treat it as brand new
                    known_counts.pop(h, None)
                    credits.pop(h, None)
                    misses.pop(h, None)
                prev_popups = set(rr.popups)

                for h, t in current.items():
                    c = rr.counts.get(h)
                    prev_c = known_counts.get(h)
                    grew = c is not None and prev_c is not None and c > prev_c
                    grew_by = (c - prev_c) if grew else 0
                    if h not in known:
                        # A handle we have no record of. It is a NEW alert, unless it is a popup we lost sight of
                        # a moment ago (see `lost`) that is back with exactly the same text: then it is the alert
                        # we already sent. Controlled by RESEND_GUARD / LOST_GRACE (see TUNING SWITCHES).
                        back = lost.pop(h, None)
                        same_again = (RESEND_GUARD and back is not None and back[0] == t
                                      and (not back[3] or time.time() - back[2] <= LOST_GRACE)
                                      and not (c is not None and back[1] is not None and c > back[1]))
                        if same_again:
                            log(f"{tag} popup hwnd {h} readable again with the same text as before, "
                                f"NOT re-sending: {t.splitlines()[0]}")
                        else:
                            log(f"{tag} NEW WINDOW spawned (hwnd {h}): {t.splitlines()[0]}")
                            dispatch(cfg, name, t, "popup")
                    else:
                        text_changed = t != known[h]
                        credit = credits.get(h)
                        if credit and time.time() > credit[1]:
                            credits.pop(h, None)
                            credit = None
                        late_row = False
                        if grew and not text_changed and credit:
                            # the list row of an alert we already sent when its TEXT appeared (a busy UI can
                            # add the row a moment later): the same alert, not a new one
                            used = min(credit[0], grew_by)
                            grew_by -= used
                            if credit[0] - used > 0:
                                credits[h] = (credit[0] - used, credit[1])
                            else:
                                credits.pop(h, None)
                            late_row = grew_by <= 0
                        if late_row:
                            log(f"{tag} alert list row {prev_c} -> {c} belongs to an alert already sent, NOT re-sending (hwnd {h})")
                        elif text_changed or grew_by > 0:
                            why = "text changed" if text_changed else f"alert list grew {prev_c} -> {c} rows, same text"
                            if grew_by > 1:
                                log(f"{tag} {grew_by} alerts arrived within one poll. The popup only shows the newest, "
                                    f"so {grew_by - 1} older alert text(s) could NOT be read here "
                                    "(the log safety net recovers them if enabled).", "WARN")
                            if text_changed and not grew:
                                credits[h] = (1, time.time() + CREDIT_SECONDS)   # its list row may still follow
                            elif text_changed:
                                credits.pop(h, None)
                            if cfg["send_on_update"]:
                                log(f"{tag} OLD WINDOW UPDATED (hwnd {h}, {why}): {t.splitlines()[0]}")
                                dispatch(cfg, name, t, "popup")
                            else:
                                log(f"{tag} old window updated (hwnd {h}, {why}), skipped by setting: {t.splitlines()[0]}")
                    if t != known.get(h):
                        shape_mark(name, t)              # teaches the log reader how many lines this message has
                    known[h] = t
                    if c is not None:
                        known_counts[h] = c
                    misses.pop(h, None)

                for h in list(known):
                    if h not in current:
                        misses[h] = misses.get(h, 0) + 1
                        if misses[h] > MISS_LIMIT:
                            lost[h] = (known[h], known_counts.get(h), time.time(), False)
                            del known[h]
                            known_counts.pop(h, None)
                            credits.pop(h, None)
                            misses.pop(h, None)

                if time.time() >= next_beat:
                    next_beat = time.time() + HEARTBEAT_SECONDS
                    for h in [h for h, v in lost.items() if time.time() - v[2] > 600]:
                        del lost[h]
                    log(f"{tag} heartbeat: PID {pid} windows={rr.n_top} popups_open={len(rr.popups)} "
                        f"readable={len(current)} list_rows={dict(rr.counts)}")
            except Exception:
                log_exception(f"{tag} popup polling")
                stop.wait(1)
            stop.wait(cfg["poll_seconds"])
    finally:
        if com:
            _com_uninit()
        log(f"{tag} stopped")


def handle_log_alert(cfg, src, text, entry, tag):
    """Decide what to do with an Alert() line found in the Experts log."""
    name = src["name"]
    clock, lag = entry_lag(entry)
    when = f" | MT5 wrote it at {clock}, script saw it {lag:.1f}s later" if lag is not None else ""
    first = text.splitlines()[0]

    alert_epoch = time.time() - lag if lag is not None else None       # when MT5 raised it
    if not cfg["popup_enabled"]:                       # log is the only capture: send everything
        log(f"{tag} LOG ALERT detected: {first}{when}", "WARN" if lag is not None and lag > 2 else "INFO")
        dispatch(cfg, name, text, "log", alert_epoch)
        return

    # popup + log: the log is the safety net
    limit = cfg["recover_seconds"]
    if lag is not None and lag > limit:
        log(f"{tag} log alert is {lag:.0f}s old (limit {limit}s), NOT sent: {first}{when}", "WARN")
        return
    chans = channels_for(cfg, name)
    if not chans:
        log_once(("noroute", name), f"[{name}] alert detected but this source is not routed to any channel", "WARN")
        return
    now = time.time()
    ae = alert_epoch if alert_epoch is not None else now
    missing = []
    for ch in chans:
        if lag is not None:
            done = hist_claim_near(name, ch["name"], text, now - lag)
        else:
            done = hist_claim_since(name, ch["name"], text, now - limit)
        if done:
            cover_mark(name, ch["name"], text, ae)      # the rest of an identical burst is covered by it too
        else:
            missing.append(ch)
    if not missing:
        log(f"{tag} log alert already delivered (by the popup) to all channels, nothing to do: {first}{when}")
        return
    near = {c["name"]: hist_recent_texts(name, c["name"], now - (lag or 0)) for c in missing}
    sent_to = [c for c in missing if dispatch_channel(cfg, c, text, name, "log", alert_epoch)]
    if sent_to:
        log(f"{tag} RECOVERED from log (popup missed it), sending to {', '.join(c['name'] for c in sent_to)}: {first}{when}", "WARN")
        for c in sent_to:
            log(f"{tag}    no match for log text {norm_key(text)!r}. Sent near that time to '{c['name']}' "
                f"(seconds vs alert, raw text): {short_near(near[c['name']])}")


def log_worker(cfg, src, stop):
    tag = f"[{src['name']}/log]"
    tail = None
    while not stop.is_set() and tail is None:
        logs_dir, portable, note = resolve_log_dir_for(src)
        if not logs_dir or not os.path.isdir(logs_dir):
            log_once((tag, "nodir", str(logs_dir)), f"{tag} Experts log folder not available yet "
                     f"({logs_dir or note}), retrying every 10s", "WARN")
            stop.wait(10)
            continue
        tail = LogTailer(logs_dir, tag)
        tail.start()
        log(f"{tag} started | {note} | {logs_dir}")
        log(f"{tag} today's file {os.path.basename(tail.today_path())} "
            f"({'exists, ' + str(tail.offset) + ' bytes already there are ignored' if os.path.exists(tail.path) else 'does not exist yet'})")
        mode = "SAFETY NET (recovers alerts the popup missed, max age "
        mode += f"{cfg['recover_seconds']}s)" if cfg["popup_enabled"] else "ONLY capture (sends every alert)"
        log(f"{tag} filter: {filter_text(cfg)} | role: {mode}")
        if os.path.exists(tail.path):
            enc, sample = tail_sample(tail.path)
            log(f"{tag} log encoding detected: {enc}. Last lines already in the file (line format sample):")
            for ln in sample:
                log(f"{tag}    {ln!r:.220}")
    if tail is None:
        return

    entries_total = alerts_total = skipped = 0
    collector = AlertCollector(src["name"])     # Alert lines waiting for their possible 2nd+ lines
    next_beat = time.time() + HEARTBEAT_SECONDS
    next_dir_check = time.time() + LOG_DIR_RECHECK

    def process_alert(entry):
        nonlocal alerts_total, skipped
        text = alert_text_from_entry(entry)
        if not text:
            return
        alerts_total += 1
        if not cfg["global_mode"] and cfg["keyword"] and cfg["keyword"].lower() not in text.lower():
            skipped += 1
            log(f"{tag} LOG ALERT skipped (keyword '{cfg['keyword']}' not in it): {text.splitlines()[0]}")
            return
        handle_log_alert(cfg, src, text, entry, tag)

    try:
        while not stop.is_set():
            try:
                if time.time() >= next_dir_check:
                    next_dir_check = time.time() + LOG_DIR_RECHECK
                    if resolve_source_pid(src):          # only while MT5 runs, so its command line is readable
                        nd, _p, _n = resolve_log_dir_for(src)
                        if nd and nd != tail.logs_dir and os.path.isdir(nd):
                            log(f"{tag} MT5 now writes its Experts log to {nd} (was {tail.logs_dir}), switching", "WARN")
                            tail = LogTailer(nd, tag)
                            tail.start()
                for entry in tail.poll():
                    entries_total += 1
                    collector.feed(entry, time.time())
                for done_entry in collector.release(time.time()):
                    try:                                   # one bad alert must not lose the ones behind it
                        process_alert(done_entry)
                    except Exception:
                        log_exception(f"{tag} handling a log alert")
            except Exception:
                log_exception(f"{tag} log polling")
                stop.wait(1)
            if tail.unmatched:
                log_once((tag, "unmatched"), f"{tag} {tail.unmatched} log line(s) were not part of any entry "
                         f"(format surprise?). Samples: {tail.samples!r}", "WARN")
            if time.time() >= next_beat:
                next_beat = time.time() + HEARTBEAT_SECONDS
                p = tail.today_path()
                try:
                    size = true_size(p)
                except OSError:
                    size = None
                log(f"{tag} heartbeat: file {os.path.basename(p)} size={size} read_offset={tail.offset} "
                    f"lines={tail.lines_seen} | entries={entries_total} alerts={alerts_total} skipped={skipped}"
                    + ("" if size is not None else " | TODAY'S LOG FILE DOES NOT EXIST YET"))
            stop.wait(max(cfg["poll_seconds"], 0.05))
    finally:
        log(f"{tag} stopped")


def check_workers(workers, stop, now=None):
    """Restart any worker thread that stopped although we are still watching."""
    now = now or time.time()
    for w in workers:
        if stop.is_set() or w["thread"].is_alive() or now - w.get("last_restart", 0) < 5:
            continue
        log(f"worker '{w['name']}' stopped unexpectedly, restarting it", "ERROR")
        w["thread"] = threading.Thread(target=w["target"], args=w["args"], daemon=True, name=w["name"])
        w["thread"].start()
        w["last_restart"] = now


def start_watching(cfg):
    """Validate, start every watcher thread, block until Ctrl+C."""
    problems = []
    if not cfg["popup_enabled"] and not cfg["log_enabled"]:
        problems.append("both popup and log capture are OFF (menu: Capture mode)")
    routed = []
    for src in cfg["sources"]:
        chans = channels_for(cfg, src["name"])
        if chans:
            routed.append(src)
        else:
            log(f"source '{src['name']}' has no routes, it will NOT be watched (menu: Routes)", "WARN")
    if not routed:
        problems.append("no source is routed to a channel (menu: Sources, Channels, Routes)")
    tokens = set()
    for src in routed:
        for ch in channels_for(cfg, src["name"]):
            t = token_for(cfg, ch)
            if not t or not ch["chat_id"]:
                problems.append(f"channel '{ch['name']}' has no bot token or chat ID")
            tokens.add(t)
    if problems:
        for p in problems:
            log(f"cannot start: {p}", "ERROR")
        return

    STATS.clear()
    stop = threading.Event()
    workers = []

    def add_worker(name, target, args):
        workers.append({"name": name, "target": target, "args": args,
                        "thread": threading.Thread(target=target, args=args, daemon=True, name=name)})

    for tok in sorted(t for t in tokens if t):
        add_worker(f"ping {mask(tok)}", _ping_loop, (tok, stop))
    log("WATCH START: " + " | ".join(
        f"{s['name']} -> {', '.join(c['name'] for c in channels_for(cfg, s['name']))}" for s in routed))
    log(f"capture: popup {'ON' if cfg['popup_enabled'] else 'OFF'}, log {'ON' if cfg['log_enabled'] else 'OFF'}"
        + (f" (safety net, recover window {cfg['recover_seconds']}s)" if cfg["popup_enabled"] and cfg["log_enabled"] else ""))
    for src in routed:
        if cfg["popup_enabled"]:
            add_worker(f"{src['name']}-popup", popup_worker, (cfg, src, stop))
        if cfg["log_enabled"]:
            add_worker(f"{src['name']}-log", log_worker, (cfg, src, stop))
    for w in workers:
        w["thread"].start()
    print("Watching... Ctrl+C to go back to the menu.\n")
    next_beat = time.time() + HEARTBEAT_SECONDS
    try:
        while True:
            time.sleep(0.5)
            check_workers(workers, stop)
            if time.time() >= next_beat:
                next_beat = time.time() + HEARTBEAT_SECONDS
                log(f"heartbeat: sent_ok={STATS['sent_ok']} sent_failed={STATS['sent_failed']} suppressed={STATS['suppressed']}")
    except KeyboardInterrupt:
        log("WATCH STOP (Ctrl+C)")
    finally:
        stop.set()
        for w in workers:
            w["thread"].join(timeout=3)


# ------------------------------------------------------------ portable mode
def copy_data(src, dst):
    for name in ("MQL5", "config", "profiles", "templates"):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s):
            log(f"portable: copying {name} ...")
            ign = shutil.ignore_patterns("Logs") if name == "MQL5" else None
            shutil.copytree(s, d, dirs_exist_ok=True, ignore=ign)


def enable_portable(src):
    exe = src["exe"]
    if not os.path.exists(exe):
        print(f"terminal64.exe not found at {exe}")
        return
    install = os.path.dirname(exe)
    pid = resolve_source_pid(src)
    data_dir, portable = find_data_dir(exe, get_cmdline(pid) if pid else [])
    if portable:
        print(f"Already portable: data + logs live in {install}")
        return

    print(
        "\nPortable mode is a launch flag (terminal64.exe /portable), so MT5 has to be\n"
        "closed and started again. In portable mode all data and logs live directly in\n"
        f"the MT5 folder:  {install}\n"
        "A first portable start begins with an EMPTY data folder, so I can copy your\n"
        "current MQL5 folder (indicators/EAs), profiles, config and templates into it.\n"
        "Price history is not copied. MT5 re-downloads it from your broker.\n"
        "If MT5 is in C:\\Program Files you may need to run this script as Administrator."
    )
    if input("Continue? (y/n): ").strip().lower() != "y":
        return
    do_copy = False
    if data_dir and os.path.normcase(data_dir) != os.path.normcase(install):
        do_copy = input(f"Copy current data from\n  {data_dir}\ninto the MT5 folder? (y/n): ").strip().lower() == "y"
    log(f"[{src['name']}] portable switch started: exe={exe} copy_data={do_copy} from={data_dir}")

    if pid:
        print("\nClose MT5 now (File > Exit). Waiting up to 3 minutes... (Ctrl+C to cancel)")
        try:
            for _ in range(180):
                if not psutil.pid_exists(pid):
                    break
                time.sleep(1)
            else:
                log("portable switch cancelled: MT5 still running", "WARN")
                return
        except KeyboardInterrupt:
            log("portable switch cancelled by user", "WARN")
            return
    time.sleep(1.5)

    if do_copy:
        try:
            copy_data(data_dir, install)
        except (OSError, shutil.Error) as e:
            log(f"portable copy FAILED: {e}. MT5 was NOT relaunched. Start it normally, "
                "or run this script as Administrator.", "ERROR")
            return

    try:
        subprocess.Popen([exe, "/portable"], cwd=install)
    except OSError as e:
        log(f"could not launch MT5: {e}", "ERROR")
        return
    log("MT5 launched with /portable, waiting for it to appear...")
    for _ in range(40):
        npid = resolve_source_pid(src)
        if npid and is_portable_cmdline(get_cmdline(npid)):
            log(f"portable terminal running (PID {npid}). Logs: {os.path.join(install, 'MQL5', 'Logs')}")
            print("Log in to your account in MT5 if it asks.")
            return
        time.sleep(1)
    log("launched, but the process was not seen yet.", "WARN")


# ------------------------------------------------------------ menu helpers
def ask_indices(prompt, n, allow_all=True):
    """'1,3' or 'a' -> list of 0-based indices, or None when empty/invalid."""
    v = input(prompt).strip().lower()
    if not v:
        return None
    if allow_all and v in ("a", "all"):
        return list(range(n))
    out = []
    for part in v.replace(" ", "").split(","):
        if part.isdigit() and 1 <= int(part) <= n:
            if int(part) - 1 not in out:
                out.append(int(part) - 1)
        elif part:
            print(f"'{part}' is not a valid number.")
            return None
    return out or None


def ask_one(prompt, n):
    v = input(prompt).strip()
    return int(v) - 1 if v.isdigit() and 1 <= int(v) <= n else None


def running_map():
    return {os.path.normcase(e): pid for pid, e in list_terminals()}


def list_sources(cfg):
    run = running_map()
    if not cfg["sources"]:
        print("  (no sources yet)")
    for i, s in enumerate(cfg["sources"], 1):
        pid = run.get(os.path.normcase(s["exe"]))
        state = "not running"
        if pid:
            state = f"running, PID {pid}" + (", portable" if is_portable_cmdline(get_cmdline(pid)) else "")
        extra = f"  [manual log folder: {s['log_dir']}]" if s.get("log_dir") else ""
        print(f"  {i}. {s['name']}   {s['exe']}   ({state}){extra}")


def list_channels(cfg):
    if not cfg["channels"]:
        print("  (no channels yet)")
    for i, c in enumerate(cfg["channels"], 1):
        own = f"own bot {mask(c['bot_token'])}" if c.get("bot_token") else "default bot"
        print(f"  {i}. {c['name']}   chat {c['chat_id']}   ({own})")


def list_routes(cfg):
    if not cfg["sources"]:
        print("  (no sources)")
    for s in cfg["sources"]:
        names = [c["name"] for c in channels_for(cfg, s["name"])]
        print(f"  {s['name']}  ->  {', '.join(names) if names else '(nothing, will NOT be watched)'}")
    unused = [c["name"] for c in cfg["channels"] if not any(r["channel"] == c["name"] for r in cfg["routes"])]
    if unused:
        print(f"  channels without a source: {', '.join(unused)}")


def offer_routes(cfg, source=None, channel=None):
    """After adding a source or channel, offer to wire it up."""
    if source is not None and cfg["channels"]:
        print("\nWhich channels should this source send to?")
        list_channels(cfg)
        idx = ask_indices("Channel numbers (e.g. 1,2 or a = all, blank = none): ", len(cfg["channels"]))
        for i in idx or []:
            link(cfg, source["name"], cfg["channels"][i]["name"])
    if channel is not None and cfg["sources"]:
        print("\nWhich sources should feed this channel?")
        list_sources(cfg)
        idx = ask_indices("Source numbers (e.g. 1,2 or a = all, blank = none): ", len(cfg["sources"]))
        for i in idx or []:
            link(cfg, cfg["sources"][i]["name"], channel["name"])
    save_settings(cfg)


def link(cfg, src_name, ch_name):
    if not any(r["source"] == src_name and r["channel"] == ch_name for r in cfg["routes"]):
        cfg["routes"].append({"source": src_name, "channel": ch_name})
        log(f"route added: {src_name} -> {ch_name}")


# ------------------------------------------------------------ source tools
def source_tools(cfg, src):
    while True:
        logs_dir, portable, note = resolve_log_dir_for(src)
        print(f"\n--- Source '{src['name']}' tools ---")
        print(f" Experts log folder: {logs_dir or '-'}  [{note}]")
        print(
            " 1. Show last 5 alerts found in today's Experts log\n"
            " 2. Write the last 15 raw lines of today's Experts log into the script log (format check)\n"
            " 3. Set manual Experts log folder (blank = auto-detect)\n"
            " 4. Enable portable mode (relaunch MT5 with /portable)\n"
            " 5. List this terminal's window titles\n"
            " b. Back"
        )
        c = input("Choice: ").strip().lower()
        if c == "1":
            show_recent_alerts(logs_dir, note)
        elif c == "2":
            show_raw_lines(logs_dir, note)
        elif c == "3":
            v = input("Full path to the MQL5\\Logs folder (blank = auto-detect): ").strip().strip('"')
            src["log_dir"] = v
            save_settings(cfg)
            log(f"[{src['name']}] manual Experts log folder set to '{v or 'auto'}'")
        elif c == "4":
            enable_portable(src)
        elif c == "5":
            pid = resolve_source_pid(src)
            if not pid:
                print("MT5 is not running.")
            else:
                for t in window_titles(pid):
                    print(f"   '{t}'")
        elif c == "b":
            return
        else:
            print("Invalid choice.")


def show_recent_alerts(logs_dir, note):
    if not logs_dir:
        print(f"Cannot locate logs: {note}.")
        return
    p = os.path.join(logs_dir, time.strftime("%Y%m%d") + ".log")
    if not os.path.exists(p):
        print(f"No log file for today yet: {p}")
        return
    enc = detect_encoding(p)
    with open(p, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\xff\xfe":
        raw = raw[2:]
    elif raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    if enc == "utf-16-le":
        raw = raw[: len(raw) - (len(raw) % 2)]
    entries = entries_from_text(raw.decode(enc, errors="replace"))
    alerts = [a for a in (alert_text_from_entry(e) for e in entries) if a]
    print(f"\n{os.path.basename(p)}: {len(entries)} log entries, {len(alerts)} Alert() entries.")
    for a in alerts[-5:]:
        print("-----")
        print(a)
    if not alerts:
        print("No 'Alert:' lines found yet. Trigger an alert, then run this again.")


def show_raw_lines(logs_dir, note):
    if not logs_dir:
        print(f"Cannot locate logs: {note}.")
        return
    fp = os.path.join(logs_dir, time.strftime("%Y%m%d") + ".log")
    if not os.path.exists(fp):
        print(f"No log file for today yet: {fp}")
        return
    enc, sample = tail_sample(fp, 15)
    log(f"RAW LOG SAMPLE of {fp} (encoding {enc}):")
    for ln in sample:
        log(f"   {ln!r:.300}")


# ------------------------------------------------------------ menus
def menu_sources(cfg):
    while True:
        print("\n--- Sources (MT5 terminals) ---")
        list_sources(cfg)
        print(" a. Add source\n r. Remove source\n t. Tools for a source (log folder, portable mode, recent alerts...)\n b. Back")
        c = input("Choice: ").strip().lower()
        if c == "a":
            print(" 1. Pick from running MT5 terminals\n 2. Type the path to terminal64.exe")
            m = input("Choice: ").strip()
            exe = None
            if m == "1":
                picked = pick_terminal()
                exe = picked[1] if picked else None
            elif m == "2":
                exe = input("Full path to terminal64.exe: ").strip().strip('"')
                if not os.path.isfile(exe):
                    print("File not found.")
                    exe = None
            if not exe:
                continue
            if any(os.path.normcase(s["exe"]) == os.path.normcase(exe) for s in cfg["sources"]):
                print("That terminal is already a source.")
                continue
            default = os.path.basename(os.path.dirname(exe)) or "MT5"
            name = input(f"Name for this source (blank = '{default}'): ").strip() or default
            name = unique_name(cfg["sources"], name)
            src = {"name": name, "exe": exe, "log_dir": ""}
            cfg["sources"].append(src)
            save_settings(cfg)
            log(f"source added: {name} ({exe})")
            offer_routes(cfg, source=src)
        elif c == "r":
            i = ask_one("Source number to remove: ", len(cfg["sources"]))
            if i is not None:
                name = cfg["sources"].pop(i)["name"]
                cfg["routes"] = [r for r in cfg["routes"] if r["source"] != name]
                save_settings(cfg)
                log(f"source removed: {name} (its routes too)")
        elif c == "t":
            i = ask_one("Source number: ", len(cfg["sources"]))
            if i is not None:
                source_tools(cfg, cfg["sources"][i])
        elif c == "b":
            return
        else:
            print("Invalid choice.")


def menu_channels(cfg):
    while True:
        print("\n--- Channels (Telegram chats) ---")
        print(f" Default bot token: {mask(cfg['bot_token'])}")
        list_channels(cfg)
        print(" a. Add channel\n e. Edit channel (chat ID / own bot token)\n r. Remove channel\n d. Set default bot token\n b. Back")
        c = input("Choice: ").strip().lower()
        if c == "a":
            name = input("Channel name (e.g. VIP): ").strip()
            chat = input("Chat ID (e.g. 123456789 or -100xxxxxxxxxx): ").strip()
            if not name or not chat:
                print("Name and chat ID are required.")
                continue
            tok = input("Own bot token for this channel (blank = use the default bot): ").strip()
            ch = {"name": unique_name(cfg["channels"], name), "chat_id": chat, "bot_token": tok}
            cfg["channels"].append(ch)
            save_settings(cfg)
            log(f"channel added: {ch['name']} (chat {chat}, {'own bot' if tok else 'default bot'})")
            offer_routes(cfg, channel=ch)
        elif c == "e":
            i = ask_one("Channel number: ", len(cfg["channels"]))
            if i is not None:
                ch = cfg["channels"][i]
                v = input(f"Chat ID (current {ch['chat_id']}, blank = keep): ").strip()
                if v:
                    ch["chat_id"] = v
                t = input("Own bot token (blank = keep, '-' = use default bot): ").strip()
                if t == "-":
                    ch["bot_token"] = ""
                elif t:
                    ch["bot_token"] = t
                save_settings(cfg)
                log(f"channel edited: {ch['name']}")
        elif c == "r":
            i = ask_one("Channel number to remove: ", len(cfg["channels"]))
            if i is not None:
                name = cfg["channels"].pop(i)["name"]
                cfg["routes"] = [r for r in cfg["routes"] if r["channel"] != name]
                save_settings(cfg)
                log(f"channel removed: {name} (its routes too)")
        elif c == "d":
            v = input("Default bot token (blank = keep current): ").strip()
            if v:
                cfg["bot_token"] = v
                save_settings(cfg)
                log("default bot token changed")
        elif c == "b":
            return
        else:
            print("Invalid choice.")


def menu_routes(cfg):
    while True:
        print("\n--- Routes (which source feeds which channel) ---")
        list_routes(cfg)
        print(
            " 1. Link sources to channels (pick several of each: every pair gets linked)\n"
            " 2. Unlink sources from channels\n"
            " 3. Clear all routes\n"
            " b. Back"
        )
        c = input("Choice: ").strip().lower()
        if c in ("1", "2"):
            if not cfg["sources"] or not cfg["channels"]:
                print("Add at least one source and one channel first.")
                continue
            list_sources(cfg)
            si = ask_indices("Source numbers (e.g. 1,2 or a = all): ", len(cfg["sources"]))
            if si is None:
                continue
            list_channels(cfg)
            ci = ask_indices("Channel numbers (e.g. 1,2 or a = all): ", len(cfg["channels"]))
            if ci is None:
                continue
            for s in si:
                for ch in ci:
                    sn, cn = cfg["sources"][s]["name"], cfg["channels"][ch]["name"]
                    if c == "1":
                        link(cfg, sn, cn)
                    else:
                        before = len(cfg["routes"])
                        cfg["routes"] = [r for r in cfg["routes"] if not (r["source"] == sn and r["channel"] == cn)]
                        if len(cfg["routes"]) < before:
                            log(f"route removed: {sn} -> {cn}")
            save_settings(cfg)
        elif c == "3":
            if input("Remove ALL routes? (y/n): ").strip().lower() == "y":
                cfg["routes"] = []
                save_settings(cfg)
                log("all routes cleared")
        elif c == "b":
            return
        else:
            print("Invalid choice.")


def menu_capture(cfg):
    while True:
        print("\n--- Capture mode ---")
        print(f" Popup grabber : {'ON' if cfg['popup_enabled'] else 'OFF'}   (fast, ~0.2s)")
        print(f" Experts log   : {'ON' if cfg['log_enabled'] else 'OFF'}   (MT5 writes it in delayed bursts)")
        if cfg["popup_enabled"] and cfg["log_enabled"]:
            print(f" -> BOTH: popup sends instantly. The log is a safety net. A log alert is sent only if it is\n"
                  f"    at most {cfg['recover_seconds']}s old AND the same text was not already delivered around that time.")
        elif cfg["log_enabled"]:
            print(" -> LOG only: every alert found in the log is sent (can be 10-50s late).")
        print(" 1. Toggle popup grabber\n 2. Toggle Experts log\n 3. Set recovery window seconds (log safety net)\n b. Back")
        c = input("Choice: ").strip().lower()
        if c == "1":
            cfg["popup_enabled"] = not cfg["popup_enabled"]
        elif c == "2":
            cfg["log_enabled"] = not cfg["log_enabled"]
        elif c == "3":
            v = input(f"Recovery window in seconds (current {cfg['recover_seconds']}): ").strip()
            if v.isdigit() and 5 <= int(v) <= 3600:
                cfg["recover_seconds"] = int(v)
            elif v:
                print("Use a whole number between 5 and 3600.")
        elif c == "b":
            return
        else:
            print("Invalid choice.")
            continue
        save_settings(cfg)
        log(f"capture: popup {'ON' if cfg['popup_enabled'] else 'OFF'}, log {'ON' if cfg['log_enabled'] else 'OFF'}, "
            f"recover window {cfg['recover_seconds']}s")


def action_filter(cfg):
    print(
        "\nFilter mode (applies to popup AND log):\n"
        "  1. GLOBAL - forward every alert (no keyword)\n"
        "  2. Keyword - only forward alerts containing a keyword\n"
        "  3. Edit keyword (keyword mode)\n"
        "  4. Edit popup 'must also contain' text (popup + keyword mode, default Entry)\n"
        "  b. Back"
    )
    c = input("Choice: ").strip().lower()
    if c == "1":
        cfg["global_mode"] = True
        log("filter: GLOBAL mode ON, all alerts will be sent")
    elif c == "2":
        cfg["global_mode"] = False
        v = input(f"Keyword (current '{cfg['keyword']}', blank = keep, '-' = none): ").strip()
        if v == "-":
            cfg["keyword"] = ""
        elif v:
            cfg["keyword"] = v
        log(f"filter: keyword mode ON, keyword '{cfg['keyword']}'")
    elif c == "3":
        v = input(f"Keyword (current '{cfg['keyword']}', '-' = none): ").strip()
        if v == "-":
            cfg["keyword"] = ""
        elif v:
            cfg["keyword"] = v
        log(f"filter: keyword now '{cfg['keyword']}'")
    elif c == "4":
        v = input(f"Must-also-contain text (current '{cfg['require_text']}', '-' = none): ").strip()
        if v == "-":
            cfg["require_text"] = ""
        elif v:
            cfg["require_text"] = v
        log(f"filter: popup must-contain text now '{cfg['require_text']}'")
    else:
        return
    save_settings(cfg)


def action_title(cfg):
    run = running_map()
    titles = []
    for s in cfg["sources"]:
        pid = run.get(os.path.normcase(s["exe"]))
        if pid:
            titles = window_titles(pid)
            break
    if titles:
        print("\nWindow titles currently open in the first running source:")
        for i, t in enumerate(titles, 1):
            print(f"  {i}. {t}")
    else:
        print("(Start MT5 and add it as a source to list its windows, or just type a title.)")
    v = input(f"Number from list or type a title (current '{cfg['window_title']}', blank = keep): ").strip()
    if not v:
        return
    cfg["window_title"] = titles[int(v) - 1] if v.isdigit() and titles and 1 <= int(v) <= len(titles) else v
    save_settings(cfg)
    log(f"popup title set to '{cfg['window_title']}'")


def action_poll(cfg):
    v = input(f"Poll interval in seconds, 0.05 - 5 (current {cfg['poll_seconds']}): ").strip()
    if not v:
        return
    try:
        f = float(v)
        if 0.05 <= f <= 5:
            cfg["poll_seconds"] = f
            save_settings(cfg)
            log(f"poll interval set to {f}s")
        else:
            print("Out of range.")
    except ValueError:
        print("Not a number.")


def action_dump(cfg):
    i = ask_one("Source number to dump: ", len(cfg["sources"])) if cfg["sources"] else None
    if i is None:
        print("Add a source first.")
        return
    src = cfg["sources"][i]
    pid = resolve_source_pid(src)
    if not pid:
        print("MT5 is not running for that source.")
        return
    holder = {"d": None}

    def get_desktop():
        if holder["d"] is None:
            holder["d"] = Desktop(backend="uia")
        return holder["d"]

    log(f"DUMP requested for source {src['name']} (PID {pid})")
    print(f"\nAll top-level windows of PID {pid}:")
    for hwnd, t, vis in win32_top_windows(pid):
        print(f"   hwnd {hwnd} visible={vis} title='{t}'")
    want = cfg["window_title"].strip().casefold()
    hw = {h for h, t, _v in win32_top_windows(pid) if want in t.strip().casefold()}
    wins = find_popups(get_desktop, pid, cfg["window_title"])
    if not wins and not hw:
        log(f"DUMP: no window with title containing '{cfg['window_title']}' is open. Trigger the popup first.", "WARN")
        return
    for h in hw:
        _snapped.discard(h)
        snapshot_popup(h, next((w for w in wins if w.handle == h), None))
    for w in wins:
        print(f"\n=== UIA tree, window handle {w.handle} ===")
        w.print_control_identifiers()


def show_own_log():
    p = log_file_for(datetime.now())
    print(f"\nLog folder: {LOG_DIR}")
    print(f"Today's file: {p}  (files {LOG_KEEP_DAYS}+ days old are deleted automatically)")
    if not os.path.exists(p):
        print("No log written yet today.")
        return
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    print("---- last 40 lines ----")
    print("\n".join(lines[-40:]))


def show_status(cfg):
    print("\n=============== MT5 Alert to Telegram ===============")
    if cfg["popup_enabled"] and cfg["log_enabled"]:
        cap = f"popup ON + log ON (safety net, recovers alerts up to {cfg['recover_seconds']}s old)"
    elif cfg["popup_enabled"]:
        cap = "popup only"
    elif cfg["log_enabled"]:
        cap = "Experts log only"
    else:
        cap = "NOTHING ENABLED"
    print(f" Capture          : {cap}")
    print(f" Default bot      : {mask(cfg['bot_token'])}")
    print(" Sources          :")
    list_sources(cfg)
    print(" Channels         :")
    list_channels(cfg)
    print(" Routes           :")
    list_routes(cfg)
    if cfg["global_mode"]:
        print(" Filter           : GLOBAL (all alerts, no keyword)")
    else:
        extra = f" + popup must contain '{cfg['require_text']}'" if cfg["require_text"] and cfg["popup_enabled"] else ""
        print(f" Filter           : keyword '{cfg['keyword'] or '(none)'}'{extra}")
    print(f" Popup title      : {cfg['window_title']}   Poll: {cfg['poll_seconds']}s")
    print(f" Send on old-popup update: {'YES' if cfg['send_on_update'] else 'NO'}   "
          f"Send already-open popup on start: {'YES' if cfg['send_existing_on_start'] else 'NO'}")
    print(f" Logs             : {LOG_DIR}")
    print("=====================================================")


def main():
    if sys.version_info < (3, 8):
        sys.exit("Python 3.8 or newer is required.")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    cfg = load_settings()
    set_redact(cfg)
    log(f"=== forwarder started | python {sys.version.split()[0]} | {os.path.abspath(__file__)} ===", console=False)
    while True:
        show_status(cfg)
        print(
            " 1. Sources  (MT5 terminals to monitor)\n"
            " 2. Channels (Telegram chats)\n"
            " 3. Routes   (which source feeds which channel)\n"
            " 4. Capture mode (popup / Experts log / both = safety net)\n"
            " 5. Filter mode (GLOBAL all alerts / keyword)\n"
            " 6. Set popup window title\n"
            " 7. Set poll interval\n"
            " 8. Toggle: send when an OLD popup's text updates\n"
            " 9. Toggle: send the already-open popup when watching starts (for testing)\n"
            "10. Send Telegram test message to every channel\n"
            "11. Dump popup control tree (debug)\n"
            "12. START watching\n"
            "13. View this script's log (last 40 lines)\n"
            " 0. Exit"
        )
        c = input("Choice: ").strip()
        log(f"menu choice: {c}", console=False)
        try:
            if c == "1":
                menu_sources(cfg)
            elif c == "2":
                menu_channels(cfg)
            elif c == "3":
                menu_routes(cfg)
            elif c == "4":
                menu_capture(cfg)
            elif c == "5":
                action_filter(cfg)
            elif c == "6":
                action_title(cfg)
            elif c == "7":
                action_poll(cfg)
            elif c == "8":
                cfg["send_on_update"] = not cfg["send_on_update"]
                save_settings(cfg)
                log(f"send on old-window update: {'YES' if cfg['send_on_update'] else 'NO'}")
            elif c == "9":
                cfg["send_existing_on_start"] = not cfg["send_existing_on_start"]
                save_settings(cfg)
                log(f"send already-open popup on start: {'YES' if cfg['send_existing_on_start'] else 'NO'}")
            elif c == "10":
                test_telegram(cfg)
            elif c == "11":
                action_dump(cfg)
            elif c == "12":
                start_watching(cfg)
            elif c == "13":
                show_own_log()
            elif c == "0":
                log("=== forwarder exited ===", console=False)
                print("Bye.")
                sys.exit(0)
            else:
                print("Invalid choice.")
        except KeyboardInterrupt:
            print("\n(cancelled)")
        except SystemExit:
            raise
        except Exception:
            log_exception(f"menu choice {c}")


if __name__ == "__main__":
    main()