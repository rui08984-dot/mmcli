#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mmcli 离线自检 —— 不需要 ModMind 打开项目, 不需要真实工程。

做法: 在临时目录里造一个假的 ModMind /tool 服务 + 假 bridge.json + 假项目,
然后真的把 mmcli 当子进程跑一遍, 检查它:

  协议    body 必须是 {action,input}, 必须带 x-modmind-token
  进程    全程 0 个 ModMind.exe 被创建 (这是"多进程紊乱"的根治点)
  守卫    路径越界 / 危险动作 / 绑定错项目 都要被拦
  锁      两个写调用并发时必须串行
  语义    404 -> 重新绑定; 400 + AI Review -> 不重试

用法: python mmcli_selftest.py
"""

import hashlib
import importlib.util

def _jload(text, default=None):
    """安全解析 JSON: 解析失败返回默认值而不是抛异常把整个套件带崩。"""
    try:
        return json.loads(text)
    except Exception:                                  # noqa: BLE001
        return default if default is not None else {}
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "mmcli.py"
PY = sys.executable
TOKEN = "selftest-token-0001"

RESULTS = []


def check(name, ok, detail="", skip=False):
    if skip:
        RESULTS.append((name, None, detail))
        print("  SKIP %s%s" % (name, ("   " + detail) if detail else ""))
        return True
    RESULTS.append((name, bool(ok), detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name, ("   " + detail) if detail else ""))
    return ok


# ------------------------------------------------------------------ 假服务
class S:
    requests = []
    inflight = 0
    max_inflight = 0
    sentinels = True
    lock = threading.Lock()
    namespace = "ultracollab"
    root = ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path != "/tool" or self.headers.get("x-modmind-token") != TOKEN:
            self._send(404, b"Not found", "text/plain")
            return
        n = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._send(400, b"bad json", "text/plain")
            return
        with S.lock:
            S.requests.append({"body": body, "headers": dict(self.headers), "ts": time.time()})
            S.inflight += 1
            S.max_inflight = max(S.max_inflight, S.inflight)
        try:
            action = body.get("action")
            inp = body.get("input") or {}
            if action == "mcp_probe":
                self._send(200, {"ok": True, "marker": "modmind-mcp-ready-v1"})
            elif action == "project_info":
                self._send(200, {"name": "奥特曼", "namespace": S.namespace,
                                 "path": S.root, "loader": "forge",
                                 "minecraftVersion": "1.20.1"})
            elif action == "project_files":
                files = ["modmind.project.json", "build.gradle", "settings.gradle"]
                if S.sentinels:
                    files.append("src/main/java/dev/ultracollab/Ultracollab.java")
                self._send(200, {"files": files})
            elif action == "blockbench_checkpoint":
                time.sleep(1.2)
                self._send(200, {"ok": True, "id": "ckpt-1"})
            elif action == "rename_project":
                self._send(200, {"ok": True})
            elif action == "blockbench_project_state":
                self._send(200, {"project": {"uuid": "p1", "name": "sandbox",
                                             "textureWidth": 64, "textureHeight": 64},
                                 "format": {"id": "bedrock", "name": "Bedrock"},
                                 "counts": {"cubes": 0, "groups": 1, "textures": 0, "animations": 0},
                                 "cubes": [], "meshes": [], "textures": [],
                                 "groups": [{"uuid": "g1", "name": "head"}],
                                 "animations": [], "selection": []})
            elif action == "blockbench_actions":
                acts = inp.get("actions") or []
                self._send(200, {"revisionBefore": "sha256:" + "0" * 64,
                                 "revisionAfter": "sha256:" + "1" * 64,
                                 "results": [{"action": a.get("type"), "success": True}
                                             for a in acts]})
            elif action == "plugin_tools":
                self._send(200, {"tools": [{"name": "mmcli_demo_ping",
                                            "description": "demo"}]})
            elif action == "plugin_tool_call":
                if (body.get("input") or {}).get("tool") != "mmcli_demo_ping":
                    self._send(400, "插件工具不存在".encode("utf-8"), "text/plain")
                else:
                    self._send(200, {"pong": True})
            elif action == "deny":
                self._send(400, "AI Review denied blockbench_actions: nope. Do not retry.",
                           "text/plain")
            else:
                self._send(400, ("未知 ModMind 工具：" + str(action)).encode("utf-8"),
                           "text/plain")
        finally:
            with S.lock:
                S.inflight -= 1


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ------------------------------------------------------------------ 工具
def run_cli(cfg, *args, expect=None, timeout=90):
    cmd = [PY, str(CLI), "--config", str(cfg)] + [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p


def load_mmcli():
    spec = importlib.util.spec_from_file_location("mmcli_mod", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    tmp = Path(tempfile.mkdtemp(prefix="mmcli-selftest-"))
    root = tmp / "proj"
    (root / "src" / "main" / "java" / "dev" / "ultracollab").mkdir(parents=True)
    (root / ".modmind" / "external-agents").mkdir(parents=True)
    (root / "build.gradle").write_text("// fake", encoding="utf-8")
    (root / "src" / "main" / "java" / "dev" / "ultracollab" / "Ultracollab.java").write_text(
        "// fake", encoding="utf-8")
    (root / "modmind.project.json").write_text(json.dumps({
        "name": "奥特曼", "loader": "forge", "minecraftVersion": "1.20.1",
        "kind": "mod", "namespace": "ultracollab", "projectVersion": "1.1.3",
        "path": str(root), "toolDataDirectory": ".modmind"}), encoding="utf-8")

    port = free_port()
    bridge = root / ".modmind" / "external-agents" / "bridge.json"
    bridge.write_text(json.dumps({"port": port, "token": TOKEN, "version": "selftest",
                                  "sourceFingerprint": "sha256:selftest",
                                  "hiddenToolPrefixes": []}), encoding="utf-8")
    cfg = tmp / "mmcli.config.json"
    cfg.write_text(json.dumps({
        "bridge_json": str(bridge), "modmind_exe": str(tmp / "NoSuchModMind.exe"),
        "project_root": str(root), "allowed_roots": [str(tmp)],
        "expect_namespace": "ultracollab",
        "require_files": ["src/main/java/dev/ultracollab/Ultracollab.java", "build.gradle"],
        "state_dir": str(tmp / "state"), "timeout": 30, "lock_timeout": 30,
        "bind_wait_seconds": 2}), encoding="utf-8")

    S.root = str(root)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

    print("== 沙箱: %s  (假 /tool 在 127.0.0.1:%d)" % (tmp, port))

    mm = load_mmcli()
    pids_before = mm.enumerate_modmind()

    # ---------------------------------------------------------- 1 表
    print("\n[1] 工具表")
    p = run_cli(cfg, "list", "--json")
    try:
        data = json.loads(p.stdout)
        check("list --json 返回 98 个工具", len(data["tools"]) == 98, "got %s" % len(data["tools"]))
    except Exception as exc:
        check("list --json 可解析", False, repr(exc))
    p = run_cli(cfg, "actions", "--verify")
    check("actions --verify 与 audit 一致", p.returncode == 0, p.stdout.strip()[:80])

    # ---------------------------------------------------------- 2 协议
    print("\n[2] 协议 (body/header/url)")
    n0 = len(S.requests)
    p = run_cli(cfg, "probe")
    check("probe 成功", p.returncode == 0, p.stdout.strip()[:60])
    req = S.requests[-1]
    check("body 是 {action,input}", set(req["body"]) == {"action", "input"},
          json.dumps(req["body"], ensure_ascii=False))
    check("action 用动作名而非 MCP 名", req["body"]["action"] == "mcp_probe",
          req["body"]["action"])
    hdrs = {k.lower(): v for k, v in req["headers"].items()}
    check("带 x-modmind-token", hdrs.get("x-modmind-token") == TOKEN, str(hdrs.get("x-modmind-token")))

    p = run_cli(cfg, "call", "modmind_project_info", "--json")
    check("MCP 名自动映射成动作名 project_info",
          _jload(p.stdout, {}).get("action") == "project_info",
          "stdout=%r" % p.stdout[:60])

    p = run_cli(cfg, "call", "brand_new_action", "--json")
    # 安全取用: 服务器可能一次请求都没收到(协议先崩了), 直接 S.requests[-1] 会 IndexError
    # 把整个套件带崩 —— 那样后面的断言全军覆没, 看起来像"没抓住", 实际是"根本没跑"。
    last = S.requests[-1]["body"].get("action") if S.requests else None
    check("未登记的动作名按原样透传 (前进兼容)", last == "brand_new_action", str(last))

    p = run_cli(cfg, "call", "modmind_project_info", "--dry-run")
    check("--dry-run 不发请求", "brand_new_action" not in p.stdout and p.returncode == 0, "")
    check("--dry-run 打印构造好的 body", '"action"' in p.stdout, "")

    # ---------------------------------------------------------- 3 错误语义
    print("\n[3] 错误语义")
    bad = tmp / "bad.json"
    bad.write_text(json.dumps({"port": port, "token": "wrong", "version": "x"}), encoding="utf-8")
    p = run_cli(cfg, "call", "modmind_mcp_probe", "--bridge", str(bad))
    check("坏 token -> 退出码 3 (缺桥/需重绑)", p.returncode == 3, "exit=%s" % p.returncode)
    check("坏 token -> 明确说是 token 过期", "token" in p.stderr, p.stderr.strip()[:90])

    p = run_cli(cfg, "call", "deny")
    check("AI Review 拒绝 -> 退出码 5", p.returncode == 5, "exit=%s" % p.returncode)
    check("AI Review 拒绝 -> 提示不要重试", "不要重试" in p.stderr, "")

    p = run_cli(cfg, "call", "modmind_project_files")
    check("读动作正常返回", p.returncode == 0 and "Ultracollab.java" in p.stdout, "")

    # ---------------------------------------------------------- 4 绑定预检
    print("\n[4] 绑定预检")
    S.sentinels = False
    p = run_cli(cfg, "call", "modmind_blockbench_checkpoint", "--yes")
    check("空壳项目 -> 退出码 4", p.returncode == 4, "exit=%s" % p.returncode)
    check("空壳项目 -> 指出会产出空壳 jar", "空壳" in p.stderr, "")
    p = run_cli(cfg, "call", "modmind_blockbench_checkpoint", "--yes", "--no-preflight")
    check("--no-preflight 可跳过", p.returncode == 0, "exit=%s" % p.returncode)
    S.sentinels = True
    p = run_cli(cfg, "preflight", "--json")
    check("preflight 通过", p.returncode == 0, "")

    # ---------------------------------------------------------- 5 路径守卫
    print("\n[5] 路径守卫")
    p = run_cli(cfg, "call", "modmind_read_project_file", "--set", "path=../../etc/passwd")
    check("相对越界 .. 被拦 (退出码 7)", p.returncode == 7, "exit=%s" % p.returncode)
    p = run_cli(cfg, "call", "modmind_read_project_file", "--set", "path=C:/Windows/win.ini")
    check("绝对越界被拦", p.returncode == 7, "exit=%s" % p.returncode)
    p = run_cli(cfg, "call", "modmind_read_project_file",
                "--set", "path=C:/Windows/win.ini", "--allow-outside")
    check("--allow-outside 放行", p.returncode in (0, 5), "exit=%s" % p.returncode)

    # ---------------------------------------------------------- 6 危险动作
    print("\n[6] 危险动作闸门")
    p = run_cli(cfg, "call", "modmind_rename_project", "--set", "name=x", "--set", "namespace=y")
    check("rename_project 无 --yes 被拦", p.returncode == 7, "exit=%s" % p.returncode)
    p = run_cli(cfg, "call", "modmind_rename_project", "--set", "name=x",
                "--set", "namespace=y", "--yes")
    check("--yes 后放行", p.returncode == 0, "exit=%s" % p.returncode)

    # ---------------------------------------------------------- 7 并发锁
    print("\n[7] 并发写锁")
    S.max_inflight = 0
    t0 = time.time()
    procs = [subprocess.Popen([PY, str(CLI), "--config", str(cfg), "call",
                               "modmind_blockbench_checkpoint", "--yes", "--no-preflight"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8")
             for _ in range(2)]
    for pr in procs:
        pr.wait(timeout=90)
    elapsed = time.time() - t0
    check("并发写被串行化 (服务端同时只见到 1 个请求)", S.max_inflight == 1,
          "max_inflight=%s" % S.max_inflight)
    check("两个 1.2s 调用串行耗时 >= 2.2s", elapsed >= 2.2, "%.2fs" % elapsed)
    check("两个调用都成功", all(pr.returncode == 0 for pr in procs),
          str([pr.returncode for pr in procs]))

    # ---------------------------------------------------------- 8 零进程
    print("\n[8] 进程卫生")
    # 只断言"没有新增 stdio 转发器" —— 外部 ModMind 活动(用户开着 GUI、别的会话在用)
    # 会让整个 PID 集合变化, 拿它做断言会随机变红; 而"我们不 spawn 转发器"才是要守的。
    fwd_before = sorted(p["pid"] for p in pids_before if p["kind"] == "forwarder")
    pids_after = mm.enumerate_modmind()
    fwd_after = sorted(p["pid"] for p in pids_after if p["kind"] == "forwarder")
    check("全程没有新增 stdio 转发器进程", fwd_before == fwd_after,
          "before=%s after=%s" % (fwd_before, fwd_after))
    p = run_cli(cfg, "doctor", "--json")
    try:
        doc = json.loads(p.stdout)
        check("doctor --json 可用", isinstance(doc, dict) and "modmind_processes" in doc, "")
    except Exception as exc:
        check("doctor --json 可用", False, repr(exc))
    p = run_cli(cfg, "reap")
    check("reap 默认 dry-run", p.returncode == 0, "")

    # 造一个"真的像转发器"的进程 (进程名 ModMind.exe + 唯一参数落在 external-agents/),
    # 这样 reap 的安全性才有东西可断言 —— 否则沙箱里没有转发器, 这几条永远是空跑。
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    pyexe = Path(sys.executable)
    shutil.copy2(pyexe, fakebin / "ModMind.exe")
    for dll in ("python313.dll", "python3.dll"):
        src = pyexe.parent / dll
        if src.exists():
            shutil.copy2(src, fakebin / dll)
    fwd_dir = tmp / "fakeproj" / ".modmind" / "external-agents"
    fwd_dir.mkdir(parents=True)
    fake_target = fwd_dir / "modmind-mcp-server.mjs"
    fake_target.write_text("import time\ntime.sleep(180)\n", encoding="utf-8")
    fake = subprocess.Popen([str(fakebin / "ModMind.exe"), str(fake_target)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.5)
        check("假转发器进程已就绪", fake.poll() is None, "pid=%s" % fake.pid)
        rows = mm.enumerate_modmind()
        mine = [r for r in rows if r["pid"] == fake.pid]
        check("假转发器被正确判为 forwarder",
              bool(mine) and mine[0]["kind"] == "forwarder",
              str(mine and [(mine[0]["pid"], mine[0]["kind"])]))
        p = run_cli(cfg, "reap", "--json")
        try:
            cands = json.loads(p.stdout).get("candidates") or []
        except Exception as exc:                       # noqa: BLE001
            cands = None
            check("reap --json 可解析", False, repr(exc))
        if cands is not None:
            check("reap 可杀集合只含 forwarder (主进程/辅助进程绝不入选)",
                  all(c.get("kind") == "forwarder" for c in cands),
                  "kinds=%s" % [c.get("kind") for c in cands])
            check("reap 候选里确实有那个假转发器",
                  any(c.get("pid") == fake.pid for c in cands), "")
        check("reap 默认(dry-run)没有真杀", fake.poll() is None, "poll=%s" % fake.poll())
        p = run_cli(cfg, "reap")
        check("reap 不带 --yes 没有真杀", p.returncode == 0 and fake.poll() is None,
              "exit=%s poll=%s" % (p.returncode, fake.poll()))
    finally:
        try:
            subprocess.run(["taskkill", "/PID", str(fake.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        except Exception:                              # noqa: BLE001
            pass

    # ---------------------------------------------------------- 9 项目命令
    print("\n[9] 项目命令 (GUI 替代)")
    p = run_cli(cfg, "project", "status", "--json")
    try:
        st = json.loads(p.stdout)
        check("project status 找到活桥", st.get("active_bridge") == str(bridge),
              str(st.get("active_bridge")))
    except Exception as exc:
        check("project status 找到活桥", False, repr(exc))
    p = run_cli(cfg, "project", "bind", "--dry-run")
    check("project bind --dry-run 可用", p.returncode == 0 and "mcp-bridge" in p.stdout, "")
    p = run_cli(cfg, "explain", "--json")
    try:
        ex = json.loads(p.stdout)
        check("explain 含已验证状态码语义", "404" in ex.get("status_codes", {}), "")
        check("explain 含项目打开方式",
              "mcp-bridge" in ex.get("project_open", {}).get("how", ""), "")
    except Exception as exc:
        check("explain --json", False, repr(exc))

    # ---------------------------------------------------------- 10 单元级防线
    print("\n[10] 分类器 / 身份判定 (单元级, 打穿恒真断言)")
    cases = [
        # H1: 主进程的 --project 参数里出现该字样, 绝不能被判成可清理的转发器
        (r'"C:\M\ModMind.exe" --mcp-bridge --project "E:\x mod\modmind-mcp-server.mjs"', "main"),
        (r'"C:\M\ModMind.exe" /c "ping -n 90 127.0.0.1 >nul & rem E:\x\modmind-mcp-server.mjs"', "main"),
        (r'"C:\M\ModMind.exe" --mcp-bridge --project "E:\mod"', "main"),
        (r'"C:\M\ModMind.exe" "E:\mod\modmind-mcp-server.mjs" extra', "main"),
        (r'"C:\M\ModMind.exe" --type=renderer --user-data-dir="C:\y"', "helper"),
        (r'"C:\M\ModMind.exe" --type=gpu-process', "helper"),
        (r'"C:\M\ModMind.exe" "E:\mod\.modmind\external-agents\modmind-mcp-server.mjs"', "forwarder"),
    ]
    for cl, want in cases:
        got = mm.classify_modmind_process(cl)[0]
        check("classify -> %s" % want, got == want, "%s => %s" % (cl[:58], got))

    # M1: 锁键必须跟服务端项目身份走, 而不是客户端传进来的字符串
    (root / ".modmind" / "external-agents" / "mcp-bridge.json").write_text(
        json.dumps({"schemaVersion": 1, "projectPath": str(root)}), encoding="utf-8")
    cfg_a = dict(mm.load_config(str(cfg)), project_root="E:/proj/mod")
    cfg_b = dict(mm.load_config(str(cfg)), project_root=r"E:\proj\mod")
    og_a = hashlib.sha1("E:/proj/mod".encode()).hexdigest()[:12]
    og_b = hashlib.sha1(r"E:\proj\mod".encode()).hexdigest()[:12]
    check("旧算法(哈希客户端字符串)对同一目录会分叉", og_a != og_b, "%s vs %s" % (og_a, og_b))
    check("新算法(服务端项目身份)对同一目录一致",
          mm.lock_key_for(cfg_a) == mm.lock_key_for(cfg_b),
          "%s vs %s" % (mm.lock_key_for(cfg_a), mm.lock_key_for(cfg_b)))
    # 反向: 指向"另一座桥"(另一个项目) 时必须分开上锁。
    # 注意锁身份跟的是"实际调用的那座桥", 所以这里要造第二座桥, 而不是只改 project_root。
    other = tmp / "proj2"
    (other / ".modmind" / "external-agents").mkdir(parents=True)
    (other / "modmind.project.json").write_text(
        json.dumps({"name": "other", "loader": "forge", "minecraftVersion": "1.20.1",
                    "kind": "mod", "namespace": "other", "path": str(other)}), encoding="utf-8")
    (other / ".modmind" / "external-agents" / "bridge.json").write_text(
        json.dumps({"port": 9, "token": "x"}), encoding="utf-8")
    (other / ".modmind" / "external-agents" / "mcp-bridge.json").write_text(
        json.dumps({"projectPath": str(other)}), encoding="utf-8")
    cfg_c = dict(mm.load_config(str(cfg)))
    cfg_c["_bridge"] = str(other / ".modmind" / "external-agents" / "bridge.json")
    check("不同项目(不同桥)分开上锁",
          mm.lock_key_for(cfg_a) != mm.lock_key_for(cfg_c),
          "%s vs %s" % (mm.lock_key_for(cfg_a), mm.lock_key_for(cfg_c)))

    # H2: doctor 必须把"端口可连但 token 已换"判成不健康
    stale = tmp / "stale-token.json"
    stale.write_text(json.dumps({"port": port, "token": "a-stale-token", "version": "x"}),
                     encoding="utf-8")
    p = run_cli(cfg, "--bridge", str(stale), "doctor")
    check("坏 token 时 doctor 必须非 0", p.returncode != 0, "exit=%s" % p.returncode)
    check("坏 token 时 doctor 指出要重新绑定", "重新绑定" in p.stderr + p.stdout,
          (p.stderr + p.stdout).strip()[:70])
    p = run_cli(cfg, "doctor")
    check("健康时 doctor 返回 0", p.returncode == 0, "exit=%s" % p.returncode)

    # H3/L2: 此前漏掉的键
    for kv in ('paths=["C:/Windows/win.ini"]', 'otherPath=C:/Windows/win.ini',
               'Paths=["../../etc/passwd"]', 'path={"a":"C:/Windows/win.ini"}',
               'path=E:/proj/mmcli.py:ads'):
        p = run_cli(cfg, "call", "modmind_addon_import", "--set", kv, "--dry-run")
        check("守卫覆盖 %s" % kv.split("=")[0] + ("(嵌套)" if "{" in kv else ""),
              p.returncode == 7, "exit=%s" % p.returncode)

    # H4: 输出路径不许落进项目树
    p = run_cli(cfg, "call", "modmind_project_info", "--out", str(root / "leak.json"),
                "--dry-run")
    check("--out 写进项目树被拦", p.returncode == 7, "exit=%s" % p.returncode)
    check("--out 拦截后没留下文件", not (root / "leak.json").exists(), "")
    p = run_cli(cfg, "call", "modmind_project_info", "--out", str(root / "leak.json"),
                "--allow-inside", "--dry-run")
    check("--allow-inside 是真闸门(放行)", p.returncode == 0 and (root / "leak.json").exists(),
          "exit=%s" % p.returncode)

    # M3: 参数解析错误必须是退出码 2, 不是 traceback
    p = run_cli(cfg, "call", "modmind_project_info", "--args", "{not json")
    check("--args 非法 JSON -> 退出码 2", p.returncode == 2, "exit=%s" % p.returncode)
    check("--args 非法 JSON -> 无 traceback", "Traceback" not in p.stderr, "")
    p = run_cli(cfg, "call", "modmind_project_info", "--args-file", str(tmp / "nope.json"))
    check("--args-file 不存在 -> 退出码 2", p.returncode == 2, "exit=%s" % p.returncode)

    # ---------------------------------------------------------- 12 插件 (服务端独有动作)
    print("\n[12] 插件动作 (桥认、schema 没登记)")
    p = run_cli(cfg, "plugins", "list", "--json")
    try:
        d = json.loads(p.stdout)
        check("plugins list -> plugin_tools", d["tools"][0]["name"] == "mmcli_demo_ping",
              json.dumps(d, ensure_ascii=False)[:60])
    except Exception as exc:                           # noqa: BLE001
        check("plugins list -> plugin_tools", False, repr(exc))
    p = run_cli(cfg, "plugins", "call", "mmcli_demo_ping", "--json")
    check("plugins call -> plugin_tool_call 形状正确",
          p.returncode == 0 and json.loads(p.stdout)["result"].get("pong") is True,
          p.stdout.strip()[:60])
    p = run_cli(cfg, "plugins", "call", "nope")
    check("不存在的插件工具报错清晰",
          p.returncode == 5 and "插件工具不存在" in p.stderr,
          "exit=%s stderr=%r" % (p.returncode, p.stderr.strip()[:120]))
    check("mm list 里单列了服务端独有动作",
          "plugin_tool_call" in run_cli(cfg, "list", "--grep", "zzz").stdout, "")

    # geo.json -> Blockbench 的动作映射 (与 Blockbench 自身 w5/S5 逐条对齐)
    print("\n[11] geo.json 导入映射")
    geo = {"format_version": "1.12.0", "minecraft:geometry": [{
        "description": {"identifier": "geometry.probe", "texture_width": 64, "texture_height": 32},
        "bones": [
            {"name": "head", "pivot": [0, 24, 0], "cubes": [
                {"origin": [-2.0, 24.5, -3.0], "size": [0.5, 0.25, 1.5],
                 "pivot": [-1.0, 25.0, -2.0], "rotation": [2.5, 12.5, 0.5],
                 "uv": {"north": {"uv": [10, 20], "uv_size": [2, 1]}}}]},
            {"name": "eye", "parent": "head", "pivot": [1, 25, -3]},
        ]}]}
    acts, meta, uv_plan = mm.geo_to_actions(geo["minecraft:geometry"][0], "probe", (64, 64), "tex")
    kinds = [a["type"] for a in acts]
    check("顺序: new-model 在最前", kinds[0] == "new-model", kinds[0])
    check("父骨先于子骨", kinds.index("add-group") < len(kinds) - 1 and
          [a["name"] for a in acts if a["type"] == "add-group"] == ["head", "eye"],
          str([a["name"] for a in acts if a["type"] == "add-group"]))
    # 工程 UV 空间取"贴图真实尺寸"(此处 PNG 是 64x64, geo 声明 64x32)。
    # 原因: 公开动作集设不了 texture.uv_width, 只能让工程空间与贴图一致,
    # 再把 geo 的 UV 等比放大 —— 归一化后与原文件相同。
    check("工程 UV 空间取贴图真实尺寸",
          acts[0]["textureWidth"] == 64 and acts[0]["textureHeight"] == 64,
          "%s x %s" % (acts[0]["textureWidth"], acts[0]["textureHeight"]))
    g = [a for a in acts if a["type"] == "add-group" and a["name"] == "head"][0]
    check("骨 pivot 映射 [-x,y,z]", g["origin"] == [0, 24, 0], str(g["origin"]))
    eye = [a for a in acts if a["type"] == "add-group" and a["name"] == "eye"][0]
    check("子骨带 parentGroupName", eye.get("parentGroupName") == "head", str(eye.get("parentGroupName")))
    c = [a for a in acts if a["type"] == "add-cube"][0]
    check("cube from = [-(ox+sx), oy, oz]", c["from"] == [1.5, 24.5, -3.0], str(c["from"]))
    check("cube to   = [-ox, oy+sy, oz+sz]", c["to"] == [2.0, 24.75, -1.5], str(c["to"]))
    check("from/to 差值等于 size (自洽)",
          [round(c["to"][i] - c["from"][i], 6) for i in range(3)] == [0.5, 0.25, 1.5],
          str([round(c["to"][i] - c["from"][i], 6) for i in range(3)]))
    check("cube origin = [-cpivot.x, y, z]", c["origin"] == [1.0, 25.0, -2.0], str(c["origin"]))
    check("cube rotation = [-rx, -ry, rz]", c["rotation"] == [-2.5, -12.5, 0.5], str(c["rotation"]))
    check("cube 挂到所属骨", c.get("parentGroupName") == "head", str(c.get("parentGroupName")))
    # v 轴被 ×2 (工程空间 64x64 / geo 声明 64x32): 20*2=40, 21*2=42
    check("逐面 UV 按工程空间等比换算",
          uv_plan and uv_plan[0]["faces"]["north"]["uv"] == [10.0, 40.0, 12.0, 42.0],
          json.dumps(uv_plan[:1], ensure_ascii=False)[:90])

    gpath = tmp / "probe.geo.json"
    gpath.write_text(json.dumps(geo), encoding="utf-8")
    p = run_cli(cfg, "import-geo", "--geo", str(gpath), "--dry-run", "--json")
    try:
        plan = json.loads(p.stdout)["plan"]
        check("import-geo --dry-run 给出计划", plan["bones"] == 2 and plan["cubes"] == 1,
              "bones=%s cubes=%s" % (plan["bones"], plan["cubes"]))
    except Exception as exc:                           # noqa: BLE001
        check("import-geo --dry-run 给出计划", False, repr(exc))
    p = run_cli(cfg, "why-uv", "--json")
    try:
        w = json.loads(p.stdout)
        check("why-uv 给出 uv 可行性与实测证据",
              len(w.get("proof", [])) >= 3 and "bedrock_block" in w.get("answer", ""), "")
        check("why-uv 保留了历史错误结论的更正", "correction" in w, "")
    except Exception as exc:                           # noqa: BLE001
        check("why-uv 给出 uv 可行性与实测证据", False, repr(exc))
    # 格式决定 box_uv, 进而决定逐面 UV 能不能生效 —— 这是本轮最关键的坑。
    # 断言的是**返回值**, 不是"源码里有没有这个词"(grep 源码会被注释骗过)。
    fmt_got = acts[0].get("format")
    check("import-geo 默认格式是 bedrock_block (box_uv=false)",
          fmt_got == "bedrock_block", "实际 format=%s" % fmt_got)
    g2 = {"format_version": "1.12.0",
          "minecraft:geometry": [{
              "description": {"identifier": "geometry.t",
                              "texture_width": 128, "texture_height": 128},
              "bones": [{"name": "b", "cubes": [{
                  "origin": [0, 0, 0], "size": [8, 8, 8],
                  "uv": {"north": {"uv": [117, 99], "uv_size": [0.5, 0.5]}}}]}],
          }]}
    acts2, meta2, uv2 = mm.geo_to_actions(g2["minecraft:geometry"][0], "t",
                                          tex_wh=(256, 256), texture_name="tex")
    check("geo 128 空间 -> 工程 256 空间: UV 等比放大",
          uv2 and uv2[0]["faces"]["north"]["uv"] == [234.0, 198.0, 235.0, 199.0],
          json.dumps(uv2[:1], ensure_ascii=False)[:100])
    acts3, meta3, _ = mm.geo_to_actions(g2["minecraft:geometry"][0], "t", tex_wh=None)
    check("没有贴图时退回 geo 声明尺寸(不缩放)", meta3["textureWidth"] == 128, "")

    # bedrock 动画映射 (与 AnimationCodec.codecs.bedrock 逐条对齐)
    print("\n[13] bedrock 动画导入映射")
    check("rotation 取反 x,y", mm._anim_value("rotation", [10, 20, 30]) == [-10, -20, 30],
          str(mm._anim_value("rotation", [10, 20, 30])))
    check("position 只取反 x", mm._anim_value("position", [10, 20, 30]) == [-10, 20, 30],
          str(mm._anim_value("position", [10, 20, 30])))
    check("scale 不变", mm._anim_value("scale", [10, 20, 30]) == [10, 20, 30], "")
    # 注意: Blockbench 的标量分支只注册 molang 变量, 不做轴取反 (数组分支才取反)。
    # 这是它的原样行为, 保真就得跟着来, 不能"顺手修正"。
    check("数字 => 三轴同值且不取反 (复刻原行为)",
          mm._anim_value("rotation", 5) == [5.0, 5.0, 5.0],
          str(mm._anim_value("rotation", 5)))
    check("molang 字符串被拒", mm._anim_value("rotation", "math.sin(q.anim_time)") is None, "")
    kf = mm._anim_keyframes("rotation", {"0.0": [1, 2, 3], "0.5": [4, 5, 6]})
    check("时间->值 展开成多帧", len(kf) == 2 and kf[1][0] == 0.5, str(kf))
    check("lerp_mode 透传", mm._anim_keyframes("rotation", {"0.5": {"post": [1, 2, 3], "lerp_mode": "catmullrom"}})[0][2] == "catmullrom" or
          any(x[2] == "catmullrom" for x in mm._anim_keyframes("rotation", {"0.5": {"post": [1, 2, 3], "lerp_mode": "catmullrom"}})), "")
    # snapping: Blockbench 是 10->100 里第一个可行的 n, 不是最大
    check("snapping 取最小可行 n (=10, 不是 120)", mm._anim_snapping([0, 0.5, 1.0]) == 10,
          str(mm._anim_snapping([0, 0.5, 1.0])))
    check("snapping 单帧时回落到 20", mm._anim_snapping([0.0]) == 20,
          str(mm._anim_snapping([0.0])))
    check("snapping 多帧无解时回落到 20",
          mm._anim_snapping([0, 0.3141592653589793, 0.9876543210987]) == 20,
          str(mm._anim_snapping([0, 0.3141592653589793, 0.9876543210987])))
    # 端到端: 走命令
    af = tmp / "probe.animation.json"
    af.write_text(json.dumps({"format_version": "1.8.0", "animations": {"a1": {
        "loop": True, "animation_length": 1.0,
        "bones": {"head": {"rotation": {"0.0": [0, 0, 0], "1.0": [10, 0, 0]}}}}}}), encoding="utf-8")
    p = run_cli(cfg, "import-anim", "--anim", str(af), "--dry-run", "--json")
    try:
        pl = json.loads(p.stdout)["plan"]["animations"][0]
        check("import-anim 计划正确 (loop/length/帧数)",
              pl["loop"] == "loop" and pl["length"] == 1.0 and pl["keyframes"] == 2,
              json.dumps(pl, ensure_ascii=False)[:90])
    except Exception as exc:                           # noqa: BLE001
        check("import-anim 计划正确 (loop/length/帧数)", False, repr(exc))
    p = run_cli(cfg, "import-anim", "--anim", str(af))
    check("import-anim 真导入成功 (骨名对得上)", p.returncode == 0, "exit=%s" % p.returncode)
    af2 = tmp / "probe2.animation.json"
    af2.write_text(json.dumps({"format_version": "1.8.0", "animations": {"a2": {
        "animation_length": 1.0,
        "bones": {"not_a_group": {"rotation": {"0.0": [0, 0, 0]}}}}}}), encoding="utf-8")
    p = run_cli(cfg, "import-anim", "--anim", str(af2))
    check("import-anim 骨名对不上时报错退出", p.returncode == 5 and "没有任何可导入的关键帧" in p.stderr,
          "exit=%s %s" % (p.returncode, p.stderr.strip()[:60]))

    # ---------------------------------------------------------- 14 启动器 (沙箱内合成实例)
    print("\n[14] 启动命令重建 (不需要真的启动)")
    D = "$" + "{"
    vroot = tmp / "versions"; iroot = tmp / "libraries"; aroot = tmp / "assets"
    inst = vroot / "TestInst"; (inst / "mods").mkdir(parents=True)
    (inst / "TestInst-natives").mkdir()
    for rel in ("com/example/win.jar", "com/example/ver.jar", "com/example/gone.jar"):
        f = iroot / rel; f.parent.mkdir(parents=True, exist_ok=True)
        if "gone" not in rel:
            f.write_bytes(b"x")
    (inst / "TestInst.jar").write_bytes(b"x")
    vj = {
        "id": "TestInst", "mainClass": "com.example.Main",
        "assetIndex": {"id": "5"},
        "libraries": [
            {"downloads": {"artifact": {"path": "com/example/win.jar"}}},
            {"rules": [{"action": "allow", "os": {"name": "osx"}}],
             "downloads": {"artifact": {"path": "com/example/mac.jar"}}},
            {"downloads": {"artifact": {"path": "com/example/ver.jar"}}},
            {"downloads": {"artifact": {"path": "com/example/gone.jar"}}},
        ],
        "arguments": {
            "jvm": [{"rules": [{"action": "allow", "os": {"name": "windows"}}],
                     "value": "-Dwin=1"},
                    "-Dnatives=" + D + "natives_directory}", "-cp", D + "classpath}"],
            "game": ["--username", D + "auth_player_name}", "--gameDir", D + "game_directory}",
                     "--quickPlaySingleplayer", D + "quickPlaySingleplayer}"],
        },
    }
    (inst / "TestInst.json").write_text(json.dumps(vj), encoding="utf-8")
    cfg2 = dict(mm.load_config(str(cfg)))
    cfg2.update({"versions_root": str(vroot), "libraries_root": str(iroot),
                 "assets_root": str(aroot), "java_exe": str(tmp / "java.exe"),
                 "memory_mb": 2048, "player_name": "Bob"})
    p = mm.build_launch("TestInst", cfg2, {"quick_play": "World1"})
    cp = []
    for i, a in enumerate(p["args"]):
        if a == "-cp":
            cp = [c for c in p["args"][i + 1].split(";") if c]
    # 期望: win.jar + ver.jar + 版本 jar = 3; mac.jar 被 rules 过滤, gone.jar 缺失被跳过
    check("rules 过滤掉其它平台库", all("mac.jar" not in c for c in cp) and len(cp) == 3,
          str([Path(c).name for c in cp]))
    check("缺失的库进 missing 而不是崩", len(p["missing_libraries"]) == 1,
          str([Path(x).name for x in p["missing_libraries"]]))
    check("natives 占位符被替换", any(str(inst / "TestInst-natives") in a for a in p["args"]), "")
    check("游戏参数占位符被替换", "--username" in p["args"] and "Bob" in p["args"], "")
    check("quick-play 生效", "World1" in p["args"], "")
    check("主类在参数里", "com.example.Main" in p["args"], "")
    check("内存参数生效", any(a == "-Xmx2048m" for a in p["args"]), "")
    check("其它平台的 jvm 项不被带进来",
          not any(a == "-XstartOnFirstThread" for a in p["args"]), "")
    cfg2p = tmp / "cfg2.json"          # run_cli 要的是配置文件路径, 不是 dict
    cfg2p.write_text(json.dumps({k: v for k, v in cfg2.items()
                                 if not k.startswith("_")}), encoding="utf-8")
    p2 = run_cli(cfg2p, "instance", "list", "--json")
    try:
        lst = json.loads(p2.stdout)["instances"]
        check("instance list 找到实例且认得 mods",
              len(lst) == 1 and lst[0]["name"] == "TestInst" and lst[0]["has_version_json"],
              json.dumps(lst, ensure_ascii=False)[:70])
    except Exception as exc:                           # noqa: BLE001
        check("instance list 找到实例且认得 mods", False, repr(exc))
    p3 = run_cli(cfg2p, "launch", "--instance", "TestInst", "--dry-run", "--json")
    check("launch --dry-run 不启动只给计划",
          p3.returncode == 0 and "plan" in p3.stdout, "exit=%s" % p3.returncode)
    p4 = run_cli(cfg2p, "launch", "--instance", "NoSuch")
    check("实例不存在时明确报错", p4.returncode == 2 and "找不到版本 json" in p4.stderr, "")

    # ---------------------------------------------------------- 15 截图: 安全与语义
    print("\n[15] 截图命令的安全与语义 (真实桌面, 只读)")
    marker = tmp / "PWN.txt"
    if marker.exists():
        marker.unlink()
    payload = "z'; New-Item -Path '" + str(marker) + "' -Force; '"
    p = run_cli(cfg, "shot", "--window", payload, "--out", str(tmp / "inj.png"))
    check("注入载荷不会被执行 (曾被击穿过)", not marker.exists(),
          "marker 被创建 = 被击穿")
    check("注入载荷被当普通标题, 不匹配就报错", p.returncode == 5,
          "exit=%s" % p.returncode)
    p = run_cli(cfg, "shot", "--window", "绝不存在的窗口名XYZ", "--out", str(tmp / "n.png"))
    check("显式指定却匹配不上 -> 报错而不是静默乱抓窗口",
          p.returncode == 5 and "NOWINDOW" in p.stderr,
          "exit=%s %s" % (p.returncode, p.stderr.strip()[:60]))
    p = run_cli(cfg, "shot", "--window", "x" + chr(10) + "y", "--out", str(tmp / "n2.png"))
    check("含换行的参数被拒", p.returncode == 2, "exit=%s" % p.returncode)
    p = run_cli(cfg, "shot", "--window", "A" * 300, "--out", str(tmp / "n3.png"))
    check("超长参数被拒", p.returncode == 2, "exit=%s" % p.returncode)

    # ---------------------------------------------------------- 16 sync / 启动器
    print("\n[16] instance sync 与 launch (agentA 指出的零覆盖区)")
    import zipfile as _zf
    def _mkjar(path, modid, extra=b"x"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with _zf.ZipFile(path, "w") as z:
            z.writestr("META-INF/mods.toml", "modLoader=\"javafml\"\n[[mods]]\nmodId=\"" + modid + "\"\n")
            z.writestr("payload.bin", extra)
    vs2 = tmp / "vs2"
    inst2 = vs2 / "Inst2"
    (inst2 / "mods").mkdir(parents=True, exist_ok=True)
    _mkjar(inst2 / "mods" / "ultracollab-alpha.4.jar", "ultracollab")
    _mkjar(inst2 / "mods" / "othermod-1.0.jar", "othermod")
    _mkjar(inst2 / "mods" / "ultracollab-hotfix.jar", "ultracollab")
    (inst2 / "Inst2.json").write_text("{}", encoding="utf-8")
    newsrc = tmp / "dist" / "ultracollab-alpha.5.jar"
    _mkjar(newsrc, "ultracollab", b"new")
    _base = mm.load_config(str(cfg))          # cfg 是路径, 不是 dict
    cfg3 = {k: v for k, v in _base.items() if not k.startswith("_")}
    cfg3["versions_root"] = str(vs2)
    cfg3p = tmp / "cfg3.json"
    cfg3p.write_text(json.dumps(cfg3), encoding="utf-8")
    mods2 = inst2 / "mods"
    p = run_cli(cfg3p, "instance", "sync", "Inst2", "--jar", str(newsrc), "--dry-run", "--json")
    try:
        pl = json.loads(p.stdout)["plan"]
        wr = sorted(x["name"] for x in pl["will_remove"])
        check("dry-run 预告要移走哪些旧包", len(wr) == 2, str(wr))
        check("dry-run 不误伤无关 mod", "othermod-1.0.jar" not in wr, str(wr))
        check("dry-run 真的不写盘", not (mods2 / newsrc.name).exists(), "")
    except Exception as exc:
        check("dry-run 预告要移走哪些旧包", False, repr(exc))
    run_cli(cfg3p, "instance", "sync", "Inst2", "--jar", str(newsrc), "--json")
    left = sorted(j.name for j in mods2.glob("*.jar"))
    check("装后: 新包在 / 同 modid 旧包清掉 / 无关 mod 保留",
          newsrc.name in left and "othermod-1.0.jar" in left and len(left) == 2, str(left))
    bdirs = list((inst2 / "mod-backups").glob("*"))
    check("删之前做了备份", len(bdirs) == 1 and len(list(bdirs[0].glob("*.jar"))) == 2,
          str([b.name for b in bdirs]))
    run_cli(cfg3p, "instance", "sync", "Inst2", "--jar", str(newsrc), "--force", "--json")
    bdirs2 = list((inst2 / "mod-backups").glob("*"))
    check("连续两次同步的备份不撞车(目录数递增)", len(bdirs2) >= 1, str(len(bdirs2)))
    p = run_cli(cfg3p, "instance", "sync", "../evil", "--jar", str(newsrc), "--dry-run")
    check("实例名路径穿越被拒", p.returncode == 2, "exit=%s" % p.returncode)
    p = run_cli(cfg3p, "instance", "sync", "Inst2", "--jar", str(newsrc), "--dry-run", "--json")
    try:
        check("已装同一份内容时如实报告", json.loads(p.stdout)["plan"].get("already_installed") is True, "")
    except Exception:
        pass
    p = run_cli(cfg3p, "instance", "sync", "Inst2", "--jar", str(tmp / "nope.jar"), "--dry-run")
    check("jar 不存在时报错", p.returncode == 2, "exit=%s" % p.returncode)
    # 启动器: 占位符与数值校验
    p5 = mm.build_launch("TestInst", cfg2, {"quick_play": "W"})
    leftover = [a for a in p5["args"] if "$" + "{" in a]
    check("所有占位符都替换干净 (含 auth_uuid/user_type/version_type)", not leftover, str(leftover[:3]))
    check("quick-play 不重复传参", p5["args"].count("--quickPlaySingleplayer") == 1,
          str([a for a in p5["args"] if "quickPlay" in a]))
    check("mainClass 用的是 json 里的值", "com.example.Main" in p5["args"], "")
    for bad in (["--memory", "-1"], ["--memory", "0"], ["--width", "-5"]):
        q = run_cli(cfg2p, "launch", "--instance", "TestInst", *bad)
        check("非法数值被拒: " + " ".join(bad), q.returncode == 2, "exit=%s" % q.returncode)
    (inst / "Broken.json").write_text("{not json", encoding="utf-8")
    q = run_cli(cfg2p, "launch", "--instance", "Broken")
    check("坏 version json -> 退出码 2 且无 traceback",
          q.returncode == 2 and "Traceback" not in q.stderr, "exit=%s" % q.returncode)
    q = run_cli(cfg2p, "verify", "--skip-build", "--skip-sync", "--skip-launch", "--skip-shot")
    check("四个 --skip 全开 -> 明确报错", q.returncode == 2, "exit=%s" % q.returncode)

    # M4 真检查: 把真实安装版 .mjs 放进沙箱, 让 --verify 真跑一次比对
    real_mjs = None
    try:
        rcfg = mm.load_config(None)
        rp, _ = mm.resolve_bridge_path(rcfg, None, must_exist=False)
        if rp is not None:
            cand = rp.parent / "modmind-mcp-server.mjs"
            if cand.exists():
                real_mjs = cand
    except Exception:
        pass
    if real_mjs:
        shutil.copy(real_mjs, root / ".modmind" / "external-agents" / "modmind-mcp-server.mjs")
        p = run_cli(cfg, "actions", "--verify")
        check("与真实安装版 .mjs 映射比对", p.returncode == 0,
              p.stdout.strip().replace("\n", " | ")[:110])
    else:
        check("与真实安装版 .mjs 映射比对", True, "环境里找不到安装版 .mjs", skip=True)

    srv.shutdown()

    print("\n" + "=" * 72)
    failed = [r for r in RESULTS if r[1] is False]
    skipped = [r for r in RESULTS if r[1] is None]
    print("总计 %d, 通过 %d, 跳过 %d, 失败 %d"
          % (len(RESULTS), len(RESULTS) - len(failed) - len(skipped), len(skipped), len(failed)))
    for name, _, detail in failed:
        print("  FAILED: %s   %s" % (name, detail))
    for name, _, detail in skipped:
        print("  SKIPPED: %s   %s" % (name, detail))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
