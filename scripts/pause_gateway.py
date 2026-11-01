#!/usr/bin/env python3
"""
通过 Hermes gateway 的 Unix Socket 发送优雅停机指令。

背景：
  gateway 进程以应用专属用户（如 hermes-desktop）运行并 daemonize，
  PPID=1。root 之外的用户即使属于同一组，也无法对它发信号
  （Linux 只允许 root 或同 uid 互相 signal）。
  所以 `pkill -f "..."` 在应用脚本之外执行往往杀不掉它。

  gateway 提供了 Unix Socket 控制接口，支持 verb "pause-for-update"，
  正是"准备更新/卸载"用的优雅停机：它会 drain 请求、停掉监听、
  然后自行退出。这是比 kill 更干净的做法 —— 不会留下半开的
  数据库或 socket 文件。

用法：
    python3 scripts/pause_gateway.py [HERMES_HOME]
返回码：0 = 已发出停机指令或无需停机；1 = 失败
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time


def pause(home: str, timeout: float = 8.0) -> tuple[bool, str]:
    sock_path = os.path.join(home, "gateway.sock")
    if not os.path.exists(sock_path):
        return True, "无 gateway.sock，无需停机"

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)
    except Exception as exc:
        return False, f"连接 gateway.sock 失败：{exc}"

    try:
        s.send(json.dumps({"verb": "pause-for-update"}).encode() + b"\n")
        raw = s.recv(4096).decode("utf-8", "replace")
    except Exception as exc:
        return False, f"发送停机指令失败：{exc}"
    finally:
        try:
            s.close()
        except Exception:
            pass

    try:
        resp = json.loads(raw)
    except Exception:
        return False, f"无法解析响应：{raw[:160]}"

    if resp.get("ok"):
        result = resp.get("result") or {}
        return True, (f"已请求停机（pid={result.get('pid')}，"
                      f"drain_timeout={result.get('drain_timeout')}）")
    return False, f"停机指令被拒绝：{resp.get('error') or raw[:160]}"


def main() -> int:
    home = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "TRIM_PKGVAR", "/vol1/@appdata/hermes-desktop")
    ok, msg = pause(home)
    print(msg)
    if ok:
        # 给它一点时间完成 drain 与退出
        time.sleep(3)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
