import subprocess
import os
import threading
import json
import asyncio

def check_tailscale(label="Main"):
    ts_exe = "tailscale"
    cmd = [ts_exe, "status", "--json"]
    print(f"[{label}] Running: {' '.join(cmd)}")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=7)
        print(f"[{label}] Return code: {p.returncode}")
        if p.stdout:
             print(f"[{label}] Stdout: {p.stdout[:50]}...")
        if p.stderr:
             print(f"[{label}] Stderr: {p.stderr[:50]}...")
    except Exception as e:
        print(f"[{label}] Exception: {e}")

async def main():
    check_tailscale("Main")
    print("[Async] Running to_thread...")
    await asyncio.to_thread(check_tailscale, "to_thread")

if __name__ == "__main__":
    asyncio.run(main())
