
# Windows named mutex has no stale lock file and is released automatically on process exit.
import ctypes as _si_ctypes
import sys as _si_sys

_si_mutex = _si_ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\CHZZK_RECORDER_ALL_IN_ONE_V1")
if not _si_mutex:
    raise OSError("All-in-One mutex creation failed")
if _si_ctypes.windll.kernel32.GetLastError() == 183:
    print("[SINGLE_INSTANCE] All-in-One이 이미 실행 중입니다. 새 실행을 종료합니다.", flush=True)
    _si_sys.exit(73)

import os
import json
import ctypes
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SUPERVISOR_STATUS_PATH = BASE_DIR / ".chzzk_supervisor_status.json"
SUPERVISOR_COMMAND_PATH = BASE_DIR / ".chzzk_supervisor_command.json"

# Uploader is intentionally NOT auto-started.
# Use dashboard buttons to start/stop uploader.
PROGRAMS = [
    ("DASHBOARD", [sys.executable, "-m", "uvicorn", "dashboard_server:app", "--host", "127.0.0.1", "--port", "8765", "--no-access-log"], True),
    ("RECORDER", [sys.executable, "-u", "recorder_v07.py"], True),
    ("TS_SWEEPER", [sys.executable, "-u", "stable_ts_sweeper.py"], True),
    ("MERGE_WORKER", [sys.executable, "-u", "drive_parts_merge_worker.py"], True),
]


def setup_console():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def line(prefix, msg):
    print(f"[{now()}] [{prefix}] {msg}", flush=True)


class ManagedProcess:
    def __init__(self, name, cmd, restart=True):
        self.name = name
        self.cmd = cmd
        self.restart = restart
        self.proc = None
        self.thread = None
        self.stop_requested = False
        self.last_start = 0
        self.adopted_pid = None
        self.restart_due = None

    @property
    def pid(self):
        return self.proc.pid if self.proc is not None else self.adopted_pid

    def adopt(self, pid):
        self.adopted_pid = int(pid)
        self.proc = None
        self.last_start = time.time()
        line("SYSTEM", f"{self.name} existing process adopted. PID={self.adopted_pid}")

    def script_exists(self):
        for item in self.cmd:
            if item.endswith(".py"):
                return (BASE_DIR / item).exists()
        return True

    def start(self):
        if not self.script_exists():
            line("SYSTEM", f"{self.name} script missing, skipped: {' '.join(self.cmd)}")
            return

        self.last_start = time.time()
        self.adopted_pid = None
        self.stop_requested = False
        self.restart_due = None

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )

        line("SYSTEM", f"{self.name} started. PID={self.proc.pid}")
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        try:
            if not self.proc or not self.proc.stdout:
                return
            for raw in self.proc.stdout:
                text = raw.rstrip("\r\n")
                if text:
                    line(self.name, text)
        except Exception as e:
            line("SYSTEM", f"{self.name} log reader error: {e}")

    def poll(self):
        if self.adopted_pid is not None:
            return None if process_is_alive(self.adopted_pid) else 0
        if self.proc is None:
            return None
        return self.proc.poll()

    def stop(self):
        self.stop_requested = True

        if self.adopted_pid is not None:
            pid = self.adopted_pid
            line("SYSTEM", f"Stopping adopted {self.name}... PID={pid}")
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
                for _ in range(20):
                    if not process_is_alive(pid):
                        break
                    time.sleep(0.5)
            except Exception as e:
                line("SYSTEM", f"{self.name} adopted process stop warning: {e}")
            self.adopted_pid = None
            return

        if self.proc is None or self.proc.poll() is not None:
            return

        line("SYSTEM", f"Stopping {self.name}... PID={self.proc.pid}")

        try:
            if os.name == "nt":
                try:
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                    self.proc.wait(timeout=10)
                    return
                except Exception:
                    pass
                self.proc.terminate()
            else:
                self.proc.send_signal(signal.SIGINT)

            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

        except Exception as e:
            line("SYSTEM", f"{self.name} stop error: {e}")


