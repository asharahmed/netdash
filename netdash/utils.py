import asyncio
import os
import subprocess
from typing import Dict, List, Tuple


def normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


async def _run_async(cmd: List[str], timeout: int = 5) -> Tuple[int, str, str]:
    try:
        cmd_str = " ".join(f'"{c}"' for c in cmd)
        env = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

        proc = await asyncio.create_subprocess_shell(
            cmd_str,
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
