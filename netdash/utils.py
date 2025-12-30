import asyncio
import os
import platform
import subprocess
from typing import Dict, List, Tuple

try:
    import fcntl
    import selectors
    import signal
    import time
except Exception:
    fcntl = None
    selectors = None
    signal = None
    time = None


def normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


async def _run_async(cmd: List[str], timeout: int = 5) -> Tuple[int, str, str]:
    if _can_use_posix_spawn():
        return await asyncio.to_thread(_run_posix_spawn, cmd, timeout)
    if platform.system().lower() == "darwin":
        return 999, "", "posix_spawn unavailable; refusing to fork on macOS"
    try:
        env = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode if proc.returncode is not None else 0, stdout.decode(), stderr.decode()
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return 999, "", "timeout"
    except Exception as e:
        return 999, "", str(e)


def _run(cmd: List[str], timeout: int = 5) -> Tuple[int, str, str]:
    if _can_use_posix_spawn():
        return _run_posix_spawn(cmd, timeout)
    if platform.system().lower() == "darwin":
        return 999, "", "posix_spawn unavailable; refusing to fork on macOS"
    try:
        clean_env: Dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=clean_env)
        if p.returncode < 0 or p.returncode > 100:
            print(f"DEBUG: Process {cmd} failed with rc={p.returncode}")
            print(f"DEBUG: Stderr: {p.stderr}")
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        print(f"DEBUG: Exception in _run for {cmd}: {e}")
        return 999, "", str(e)


def _can_use_posix_spawn() -> bool:
    return (
        hasattr(os, "posix_spawnp")
        and hasattr(os, "POSIX_SPAWN_DUP2")
        and hasattr(os, "POSIX_SPAWN_CLOSE")
        and fcntl is not None
        and selectors is not None
        and signal is not None
        and time is not None
    )


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _decode_wait_status(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return 999


def _run_posix_spawn(cmd: List[str], timeout: int = 5) -> Tuple[int, str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()
    file_actions = [
        (os.POSIX_SPAWN_DUP2, stdout_w, 1),
        (os.POSIX_SPAWN_DUP2, stderr_w, 2),
        (os.POSIX_SPAWN_CLOSE, stdout_r),
        (os.POSIX_SPAWN_CLOSE, stderr_r),
        (os.POSIX_SPAWN_CLOSE, stdout_w),
        (os.POSIX_SPAWN_CLOSE, stderr_w),
    ]
    try:
        pid = os.posix_spawnp(cmd[0], cmd, env, file_actions=file_actions)
    except Exception as e:
        for fd in (stdout_r, stdout_w, stderr_r, stderr_w):
            try:
                os.close(fd)
            except Exception:
                pass
        return 999, "", str(e)

    os.close(stdout_w)
    os.close(stderr_w)
    _set_nonblocking(stdout_r)
    _set_nonblocking(stderr_r)

    selector = selectors.DefaultSelector()
    selector.register(stdout_r, selectors.EVENT_READ)
    selector.register(stderr_r, selectors.EVENT_READ)
    stdout_chunks: List[bytes] = []
    stderr_chunks: List[bytes] = []

    start = time.monotonic()
    rc = None
    stdout_open = True
    stderr_open = True
    while stdout_open or stderr_open or rc is None:
        if timeout and (time.monotonic() - start) > timeout:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except Exception:
                pass
            for fd in (stdout_r, stderr_r):
                try:
                    os.close(fd)
                except Exception:
                    pass
            return 999, "", "timeout"

        for key, _mask in selector.select(timeout=0.1):
            fd = key.fd
            try:
                data = os.read(fd, 4096)
            except BlockingIOError:
                data = b""
            if not data:
                selector.unregister(fd)
                try:
                    os.close(fd)
                except Exception:
                    pass
                if fd == stdout_r:
                    stdout_open = False
                else:
                    stderr_open = False
                continue
            if fd == stdout_r:
                stdout_chunks.append(data)
            else:
                stderr_chunks.append(data)

        if rc is None:
            try:
                pid_done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pid_done, status = pid, 0
            if pid_done == pid:
                rc = _decode_wait_status(status)

    final_rc = rc if rc is not None else 0
    out = b"".join(stdout_chunks).decode()
    err = b"".join(stderr_chunks).decode()
    if final_rc < 0 or final_rc > 100:
        print(f"DEBUG: Process {cmd} failed with rc={final_rc}")
        print(f"DEBUG: Stderr: {err}")
    return final_rc, out, err