def process_is_alive(pid):
    if not pid:
        return False
    try:
        open_process = ctypes.windll.kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        handle = open_process(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259
    except Exception:
        return False


def find_existing_script_process(script_name, exclude_pid=None):
    """Find an already running Python child by its script name.

    The supervisor status file can briefly miss an adopted child when the
    supervisor itself is replaced.  Named mutexes prevent duplication, but
    without this lookup the supervisor would keep launching short-lived
    duplicates forever.  Query Windows read-only and adopt the oldest match.
    """
    if os.name != "nt":
        return None
    safe_name = str(script_name).replace("'", "''")
    ps = (
        "$items=Get-CimInstance Win32_Process | "
        "Where-Object {$_.Name -eq 'python.exe' -and "
        f"$_.CommandLine -like '*{safe_name}*'}} | "
        "Sort-Object CreationDate | Select-Object -ExpandProperty ProcessId; "
        "$items"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            return None
        for raw in completed.stdout.splitlines():
            try:
                pid = int(raw.strip())
            except Exception:
                continue
            if pid != int(exclude_pid or 0) and process_is_alive(pid):
                return pid
    except Exception:
        pass
    return None


def write_supervisor_status(processes):
    data = {
        "supervisor_pid": os.getpid(),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_epoch": time.time(),
        "children": {p.name: p.pid for p in processes if p.pid and p.poll() is None},
    }
    # Dashboard/watchdog readers can briefly hold the JSON open on Windows.
    # A single sharing violation must never unwind main() and stop every child.
    # Use a per-process temp file and retry the atomic replace for a short time.
    tmp = SUPERVISOR_STATUS_PATH.with_name(
        f"{SUPERVISOR_STATUS_PATH.name}.{os.getpid()}.tmp"
    )
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        last_error = None
        for attempt in range(30):
            try:
                os.replace(tmp, SUPERVISOR_STATUS_PATH)
                return True
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.05 + min(attempt, 10) * 0.01)
        line("SYSTEM", f"Supervisor status update skipped after sharing retries: {last_error}")
        return False
    except Exception as exc:
        # Status reporting is diagnostic.  Child supervision must keep running
        # even if the status file is temporarily unavailable or read-only.
        line("SYSTEM", f"Supervisor status update warning: {type(exc).__name__}: {exc}")
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def load_adoptable_children():
    try:
        data = json.loads(SUPERVISOR_STATUS_PATH.read_text(encoding="utf-8"))
        old_supervisor = int(data.get("supervisor_pid") or 0)
        if old_supervisor and process_is_alive(old_supervisor):
            return {}
        return {name: int(pid) for name, pid in (data.get("children") or {}).items()
                if pid and process_is_alive(pid)}
    except Exception:
        return {}


def handle_supervisor_command(processes):
    if not SUPERVISOR_COMMAND_PATH.exists():
        return
    try:
        command = json.loads(SUPERVISOR_COMMAND_PATH.read_text(encoding="utf-8"))
        action = command.get("action")
        target = command.get("target")
        proc = next((p for p in processes if p.name == target), None)
        if not proc:
            line("SYSTEM", f"Unknown supervisor command target: {target}")
            return
        line("SYSTEM", f"Supervisor command received: {action} {target}")
        if action == "start" and proc.poll() is None and proc.pid:
            line("SYSTEM", f"{target} is already running. Duplicate start ignored. PID={proc.pid}")
            return
        if action in ("stop", "reload"):
            proc.stop()
        if action in ("start", "reload"):
            time.sleep(1.0)
            proc.start()
    except Exception as e:
        line("SYSTEM", f"Supervisor command error: {e}")
    finally:
        SUPERVISOR_COMMAND_PATH.unlink(missing_ok=True)


def main():
    setup_console()

    print("=" * 90)
    line("SYSTEM", "CHZZK All-in-One Supervisor started")
    line("SYSTEM", "Uploader is NOT auto-started.")
    line("SYSTEM", "Use dashboard buttons to start/stop uploader.")
    line("SYSTEM", f"Folder: {BASE_DIR}")
    line("SYSTEM", "Running: dashboard / recorder / TS sweeper / unified merge worker")
    line("SYSTEM", "Stop all: Ctrl + C")
    print("=" * 90)

    processes = [ManagedProcess(name, cmd, restart) for name, cmd, restart in PROGRAMS]
    adoptable = load_adoptable_children()

    for proc in processes:
        old_pid = adoptable.get(proc.name)
        if old_pid:
            proc.adopt(old_pid)
        else:
            script_name = next((item for item in proc.cmd if item.endswith(".py")), "")
            existing_pid = find_existing_script_process(script_name) if script_name else None
            if existing_pid:
                proc.adopt(existing_pid)
            else:
                proc.start()
        time.sleep(1.0)
    write_supervisor_status(processes)

    print("=" * 90)
    line("SYSTEM", "Dashboard: http://127.0.0.1:8765")
    line("SYSTEM", "Start uploader only from dashboard when needed.")
    print("=" * 90)

    try:
        while True:
            time.sleep(3)
            handle_supervisor_command(processes)
            write_supervisor_status(processes)

            for proc in processes:
                code = proc.poll()

                if code is not None and not proc.stop_requested:
                    if code == 73:
                        script_name = next((item for item in proc.cmd if item.endswith(".py")), "")
                        existing_pid = find_existing_script_process(script_name, exclude_pid=proc.pid)
                        if existing_pid:
                            proc.adopt(existing_pid)
                            proc.restart_due = None
                            continue
                    if proc.restart and proc.restart_due is None:
                        elapsed = time.time() - proc.last_start
                        wait = 10 if elapsed < 20 else 3
                        proc.restart_due = time.time() + wait
                        line("SYSTEM", f"{proc.name} exited. code={code}; restart scheduled after {wait}s")
                    if proc.restart and proc.restart_due is not None and time.time() >= proc.restart_due:
                        proc.start()

    except KeyboardInterrupt:
        print()
        line("SYSTEM", "Ctrl+C detected. Stopping all.")

    finally:
        for proc in reversed(processes):
            proc.stop()
        line("SYSTEM", "All stopped.")


if __name__ == "__main__":
    main()


