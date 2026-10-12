#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mmcli - ModMind 直连 CLI (HTTP /tool, 默认零子进程)

设计目标 (对应 MODMIND_CLI_HANDOFF.md 的待办与踩坑):

  1. 零进程泄漏    调用路径默认走 HTTP 直连 http://127.0.0.1:<port>/tool, 不 spawn ModMind.exe。
                   (例外不遮掩: doctor / reap 是诊断命令, 会起一次 powershell 枚举进程,
                    必要时 taskkill; 这不属于"每次调用一个进程"的泄漏。)
  0/1/2/3/4/5/6/7 = 成功/内部错误/用法/无桥/绑定不符/工具错/抢锁超时/守卫拦截
                   stdio 转发器是"每次调用起一个 Electron 进程"的元凶, 本 CLI 默认永不使用它;
                   只有显式给 --transport stdio --allow-stdio 才会走, 且强制 kill 进程树 + 审计留痕。
  2. 零项目污染    锁 / 缓存 / 日志 / 审计全部写在 state_dir (%LOCALAPPDATA%/mmcli),
                   绝不往项目树里写任何东西; 输出文件默认落在项目外, 想写进项目要显式 --allow-inside。
  3. 绑错项目免谈  任何写类动作前自动 preflight: 校验 namespace + 哨兵文件,
                   防止再次出现"对着脚手架空壳构建出 1708 字节 jar"。
  4. 并发不打架    写类动作走跨进程排他锁; 读类动作默认不加锁。
  5. 路径不越界    所有 path/relativePath/projectPath/outputDirectory/targetPath 参数做越界校验。

协议真相 (实测抓包, 见 --explain):
    POST http://127.0.0.1:<port>/tool
    headers: content-type: application/json
             x-modmind-token: <bridge.json 里的 token>
    body:    {"action": "<动作名>", "input": {...}}
    注意: body 用的不是 MCP 工具名, 而是映射表里的动作名; 且必须带 token 头。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import zipfile
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.1.0"

# ---------------------------------------------------------------- 退出码
EX_OK = 0
EX_USAGE = 2          # 参数/配置错误
EX_NO_BRIDGE = 3      # ModMind 没打开项目 -> bridge.json 缺失或端口不通
EX_BINDING = 4        # 项目绑定不符 (preflight 失败)
EX_TOOL = 5           # 工具返回错误 / HTTP 非 2xx
EX_LOCK = 6           # 抢锁超时
EX_GUARD = 7          # 守卫拦截 (路径越界 / 危险动作未确认 / 输出落进项目树)
EX_INTERNAL = 1       # 意外内部错误 (带 traceback; 属 bug, 请连 stderr 一起上报)

# ---------------------------------------------------------------- 工具表
# 权威来源: 安装版 <project>/.modmind/external-agents/modmind-mcp-server.mjs
# 格式: MCP工具名|动作名(HTTP body 里的 action)|读写级别
# 级别: read(只读本地) readremote(只读联网) safe(可重复的轻写) managed(真写) unknown
# 校验: 与 audit/tool_names.json 的 98 个名字集合完全一致 (mmcli actions --verify 可复验)
TOOL_TABLE = """
modmind_addon_import|addon_import|managed
modmind_addon_link_project|addon_link_project|managed
modmind_addon_prepare|addon_prepare|managed
modmind_addon_relationships|addon_relationships|read
modmind_apply_edits|apply_edits|managed
modmind_asset_apply_advanced|asset_apply_advanced|managed
modmind_asset_apply_intent|asset_apply_intent|managed
modmind_asset_apply_reference|asset_apply_reference|managed
modmind_asset_apply_refinement|asset_apply_refinement|managed
modmind_asset_compile_advanced|asset_compile_advanced|read
modmind_asset_compile_intent|asset_compile_intent|read
modmind_asset_compile_reference|asset_compile_reference|read
modmind_asset_compile_refinement|asset_compile_refinement|read
modmind_asset_preview_advanced|asset_preview_advanced|read
modmind_asset_preview_intent|asset_preview_intent|read
modmind_asset_preview_reference|asset_preview_reference|read
modmind_asset_preview_refinement|asset_preview_refinement|read
modmind_asset_visual_review|asset_visual_review|read
modmind_blockbench_actions|blockbench_actions|managed
modmind_blockbench_capture_views|blockbench_capture_views|read
modmind_blockbench_checkpoint|blockbench_checkpoint|safe
modmind_blockbench_history|blockbench_history|read
modmind_blockbench_project_state|blockbench_project_state|read
modmind_blockbench_restore_history|blockbench_restore_history|managed
modmind_blockbench_validate|blockbench_validate|read
modmind_build_project|build_project|managed
modmind_creation_context|creation_context|managed
modmind_dependency_install|dependency_install|managed
modmind_dependency_search|dependency_search|readremote
modmind_get_app_settings|get_app_settings|read
modmind_image_generate|image_generate|managed
modmind_image_perfect_pixel|image_perfect_pixel|managed
modmind_image_project_assets|image_project_assets|read
modmind_image_read_project_asset|image_read_project_asset|read
modmind_image_remove_background|image_remove_background|managed
modmind_image_studio_info|image_studio_info|readremote
modmind_list_project_directory|list_project_directory|read
modmind_local_server|server_operation|managed
modmind_mapping_class|mappings_class|readremote
modmind_mapping_search|mappings_search|readremote
modmind_maven_dependency_install|maven_dependency_install|managed
modmind_mcmod_files|mcmod_files|readremote
modmind_mcmod_search|mcmod_search|readremote
modmind_mcp_probe|mcp_probe|read
modmind_model_image_capability|model_image_capability|unknown
modmind_modpack_apply_keybinds|modpack_apply_keybinds|managed
modmind_modpack_apply_optimization_profile|modpack_apply_optimization_profile|managed
modmind_modpack_apply_plan|modpack_apply_plan|managed
modmind_modpack_build_server|modpack_build_server|managed
modmind_modpack_delegate_module|modpack_delegate_module|managed
modmind_modpack_download_content|modpack_download_content|managed
modmind_modpack_migration_apply|modpack_migration_apply|managed
modmind_modpack_migration_history|modpack_migration_history|read
modmind_modpack_migration_preview|modpack_migration_preview|readremote
modmind_modpack_migration_targets|modpack_migration_targets|read
modmind_modpack_migration_undo|modpack_migration_undo|managed
modmind_modpack_module_task|modpack_module_task|read
modmind_modpack_modules|modpack_modules|read
modmind_modpack_plan|modpack_plan|managed
modmind_modpack_run_server_scenario|modpack_run_server_scenario|managed
modmind_modpack_verify_server_join|modpack_verify_server_join|managed
modmind_modpack_write_ftb_quest|modpack_write_ftb_quest|managed
modmind_modpack_write_patchouli_book|modpack_write_patchouli_book|managed
modmind_plugins_read_source|plugin_read_source|read
modmind_plugins_reload|plugin_reload|safe
modmind_plugins_scaffold|plugin_scaffold|managed
modmind_plugins_write_files|plugin_write_files|managed
modmind_probe_java_home|probe_java_home|read
modmind_project_files|project_files|read
modmind_project_info|project_info|read
modmind_project_knowledge_read|project_knowledge_read|read
modmind_project_knowledge_save|project_knowledge_save|safe
modmind_project_search|project_search|read
modmind_project_setup|project_setup|managed
modmind_read_document|read_document|read
modmind_read_project_file|read_project_file|read
modmind_release_preflight|release_preflight|read
modmind_rename_project|rename_project|managed
modmind_research|research|read
modmind_resource_pack|resource_pack_operation|managed
modmind_runtime_state|runtime_state|read
modmind_scan_java_homes|scan_java_homes|read
modmind_set_app_setting|set_app_setting|managed
modmind_set_intent|set_intent|safe
modmind_sound_create|sound_create|managed
modmind_sound_library|sound_read|read
modmind_test_action|test_action|managed
modmind_test_capture|test_capture|managed
modmind_test_matrix|test_matrix|managed
modmind_test_minecraft|test_minecraft|managed
modmind_test_observe|test_observe|managed
modmind_test_rendered|test_rendered|managed
modmind_test_scenario|test_scenario|managed
modmind_test_session|test_session|managed
modmind_update_todo|update_todo|safe
modmind_validate_content|content_validate|read
modmind_web_read|web_read|readremote
modmind_web_search|web_search|readremote
"""


def _load_table():
    by_name, by_action = {}, {}
    for line in TOOL_TABLE.strip().splitlines():
        name, action, level = line.split("|")
        by_name[name] = {"name": name, "action": action, "level": level}
        by_action[action] = name
    return by_name, by_action


TOOLS, ACTION_TO_NAME = _load_table()

READ_LEVELS = {"read", "readremote"}

# 桥认、但 MCP schema 里没有登记的动作。
# 来源: 主进程 switch(body.action) 的 94 个 action + handlePluginAction 的 6 个 plugin_ action
# = 100 个; 而 schema 只登记了 98 个。差的就是这两个整动作。
# (由独立审计实测确认: plugin_tools → {"tools":[]}; plugin_tool_call → "插件工具不存在：x",
#  后者说明确实进了 handler 而不是被当未知工具拒绝。)
SERVER_ONLY_ACTIONS = {
    "plugin_tools": "列出已安装插件注册的工具",
    "plugin_tool_call": "调用某个插件工具 (需要 input={tool, input})",
}

# 少数 managed 工具在特定子操作下其实是只读的 (避免无谓抢锁)
SUBOP_READONLY = {
    "test_session": {"capabilities", "state"},
    "test_observe": {"*"},
    "test_capture": {"*"},
    "server_operation": {"state"},
    "resource_pack_operation": {"list", "validate"},
    "modpack_modules": {"list", "info", "files", "read", "search"},
}

# 需要 --yes 才放行的不可逆/全局动作
DANGEROUS = {
    "rename_project", "project_setup", "set_app_setting", "plugins_write_files",
    "modpack_migration_apply", "modpack_migration_undo", "addon_link_project",
    "modpack_apply_plan", "modpack_download_content", "dependency_install",
    "maven_dependency_install", "blockbench_restore_history",
}

# 服务端 AI Review 闸门: 这些动作会先过 ModMind 自己的 reviewAction,
# 被拒时返回 400 + "AI Review denied ..." —— 那是明确的策略拒绝, 不要重试、不要换法绕过。
# (来源: app.asar/out/main/index.js 的 REVIEWED_ACTIONS)
REVIEWED_ACTIONS = {
    "test_rendered", "modpack_delegate_module", "resource_pack_operation",
    "server_operation", "sound_create", "rename_project", "apply_edits",
    "dependency_install", "maven_dependency_install", "addon_prepare",
    "addon_import", "addon_link_project", "test_matrix", "build_project",
    "test_minecraft", "modpack_apply_plan", "modpack_download_content",
    "modpack_write_ftb_quest", "modpack_write_patchouli_book",
    "modpack_apply_keybinds", "modpack_build_server",
    "modpack_verify_server_join", "modpack_apply_optimization_profile",
    "modpack_run_server_scenario", "blockbench_actions", "blockbench_checkpoint",
    "blockbench_restore_history", "asset_apply_intent", "asset_apply_refinement",
    "asset_apply_advanced", "asset_apply_reference", "image_generate",
    "image_perfect_pixel", "image_remove_background",
}

# 参与越界校验的参数名 (全部小写; 比较时统一 lower)
# 覆盖 addon_import.paths / research.otherPath 这类此前漏掉的键。
PATH_KEYS = {
    "path", "paths", "relativepath", "relativepaths", "projectpath", "outputdir",
    "outputdirectory", "targetpath", "otherpath", "sourcepath", "file", "files",
    "dir", "directory", "directories", "folder", "folders", "location", "locations",
}
# 这些参数即使绝对路径也允许落在 allowed_roots 之外 (显式外部引用), 仅告警
PATH_WARN_ONLY = {"projectpath"}

# ---------------------------------------------------------------- 配置
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "mmcli.config.json"

# Everything machine-specific is empty by default: the tool must be useful on a fresh
# checkout. "mmcli init" writes this out as a starting point, and every command that
# needs a value says which key to set when it is missing.
DEFAULT_CONFIG = {
    # Bridge / ModMind
    "bridge_json": None,       # usually auto-discovered under project_root
    "modmind_exe": None,       # auto-detected from the usual install locations
    "project_root": None,      # the project ModMind is bound to
    # Guard rails (both are opt-IN; empty means "do not check")
    "allowed_roots": [],
    "expect_namespace": None,
    "require_files": [],
    # Behaviour
    "timeout": 300,
    "lock_timeout": 180,
    "bind_wait_seconds": 45,
    "memory_mb": 4096,
    "player_name": "Player",
    # Output locations (None = built-in defaults under state_dir)
    "state_dir": None,
    "output_dir": None,
    "manifest_template": None,
    # Game instance launching (only needed for launch / instance / verify)
    "versions_root": None,
    "libraries_root": None,
    "assets_root": None,
    "pcl_dir": None,
    "java_exe": None,
    "default_instance": None,
    "build_libs": None,
}


def _expand(p):
    return Path(str(p)).expanduser() if p else None


def cfg_path(cfg, key):
    """Read a path out of the config, or None when it is not set.

    Never returns Path("None") -- an unset key must stay unset, so callers can tell
    "not configured" apart from "configured to a nonsense path".
    """
    value = cfg.get(key)
    return Path(str(value)) if value else None


def require_cfg_path(cfg, key, why):
    """Like cfg_path, but explains how to fix it when the key is missing."""
    path = cfg_path(cfg, key)
    if path is None:
        die(EX_USAGE,
            "配置里没有 %s —— %s" % (key, why),
            "  跑 mmcli init 生成配置模板, 或用 --config <文件> 指定。")
    return path


def default_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "mmcli"
    return Path.home() / ".local" / "state" / "mmcli"


def load_config(explicit: str | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = Path(explicit) if explicit else DEFAULT_CONFIG_PATH
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            die(EX_USAGE, "配置文件无法解析: %s (%s)" % (path, exc))
        if not isinstance(user, dict):
            die(EX_USAGE, "配置文件必须是 JSON 对象: %s" % path)
        cfg.update(user)
    cfg["_config_path"] = str(path)
    cfg["_config_exists"] = path.exists()
    cfg["state_dir"] = str(_expand(cfg.get("state_dir")) or default_state_dir())
    if cfg.get("output_dir"):
        cfg["output_dir"] = str(_expand(cfg["output_dir"]))
    return cfg


def die(code: int, *msg):
    print(*msg, file=sys.stderr)
    raise SystemExit(code)


def _explain_tool_error(detail: str) -> str:
    """把服务端 400 文本翻译成"该干什么"。这些都是策略/环境问题, 不是可以重试的抖动。"""
    hints = []
    if "AI Review denied" in detail:
        hints.append("  -> ModMind 的安全审查明确拒绝了该动作。这是策略拒绝,"
                     " 不要重试、不要换等价动作绕过; 把未完成的工作报给用户。")
    if "[USER_DISABLED_TOOL]" in detail:
        hints.append("  -> 该功能被用户在本轮对话里关掉了, 需要用户勾选后重发, 不要自行绕过。")
    if "未知 ModMind 工具" in detail:
        hints.append("  -> 动作名服务端不认识。核对 mmcli list; 也可能是这个 ModMind 版本"
                     " 比工具表旧 (跑 mmcli describe 从安装版 .mjs 复验)。")
    if "只读灵感台禁止" in detail:
        hints.append("  -> 当前桥是只读灵感台模式, 只能用读取类动作。")
    if "项目知识服务不可用" in detail or "材料分析服务不可用" in detail:
        hints.append("  -> 该能力依赖 ModMind 侧的服务绑定, 与项目绑定无关, 属结构性不可用。")
    if "当前项目不是整合包" in detail:
        hints.append("  -> modpack_* 只对整合包项目有效; 当前是单 mod 项目。")
    if "No Blockbench project is open" in detail:
        hints.append("  -> Blockbench 里没有打开的工程。blockbench_* 都要求先有工程;"
                     " 无头做法是调 blockbench_actions 的 new-model 动作先建一个:"
                     " mmcli call modmind_blockbench_actions --yes --args "
                     "'{\"actions\":[{\"type\":\"new-model\",\"format\":\"modded_entity\","
                     "\"name\":\"probe\",\"textureWidth\":64,\"textureHeight\":64}]}'")
    if "无效" in detail or "必须" in detail or "缺少" in detail:
        hints.append("  -> 参数不合 schema, 用 mmcli describe <工具名> 看定义。")
    return detail + ("\n" + "\n".join(hints) if hints else "")


def _port_listening(port, timeout=1.2):
    if not port:
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def bridge_candidates(cfg):
    """按优先级列出所有可能的 bridge.json 位置。

    为什么不能写死: 一旦用 --project 重新绑定, ModMind 会删掉旧项目的 bridge.json,
    在新项目下重开一个。写死路径的脚本会在换绑那一刻集体失效 (上一轮就是这么废掉的)。
    """
    cands = []

    def add(path, why):
        if not path:
            return
        p = Path(str(path))
        if all(p != c[0] for c in cands):
            cands.append((p, why))

    add(cfg.get("_bridge"), "--bridge 指定")
    add(cfg.get("bridge_json"), "config.bridge_json")
    root = Path(str(cfg.get("project_root") or ""))
    if str(root) not in ("", "."):
        ext = root / ".modmind" / "external-agents"
        status = ext / "mcp-bridge.json"
        if status.exists():
            try:
                add(json.loads(status.read_text(encoding="utf-8")).get("bridgeConfigPath"),
                    "mcp-bridge.json 状态文件")
            except Exception:                          # noqa: BLE001
                pass
        add(ext / "bridge.json", "project_root/.modmind")
    for r in cfg.get("allowed_roots", []) or []:
        rr = Path(str(r))
        if not rr.exists():
            continue
        for pat in ("*/.modmind/external-agents/bridge.json",
                    "*/*/.modmind/external-agents/bridge.json"):
            try:
                for f in sorted(rr.glob(pat)):
                    add(f, "扫描 " + str(rr))
            except OSError:
                pass
    return cands


def resolve_bridge_path(cfg, out=None, must_exist=True):
    cands = bridge_candidates(cfg)
    explicit = cfg.get("_bridge")
    if explicit:
        # 显式指定就认它, 不回退 —— 否则排障时会被引到另一个项目的活桥上
        p = Path(str(explicit))
        if not p.exists():
            if not must_exist:
                return None, cands
            die(EX_NO_BRIDGE, "指定的 bridge.json 不存在: %s" % p)
        if out:
            out.debug("bridge.json = %s (--bridge 显式指定)" % p)
        return p, cands
    live, existing = [], []
    for p, why in cands:
        if not p.exists():
            continue
        existing.append((p, why))
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        if _port_listening(data.get("port")):
            live.append((p, why))
    if live:
        chosen = live[0]
    elif existing:
        chosen = existing[0]
    else:
        chosen = None
    if chosen is None:
        if not must_exist:
            return None, cands
        die(EX_NO_BRIDGE,
            "找不到任何 bridge.json。已尝试:",
            *["  - %s  (%s)" % (p, w) for p, w in cands],
            "",
            "bridge.json 只由 ModMind 在「为某个项目启动 MCP 桥」时生成。",
            "现在没有在跑, 所以所有 stdio/HTTP 调用都会失败 —— 这不是工具损坏。",
            "",
            "两条路:",
            "  A) 图形界面: 打开 ModMind, 打开项目",
            "  B) 命令行:   mmcli project bind <项目根路径>   (官方 --mcp-bridge --project 开关)",
            "",
            "先跑 mmcli doctor 看现状。")
    if out:
        out.debug("bridge.json = %s (%s)" % (chosen[0], chosen[1]))
        for p, why in existing[1:]:
            out.debug("另有候选: %s (%s)" % (p, why))
    return chosen[0], cands


# ---------------------------------------------------------------- 输出
class Out:
    def __init__(self, as_json=False, quiet=False, verbose=False, max_chars=4000):
        self.as_json = as_json
        self.quiet = quiet
        self.verbose = verbose
        self.max_chars = max_chars

    def info(self, *msg):
        if not self.quiet:
            print(*msg)

    def debug(self, *msg):
        if self.verbose and not self.quiet:
            print("[debug]", *msg, file=sys.stderr)

    def json(self, obj):
        print(json.dumps(obj, ensure_ascii=False, indent=1))

    def result(self, obj, text=None):
        """统一收尾: --json 时输出结构化对象, 否则输出人类可读文本。"""
        if self.as_json:
            self.json(obj if not isinstance(obj, str) else {"text": obj})
        elif text is not None:
            print(text)
        else:
            self.json(obj)


def _truncate(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit] + "\n... [截断, 共 %d 字符; 用 --raw 或 --out 取全文]" % len(text)
    return text


# ---------------------------------------------------------------- 锁
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class FileLock:
    """跨进程排他锁。锁文件写在 state_dir, 不碰项目树。"""

    def __init__(self, state_dir: Path, key: str, timeout: float, enabled: bool = True,
                 stale_after: float = 3600.0):
        self.path = state_dir / "locks" / (key + ".lock")
        self.timeout = timeout
        self.enabled = enabled
        self.stale_after = stale_after
        self.acquired = False
        self.token = os.urandom(8).hex()
        # 刚 O_EXCL 创建、还没写进 payload 的那一瞬间, 别人读到的是空文件。
        # 必须给一个宽限期, 否则会被误判成"陈旧锁"而被删掉。
        self.empty_grace = 10.0

    def __enter__(self):
        if not self.enabled:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = {"pid": os.getpid(), "token": self.token,
                           "host": socket.gethostname(), "started": time.time(),
                           "argv": sys.argv[1:6]}
                os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                os.fsync(fd)
                os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                owner = self._read_owner()
                if owner is None:
                    # 读不出内容: 可能是别人刚创建还没写完 —— 只有明显过期才敢动
                    try:
                        age = time.time() - self.path.stat().st_mtime
                    except OSError:
                        age = 0.0
                    stale = age > self.empty_grace
                else:
                    stale = (not _pid_alive(int(owner.get("pid", -1)))) \
                        or (time.time() - float(owner.get("started", 0)) > self.stale_after)
                if stale:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    raise TimeoutError(
                        "等锁超时 %.0fs: %s 被 pid=%s 占用 (先跑 mmcli lock 看状态, "
                        "确认对方已死后 mmcli lock --force-unlock)" % (
                            self.timeout, self.path.name, owner.get("pid")))
                time.sleep(0.25)

    def _read_owner(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:                             # noqa: BLE001
            return None

    def __exit__(self, *exc):
        if self.acquired:
            # 只删"确实是自己持有的"那把锁: 如果期间被别人判陈旧抢走并重建,
            # 无条件 unlink 会删掉别人的锁, 引发连锁。
            owner = self._read_owner()
            if owner is None or owner.get("token") == self.token:
                try:
                    self.path.unlink()
                except OSError:
                    pass
            self.acquired = False
        return False


# ---------------------------------------------------------------- 传输层
class Bridge:
    """HTTP 直连。默认不 spawn 任何进程。"""

    def __init__(self, cfg: dict, out: Out, transport="http", allow_stdio=False,
                 timeout=None):
        self.cfg = cfg
        self.out = out
        self.transport = transport
        self.allow_stdio = allow_stdio
        self.timeout = float(timeout if timeout is not None else cfg.get("timeout", 300))
        self._bridge_path = None
        self._cfg_cache = None
        self.spawned = []

    @property
    def bridge_path(self) -> Path:
        if self._bridge_path is None:
            self._bridge_path, _ = resolve_bridge_path(self.cfg, self.out)
        return self._bridge_path

    # -------- bridge.json
    def bridge_info(self, required=True):
        if self._cfg_cache is not None:
            return self._cfg_cache
        if not required:
            p, _ = resolve_bridge_path(self.cfg, self.out, must_exist=False)
            if p is None:
                return None
        try:
            data = json.loads(self.bridge_path.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            die(EX_NO_BRIDGE, "bridge.json 无法解析: %s (%s)" % (self.bridge_path, exc))
        for key in ("port", "token"):
            if not data.get(key):
                die(EX_NO_BRIDGE, "bridge.json 缺少 %s 字段: %s" % (key, self.bridge_path))
        self._cfg_cache = data
        return data

    def port_open(self, timeout=1.5) -> bool:
        info = self.bridge_info(required=False)
        if not info:
            return False
        try:
            with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=timeout):
                return True
        except OSError:
            return False

    # -------- 调用
    def build_request(self, action, inputs):
        info = self.bridge_info()
        body = json.dumps({"action": action, "input": inputs or {}},
                          ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "x-modmind-token": info["token"]}
        return "http://127.0.0.1:%s/tool" % info["port"], headers, body

    def call(self, action, inputs=None, dry_run=False):
        url, headers, body = self.build_request(action, inputs)
        if dry_run:
            return {"_dry_run": True, "url": url,
                    "headers": {k: ("<token>" if k == "x-modmind-token" else v)
                                for k, v in headers.items()},
                    "body": json.loads(body.decode("utf-8"))}
        if self.transport == "stdio":
            if not self.allow_stdio:
                die(EX_USAGE, "--transport stdio 需要同时给 --allow-stdio (它会产生子进程)")
            return self._stdio_call(action, inputs)
        try:
            req = urllib.request.Request(url, body, headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            if exc.code == 404:
                # ModMind 源码: method/url/token 任一不符 -> 404 "Not found"
                die(EX_NO_BRIDGE,
                    "ModMind 拒绝了请求 (HTTP 404 Not found)。",
                    "  服务端判定式: method=='POST' && url=='/tool' && header[x-modmind-token]==token,",
                    "  任何一项不符都返回 404, 所以这几乎一定是 token 过期 ——",
                    "  即 bridge.json 是上一次会话的残留, 而服务端已经换了新的 token。",
                    "  处置: mmcli doctor 看端口是否还在, 然后 mmcli project bind 重新绑定。")
            die(EX_TOOL, "HTTP %s\n%s" % (exc.code, _explain_tool_error(detail)))
        except urllib.error.URLError as exc:
            die(EX_NO_BRIDGE,
                "连不上 ModMind HTTP 服务: %s\n  %s\n"
                "  bridge.json 说端口是 %s, 但连不上 —— 大概率是 ModMind 侧项目已关闭,"
                " 或 bridge.json 是上一次会话的残留。跑 mmcli doctor。" % (
                    url, exc.reason, self.bridge_info().get("port")))
        if not raw.strip():
            die(EX_TOOL, "ModMind 返回空响应 (%s)" % url)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw_text": raw}

    # -------- stdio 兜底 (唯一会 spawn 进程的路径)
    def _stdio_call(self, action, inputs):
        server = self.bridge_path.parent / "modmind-mcp-server.mjs"
        if not server.exists():
            die(EX_USAGE, "stdio 模式需要转发器: %s" % server)
        exe = str(require_cfg_path(self.cfg, "modmind_exe",
                                   "stdio 模式需要 ModMind.exe 的完整路径"))
        payload = "\n".join([
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05",'
            '"capabilities":{},"clientInfo":{"name":"mmcli","version":"%s"}}}' % VERSION,
            '{"jsonrpc":"2.0","method":"notifications/initialized"}',
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": _action_to_mcp(action), "arguments": inputs or {}}},
                       ensure_ascii=False),
        ]) + "\n"
        env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
        flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
        self.out.debug("stdio spawn:", exe, server)
        proc = subprocess.Popen([exe, str(server)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, text=True, encoding="utf-8",
                                errors="replace", creationflags=flags)
        self._audit_spawn(proc.pid, "start")
        stdout = stderr = ""
        try:
            stdout, stderr = proc.communicate(payload, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            stderr = "timeout after %ss" % self.timeout
        finally:
            self._kill_tree(proc.pid, proc)
        lines = [l for l in (stdout or "").splitlines() if l.strip()]
        if not lines:
            die(EX_TOOL, "stdio 无响应; stderr: %s" % (stderr or "")[:400])
        resp = json.loads(lines[-1])
        res = resp.get("result", {})
        if res.get("isError"):
            die(EX_TOOL, "".join(c.get("text", "") for c in res.get("content", []))[:800])
        text = "".join(c.get("text", "") for c in res.get("content", []))
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw_text": text}

    def _kill_tree(self, pid, proc):
        """确保进程死透; 死不透就记录到审计文件, 由 mmcli reap 处理。"""
        try:
            proc.wait(timeout=3)
            return
        except Exception:                             # noqa: BLE001
            pass
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:                             # noqa: BLE001
            pass
        # 无条件收整棵树: 只检查直接子进程是不够的 —— 转发器派生的孙进程
        # (Electron helper 常常会脱离父进程) 会活下来, 而且不留任何痕迹。
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        if _pid_alive(pid):
            self._audit_spawn(pid, "leaked")
            self.out.debug("警告: pid %s 未能回收, 已记录; 跑 mmcli reap" % pid)
        else:
            self._audit_spawn(pid, "killed-tree")

    def _audit_spawn(self, pid, event):
        try:
            d = Path(self.cfg["state_dir"]) / "spawns"
            d.mkdir(parents=True, exist_ok=True)
            with (d / "spawns.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "pid": pid, "event": event,
                                     "argv": sys.argv[1:6]}, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _action_to_mcp(action: str) -> str:
    return ACTION_TO_NAME.get(action, action)


# ---------------------------------------------------------------- 守卫
def _norm(p: str) -> Path:
    return Path(str(p).replace("\\", "/"))


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def guard_paths(args, cfg, out: Out, allow_outside=False, allow_inside=False,
                project_root: Path | None = None):
    """递归校验参数里的文件路径, 防止写到项目外/项目内不该写的地方。"""
    roots = [_norm(r) for r in cfg.get("allowed_roots", [])]
    hits = []

    def visit(node, ctx=None):
        """ctx = 当前所处的"路径语义"键名。

        关键点: 路径上下文会向下继承 —— 这样 path={"a":"C:/x"} 这种
        "把路径藏在别的键名里"的写法也逃不掉; 而 3D 坐标数组
        (asset 程序里 path=[[x,y,z],...]) 因为元素是数字而不是字符串, 天然被跳过。
        """
        if isinstance(node, dict):
            for k, v in node.items():
                kk = str(k).lower()
                visit(v, kk if kk in PATH_KEYS else ctx)
        elif isinstance(node, list):
            for v in node:
                visit(v, ctx)
        elif isinstance(node, str) and ctx:
            _check(node, ctx)

    def _check(raw, key):
        p = raw.strip()
        if not p or p.startswith(("http://", "https://", "data:")):
            return
        # NTFS 备用数据流 (file.txt:stream) 只有盘符位置允许出现冒号
        tail = p[2:] if len(p) > 1 and p[1] == ":" else p
        if ":" in tail:
            hits.append("%s=%s 含非法冒号 (NTFS ADS?)" % (key, p))
            return
        candidate = Path(p.replace("\\", "/"))
        if candidate.is_absolute():
            if roots and not any(_under(candidate, r) for r in roots):
                if key in PATH_WARN_ONLY:
                    out.debug("路径在允许根之外(仅告警): %s=%s" % (key, p))
                elif not allow_outside:
                    hits.append("%s=%s 不在允许根内 %s" % (
                        key, p, [str(r) for r in roots]))
            if (project_root is not None and not allow_inside
                    and not allow_outside and _under(candidate, project_root)):
                hits.append("%s=%s 落在项目树内 (CLI 不往项目树写; 确需请加 --allow-inside)"
                            % (key, p))
        else:
            if ".." in candidate.parts and not allow_outside:
                hits.append("%s=%s 含 '..' 越界" % (key, p))

    visit(args)
    if hits:
        die(EX_GUARD, "路径守卫拦截:", *["  - " + h for h in hits],
            "",
            "确需越界请显式加 --allow-outside。")


def guard_output(target, cfg, out, allow_inside=False, what="输出"):
    """输出路径只受一条硬约束: 不许落进项目树 (要就得显式 --allow-inside)。

    输入路径不能乱写, 但输出是用户自己指定的位置, 所以不做 allowed_roots 限制 ——
    只守住"别把 CLI 的产物混进 ModMind 管的项目里"这一条。
    """
    if not target:
        return Path(target)
    p = Path(str(target)).resolve()
    proj = Path(str(cfg.get("project_root") or ""))
    if str(proj) and not allow_inside and _under(p, proj):
        die(EX_GUARD,
            "拒绝把%s写进项目树: %s" % (what, p),
            "  ModMind 自己管项目文件, CLI 的产物不该混进去 (会污染 git 工作区)。",
            "  确需如此请显式加 --allow-inside, 或换一个项目外的位置。")
    return p


def preflight(bridge: Bridge, cfg: dict, out: Out, force=False) -> dict:
    """写动作之前确认 ModMind 绑的是真工程, 不是脚手架空壳。"""
    expect_ns = cfg.get("expect_namespace") or ""
    require = [x.replace("\\", "/") for x in cfg.get("require_files", [])]
    report = {"namespace_expected": expect_ns, "require_files": require}
    if not expect_ns and not require:
        report["skipped"] = "未配置 expect_namespace / require_files"
        return report

    info = bridge.call("project_info", {})
    info_txt = json.dumps(info, ensure_ascii=False)
    report["project_info_excerpt"] = _truncate(info_txt, 400)
    bad = []
    if expect_ns and expect_ns not in info_txt:
        bad.append("project_info 里找不到 namespace=%r" % expect_ns)

    files = bridge.call("project_files", {})
    files_txt = json.dumps(files, ensure_ascii=False).replace("\\\\", "/").replace("\\", "/")
    report["project_files_excerpt"] = _truncate(files_txt, 400)
    missing = [p for p in require if p not in files_txt]
    if missing:
        bad.append("project_files 缺少哨兵文件: %s" % missing)

    if bad:
        print("项目绑定预检失败:", file=sys.stderr)
        for b in bad:
            print("  - " + b, file=sys.stderr)
        print("", file=sys.stderr)
        print("这正是上一轮踩过的坑: ModMind 绑的是脚手架空壳 mod_byqe79,", file=sys.stderr)
        print("构建只会产出 1708 字节的空壳 jar。请先在 ModMind 界面打开真实工程", file=sys.stderr)
        print("(modmind.project.json 的 path 应指向项目根, namespace 应为 %r)。" % expect_ns,
              file=sys.stderr)
        print("确认无害可加 --no-preflight 跳过。", file=sys.stderr)
        raise SystemExit(EX_BINDING)

    report["ok"] = True
    out.debug("preflight ok:", report.get("namespace_expected"))
    return report


# ---------------------------------------------------------------- 进程体检
def split_win_argv(command_line: str) -> list[str]:
    """把 Windows 命令行切成 argv（处理引号与反斜杠转义）。"""
    args, cur, i, n = [], [], 0, len(command_line)
    in_q = False
    while i < n:
        c = command_line[i]
        if c == "\\" and in_q:
            bs = 0
            while i < n and command_line[i] == "\\":
                bs += 1
                i += 1
            if i < n and command_line[i] == '"':
                cur.append("\\" * (bs // 2))
                if bs % 2:
                    cur.append('"')
                else:
                    in_q = not in_q
                    i += 1
                    continue
            else:
                cur.append("\\" * bs)
            continue
        if c == '"':
            in_q = not in_q
        elif c == " " and not in_q:
            if cur:
                args.append("".join(cur))
                cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        args.append("".join(cur))
    return args


def classify_modmind_process(command_line: str):
    """判定一个 ModMind.exe 进程是什么。

    为什么不能用"命令行里含 modmind-mcp-server.mjs"这种子串判断:
    真实 GUI 主进程的形态是
        ModMind.exe --mcp-bridge --project "<项目根>"
    只要项目路径里出现该字样(或有人把该字串塞进别的参数), 主进程就会被误判成
    "可清理的转发器" —— 这是会杀掉用户应用的。所以必须按 argv 结构判定。

    真正的转发器只有一种形态(见 mcp-config.json):
        ModMind.exe "<项目根>/.modmind/external-agents/modmind-mcp-server.mjs"
    即: 除可执行文件外**恰好一个**位置参数, 其 basename 为 modmind-mcp-server.mjs,
    父目录 basename 为 external-agents。
    """
    argv = split_win_argv(command_line or "")
    rest = [a for a in argv[1:] if not a.startswith("--type=")]
    m = re.search(r"--type=([a-z-]+)", command_line or "")
    if m:
        return "helper", m.group(1)                      # renderer / gpu-process / utility
    if len(rest) == 1:
        cand = rest[0]
        if (os.path.basename(cand) == "modmind-mcp-server.mjs"
                and os.path.basename(os.path.dirname(cand)) == "external-agents"):
            return "forwarder", None
    return "main", None


def enumerate_modmind() -> list[dict]:
    """列出 ModMind.exe 进程, 区分 GUI 实例与泄漏的 stdio 转发器。

    需要一次 powershell 调用 —— 这是诊断命令, 不属于正常工具调用路径。
    """
    ps = None
    for cand in ("powershell.exe", "pwsh.exe"):
        from shutil import which
        if which(cand):
            ps = cand
            break
    if not ps:
        return []
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name='ModMind.exe'\" | "
           "Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | "
           "ConvertTo-Json -Compress -Depth 3")
    try:
        raw = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, timeout=45).stdout.strip()
    except Exception:                                 # noqa: BLE001
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    rows = []
    for d in data:
        cl = (d.get("CommandLine") or "")
        kind, subtype = classify_modmind_process(cl)
        rows.append({
            "pid": d.get("ProcessId"),
            "ppid": d.get("ParentProcessId"),
            "command_line": cl,
            "kind": kind,
            "subtype": subtype,
            "created": d.get("CreationDate"),
        })
    order = {"main": 0, "forwarder": 1, "helper": 2}
    rows.sort(key=lambda r: (order.get(r["kind"], 3), r["pid"] or 0))
    return rows


def kill_pid(pid: int) -> bool:
    if os.name != "nt":
        # taskkill 是 Windows 专有; 非 Windows 用 SIGKILL, 不要再调 taskkill(必然失败)
        try:
            os.kill(pid, 9)
        except OSError:
            return False
        for _ in range(20):
            if not _pid_alive(pid):
                return True
            time.sleep(0.15)
        return not _pid_alive(pid)
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=30)
    except Exception:                                 # noqa: BLE001
        return False
    for _ in range(20):
        if not _pid_alive(pid):
            return True
        time.sleep(0.15)
    return not _pid_alive(pid)


# ---------------------------------------------------------------- 命令实现
def bridge_dir(cfg, fallback=None):
    """Directory that holds the active bridge.json, or None when it cannot be resolved.

    Used by the commands that read sibling artefacts (modmind-mcp-server.mjs) next to
    the bridge file, so they degrade to a "not found" message instead of crashing on
    an unconfigured path.
    """
    p = cfg_path(cfg, "bridge_json")
    if p is None:
        resolved, _ = resolve_bridge_path(cfg, None, must_exist=False)
        p = resolved
    if p is None:
        return Path(str(fallback)) if fallback else None
    return p.parent


def cmd_init(args, cfg, out):
    path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    if path.exists() and not args.force:
        die(EX_USAGE, "已存在: %s (要覆盖加 --force)" % path)
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    out.info("已写入配置: %s" % path)
    out.info("请核对 project_root / expect_namespace / require_files 三项。")


def cmd_doctor(args, cfg, out):
    rep = {"version": VERSION, "config": cfg["_config_path"],
           "config_exists": cfg["_config_exists"], "problems": [], "warnings": []}
    # NOTE: the local must not be called cfg_path -- that is the module-level helper,
    # and a local with the same name would shadow it for this whole function.
    bj, _cands = resolve_bridge_path(cfg, None, must_exist=False)
    rep["bridge_json"] = str(bj) if bj else None
    rep["bridge_json_source"] = next((w for p, w in _cands if p == bj), "未指定")
    rep["bridge_candidates"] = [{"path": str(p), "why": w, "exists": p.exists()}
                                for p, w in _cands]
    rep["bridge_json_exists"] = bool(bj and bj.exists())
    if bj and bj.exists():
        try:
            b = json.loads(bj.read_text(encoding="utf-8"))
            rep["bridge"] = {"port": b.get("port"),
                             "token_present": bool(b.get("token")),
                             "version": b.get("version"),
                             "sourceFingerprint": b.get("sourceFingerprint"),
                             "hiddenToolPrefixes": b.get("hiddenToolPrefixes")}
            s = socket.socket()
            s.settimeout(1.5)
            try:
                s.connect(("127.0.0.1", int(b.get("port", 0))))
                rep["port_open"] = True
            except OSError:
                rep["port_open"] = False
            finally:
                s.close()
        except Exception as exc:                      # noqa: BLE001
            rep["bridge_error"] = str(exc)
            rep["problems"].append("bridge.json 存在但读不出来: %s" % exc)
    else:
        rep["problems"].append(
            "bridge.json 缺失 -> ModMind 没有打开项目 (这是当前唯一阻塞, 不是工具损坏)")

    # 只看"端口能连"是不够的: token 可能已经过期(服务端返回 404), 桥其实不可用。
    # 所以这里发一次真实探针, 用它的结果定健康。
    if rep.get("port_open"):
        try:
            probe = Bridge(cfg, Out(quiet=True), "http", False, 8).call("mcp_probe", {})
            rep["probe"] = probe
            rep["bridge_live"] = bool(isinstance(probe, dict) and probe.get("ok"))
            if not rep["bridge_live"]:
                rep["problems"].append("探针返回异常: %s" % json.dumps(probe, ensure_ascii=False)[:120])
        except SystemExit as exc:
            rep["bridge_live"] = False
            rep["probe_exit"] = exc.code
            rep["problems"].append(
                "端口可连但探针失败 (exit %s) -> bridge.json 多半是上一次会话的残留"
                " (token 已换)。跑 mmcli project bind 重新绑定。" % exc.code)

    procs = enumerate_modmind()
    rep["modmind_processes"] = procs
    rep["gui_instances"] = sum(1 for p in procs if p["kind"] == "main")
    rep["helpers"] = sum(1 for p in procs if p["kind"] == "helper")
    rep["leaked_forwarders"] = sum(1 for p in procs if p["kind"] == "forwarder")
    if rep["leaked_forwarders"]:
        rep["warnings"].append(
            "%d 个 stdio 转发器进程残留; 跑 mmcli reap 清理 (只杀转发器, 不碰主进程)"
            % rep["leaked_forwarders"])
    if rep["gui_instances"] > 1:
        rep["warnings"].append("有 %d 个 ModMind 主进程; 多实例各自持项目句柄,"
                               "建议只留一个" % rep["gui_instances"])
    if rep.get("bridge_live"):
        rep["problems"] = [p for p in rep["problems"]
                           if "bridge.json 缺失" not in p and "读不出来" not in p]

    lockdir = Path(cfg["state_dir"]) / "locks"
    locks = []
    if lockdir.exists():
        for f in sorted(lockdir.glob("*.lock")):
            owner = None
            try:
                owner = json.loads(f.read_text(encoding="utf-8"))
            except Exception:                         # noqa: BLE001
                pass
            alive = bool(owner) and _pid_alive(int(owner.get("pid", -1)))
            locks.append({"file": f.name, "owner": owner, "alive": alive})
    rep["locks"] = locks
    if any(not l["alive"] for l in locks):
        rep["warnings"].append("有陈旧锁文件; mmcli lock 查看, --force-unlock 清理")

    proj = cfg_path(cfg, "project_root")
    rep["project_root"] = str(proj) if proj else None
    rep["project_root_exists"] = bool(proj and proj.exists())
    if proj is None:
        rep["warnings"].append(
            "配置里没有 project_root —— 设成 ModMind 绑定的项目目录后, "
            "本工具才能自动找到 bridge.json")
    bdir = bridge_dir(cfg)
    orphan = bdir.parent if bdir else (Path.cwd() / ".modmind")
    rep["tool_data_dirs"] = {str(orphan): orphan.exists()}
    if proj:
        rep["tool_data_dirs"][str(proj / ".modmind")] = (proj / ".modmind").exists()
    if (proj / ".modmind").exists() and orphan.exists() and orphan != (proj / ".modmind"):
        rep["warnings"].append(
            "存在两份 .modmind (%s 与 %s); 换绑项目后旧的那份会变孤儿,"
            "动它之前先冷备份并完全退出 ModMind" % (orphan, proj / ".modmind"))
    if os.environ.get("ELECTRON_RUN_AS_NODE"):
        rep["warnings"].append(
            "当前环境带着 ELECTRON_RUN_AS_NODE=%s (Electron 宿主继承); 若绕开 mmcli 直接"
            "启动 ModMind.exe, 它会退化成 Node 并报 'bad option', 应用不会启动。"
            "mmcli 已在 spawn 时清除。" % os.environ["ELECTRON_RUN_AS_NODE"])
    rep["expect_namespace"] = cfg.get("expect_namespace")
    rep["require_files"] = cfg.get("require_files")
    rep["ok"] = not rep["problems"]

    if out.as_json:
        out.json(rep)
    else:
        lines = ["mmcli %s  体检报告" % VERSION,
                 "  配置       : %s%s" % (rep["config"],
                                          "" if rep["config_exists"] else " (不存在, 用内置默认)"),
                 "  bridge.json: %s" % ("存在" if rep["bridge_json_exists"] else "缺失"),
                 "  项目根     : %s" % (
                     ("%s %s" % (rep["project_root"],
                                 "OK" if rep["project_root_exists"] else "缺失"))
                     if rep["project_root"] else "(未配置)")]
        if rep.get("bridge_error"):
            lines.append("  bridge 错误: %s" % rep["bridge_error"])
        if rep.get("bridge"):
            live = rep.get("bridge_live")
            lines.append("  端口       : %s (%s)" % (
                rep["bridge"]["port"],
                "可连" if not rep.get("port_open") else
                ("探针 OK" if live else "可连但探针失败")))
        elif rep.get("bridge_json_exists"):
            lines.append("  bridge.json: 存在但读不出来")
        lines.append("  ModMind 进程: 主进程 %d / 渲染等辅助 %d / 泄漏转发器 %d"
                     % (rep["gui_instances"], rep["helpers"], rep["leaked_forwarders"]))
        for p in procs:
            lines.append("      [%s] pid=%-7s ppid=%-7s %s"
                         % (p["kind"], p["pid"], p["ppid"],
                            (p["command_line"] or "")[:90]))
        for l in locks:
            lines.append("  锁         : %s owner_pid=%s alive=%s"
                         % (l["file"], (l["owner"] or {}).get("pid"), l["alive"]))
        for w in rep["warnings"]:
            lines.append("  警告: " + w)
        for p in rep["problems"]:
            lines.append("  问题: " + p)
        lines.append("  结论: " + ("可以干活" if rep["ok"] else "先解决上面的问题"))
        out.result(rep, "\n".join(lines))
    raise SystemExit(EX_OK if rep["ok"] else EX_NO_BRIDGE)


def cmd_reap(args, cfg, out):
    allp = enumerate_modmind()
    listing = allp if args.include_gui else [p for p in allp if p["kind"] == "forwarder"]
    rows = [p for p in allp if p["kind"] == "forwarder"]   # 只可能杀转发器
    if not listing:
        out.result({"killed": [], "candidates": []}, "没有需要清理的转发器进程。")
        return
    if not out.as_json:
        print("ModMind 进程:")
        for p in listing:
            print("  pid=%-7s %-9s %s" % (p["pid"], p["kind"], (p["command_line"] or "")[:110]))
        print("可清理 (kind=forwarder): %s" % [p["pid"] for p in rows])
    if args.dry_run or not args.yes:
        out.result({"candidates": rows, "killed": [], "dry_run": True},
                   "\n默认 dry-run。确认后加 --yes 执行 (只杀 kind=forwarder; "
                   "GUI 实例永远不会被 reap 碰到)。")
        return
    killed, failed = [], []
    for p in rows:
        if p["pid"] == os.getpid():
            continue
        (killed if kill_pid(int(p["pid"])) else failed).append(p["pid"])
    out.result({"killed": killed, "failed": failed},
               "已清理: %s%s" % (killed or "无",
                                 ("  失败: %s" % failed) if failed else ""))


def cmd_lock(args, cfg, out):
    lockdir = Path(cfg["state_dir"]) / "locks"
    if args.force_unlock:
        if not lockdir.exists():
            out.info("没有锁目录。")
            return
        n = 0
        for f in lockdir.glob("*.lock"):
            owner = {}
            try:
                owner = json.loads(f.read_text(encoding="utf-8"))
            except Exception:                         # noqa: BLE001
                pass
            if _pid_alive(int(owner.get("pid", -1))) and not args.force:
                out.info("跳过 (持有者仍活着 pid=%s): %s" % (owner.get("pid"), f.name))
                continue
            f.unlink()
            n += 1
            out.info("已删除 " + f.name)
        out.info("共清理 %d 个锁文件。" % n)
        return
    entries = []
    if lockdir.exists():
        for f in sorted(lockdir.glob("*.lock")):
            try:
                owner = json.loads(f.read_text(encoding="utf-8"))
            except Exception:                         # noqa: BLE001
                owner = None
            entries.append({"file": str(f), "owner": owner,
                            "alive": bool(owner) and _pid_alive(int(owner.get("pid", -1)))})
    out.result({"locks": entries, "state_dir": cfg["state_dir"]},
               "\n".join(["%s alive=%s %s" % (e["file"], e["alive"],
                                              json.dumps(e["owner"], ensure_ascii=False))
                          for e in entries]) or "无锁。")


def cmd_list(args, cfg, out):
    rows = sorted(TOOLS.values(), key=lambda r: r["name"])
    if args.level:
        rows = [r for r in rows if r["level"] in set(args.level)]
    if args.readonly:
        rows = [r for r in rows if r["level"] in READ_LEVELS]
    if args.writes:
        rows = [r for r in rows if r["level"] not in READ_LEVELS]
    if args.grep:
        pat = re.compile(args.grep, re.I)
        rows = [r for r in rows if pat.search(r["name"]) or pat.search(r["action"])]
    if out.as_json:
        out.json({"count": len(rows), "tools": rows})
        return
    if not args.quiet:
        print("%-46s %-46s %-9s %s" % ("MCP 工具名", "HTTP action", "级别", "闸门"))
        print("-" * 112)
    for r in rows:
        gate = "review" if r["action"] in REVIEWED_ACTIONS else ""
        print("%-46s %-46s %-9s %s" % (r["name"], r["action"], r["level"], gate))
    if not args.quiet:
        print("-" * 112)
        print("共 %d 个 (全表 %d)。闸门=review 表示服务端会先过 ModMind 自己的 AI Review,"
              " 被拒时是策略拒绝, 不要重试。" % (len(rows), len(TOOLS)))
        print()
        print("桥认、但 MCP schema 没登记的动作 (要用 mm plugins 或 mm call <动作名>):")
        for k, v in sorted(SERVER_ONLY_ACTIONS.items()):
            print("  %-24s %s" % (k, v))
        print("  合计: 桥 100 个动作 = schema 98 个 + 上面 %d 个"
              % len(SERVER_ONLY_ACTIONS))


def verify_against_installed(cfg):
    """把内嵌工具表和安装版转发器的 actions 映射逐条对照。

    内嵌表是"快照", ModMind 升级后可能漂移; 这是唯一能发现漂移的地方。
    """
    src_path = bridge_dir(cfg, SCRIPT_DIR) / "modmind-mcp-server.mjs"
    if not src_path.exists():
        p, _ = resolve_bridge_path(cfg, None, must_exist=False)
        if p is not None:
            src_path = p.parent / "modmind-mcp-server.mjs"
    if not src_path.exists():
        return {"available": False, "reason": "找不到 modmind-mcp-server.mjs: %s" % src_path}
    text = src_path.read_text(encoding="utf-8", errors="replace")
    start = text.find("const actions = {")
    block = text[start:text.find("};", start)] if start >= 0 else ""
    truth = {m.group(1): m.group(2) for m in
             re.finditer(r"(modmind_[a-z0-9_]+)\s*:\s*'([a-z0-9_]+)'", block)}
    mism = {k: {"table": TOOLS[k]["action"], "installed": v}
            for k, v in truth.items() if k in TOOLS and TOOLS[k]["action"] != v}
    return {"available": True, "source": str(src_path), "installed_count": len(truth),
            "table_count": len(TOOLS), "mismatches": mism,
            "missing_in_table": sorted(set(truth) - set(TOOLS)),
            "extra_in_table": sorted(set(TOOLS) - set(truth))}


def cmd_actions(args, cfg, out):
    if args.verify:
        audit = SCRIPT_DIR / "audit" / "tool_names.json"
        if not audit.exists():
            die(EX_USAGE, "找不到 %s" % audit)
        names = set(json.loads(audit.read_text(encoding="utf-8")))
        mine = set(TOOLS)
        ok = names == mine
        live = verify_against_installed(cfg)
        live_ok = (not live.get("available")) or (
            not live.get("mismatches") and not live.get("missing_in_table")
            and not live.get("extra_in_table"))
        lines = ["与 audit/tool_names.json %s (audit=%d, table=%d)"
                 % ("一致" if ok else "不一致", len(names), len(mine))]
        if live.get("available"):
            lines.append("与安装版 .mjs 映射 %s (installed=%d, table=%d)"
                         % ("一致" if live_ok else "不一致",
                            live["installed_count"], live["table_count"]))
            for k, v in (live.get("mismatches") or {}).items():
                lines.append("  漂移 %s: 表=%s 安装版=%s" % (k, v["table"], v["installed"]))
            if live.get("missing_in_table"):
                lines.append("  表里缺: %s" % live["missing_in_table"])
            if live.get("extra_in_table"):
                lines.append("  表里多: %s" % live["extra_in_table"])
        else:
            lines.append("跳过安装版比对: %s" % live.get("reason"))
        out.result({"match": ok and live_ok, "audit": len(names), "table": len(mine),
                    "missing": sorted(names - mine), "extra": sorted(mine - names),
                    "against_installed": live},
                   "\n".join(lines))
        raise SystemExit(EX_OK if (ok and live_ok) else EX_USAGE)
    print(TOOL_TABLE.strip())


def cmd_describe(args, cfg, out):
    name = args.name
    if name not in TOOLS and name not in ACTION_TO_NAME:
        die(EX_USAGE, "未知工具: %s (跑 mmcli list)" % name)
    mcp = name if name in TOOLS else ACTION_TO_NAME[name]
    rec = TOOLS[mcp]
    src_path = bridge_dir(cfg, SCRIPT_DIR) / "modmind-mcp-server.mjs"
    if not src_path.exists():
        die(EX_USAGE, "找不到权威转发器 (含完整 schema): %s" % src_path)
    src = src_path.read_text(encoding="utf-8", errors="replace")
    snippet = _slice_tool_source(src, mcp)
    if out.as_json:
        out.json({"mcp": mcp, "action": rec["action"], "level": rec["level"],
                  "source": str(src_path), "definition": snippet})
    else:
        print("MCP 名  : %s" % mcp)
        print("action  : %s" % rec["action"])
        print("级别    : %s" % rec["level"])
        print("定义来源: %s" % src_path)
        print("-" * 60)
        print(snippet or "(未能抽取, 请直接打开上面的文件搜 %s)" % mcp)


def _slice_tool_source(src: str, name: str) -> str | None:
    """从 .mjs 里切出一个工具的定义片段 (按条目边界切, 比括号配对稳)。"""
    start = src.find("{name:'%s'" % name)
    if start < 0:
        # 动态生成的 test_* 工具
        dyn = src.find("\n  ...['session'")
        if dyn >= 0 and name.startswith("modmind_test_"):
            end = src.find("annotations:managedAction}))", dyn)
            return src[dyn:end + 30].strip()
        return None
    endings = [src.find("\n  {name:'", start + 5), src.find("\n  ...[", start + 5),
               src.find("\n];", start + 5), src.find("\n", start + 5)]
    ends = [e for e in endings if e > 0]
    end = min(ends) if ends else start + 4000
    return src[start:end].strip().rstrip(",")


def cmd_probe(args, cfg, out):
    b = Bridge(cfg, out, cfg.get("_transport", "http"), cfg.get("_allow_stdio", False),
               cfg.get("_timeout"))
    r = b.call("mcp_probe", {})
    out.result({"ok": True, "action": "mcp_probe", "response": r},
               "桥接正常: " + json.dumps(r, ensure_ascii=False)[:200])


def cmd_preflight(args, cfg, out):
    b = Bridge(cfg, out, cfg.get("_transport", "http"), cfg.get("_allow_stdio", False),
               cfg.get("_timeout"))
    rep = preflight(b, cfg, out, force=True)
    out.result(rep, "preflight OK: namespace=%r, 哨兵文件齐全" % cfg.get("expect_namespace"))


def lock_identity(cfg) -> str:
    """锁按「服务端项目身份」取键, 而不是客户端传进来的那串路径。

    否则同一座桥, 只要 --project-root 的写法不同 (正/反斜杠、大小写) 或换了 --config
    (state_dir 不同), 就会算出不同的锁键 —— 两个进程能同时写同一个工程。
    """
    cands = []
    try:
        p, _ = resolve_bridge_path(cfg, None, must_exist=False)
    except SystemExit:
        p = None
    if p is not None:
        st = p.parent / "mcp-bridge.json"
        if st.exists():
            try:
                pp = json.loads(st.read_text(encoding="utf-8")).get("projectPath")
                if pp:
                    cands.append(pp)
            except Exception:                          # noqa: BLE001
                pass
        try:
            cands.append(str(p.parent.parent.parent))   # <root>/.modmind/external-agents/bridge.json
        except Exception:                              # noqa: BLE001
            pass
    cands.append(str(cfg.get("project_root") or "default"))
    for c in cands:
        if not c:
            continue
        try:
            return os.path.normcase(str(Path(c).resolve()))
        except OSError:
            return os.path.normcase(str(c))
    return "default"


def lock_key_for(cfg) -> str:
    return "mmcli-" + hashlib.sha1(lock_identity(cfg).encode("utf-8")).hexdigest()[:12]


def _resolve_call(args, cfg, out):
    # 用 getattr: 不是每个子命令都声明了 --transport/--allow-stdio,
    # 直接点属性会在 capture / state 这类命令上抛 AttributeError。
    return Bridge(cfg, out, getattr(args, "transport", "http"),
                  bool(getattr(args, "allow_stdio", None)),
                  getattr(args, "timeout", None) if getattr(args, "timeout", None) is not None
                  else cfg.get("_timeout"))


def _prepare_tool(args, cfg, out):
    """把 --tool/--action 归一成 (mcp_name, action, level, inputs)。"""
    ident = args.name
    inputs = _parse_inputs(args)
    if ident in TOOLS:
        rec = TOOLS[ident]
        return rec["name"], rec["action"], rec["level"], inputs
    if ident in ACTION_TO_NAME:
        mcp = ACTION_TO_NAME[ident]
        rec = TOOLS[mcp]
        out.debug("按 action 名解析: %s -> %s" % (ident, mcp))
        return mcp, rec["action"], rec["level"], inputs
    if re.fullmatch(r"[a-z0-9_]+", ident):
        # 逃生舱: 允许直接发一个未登记的动作名 (新版本 ModMind 可能新增)
        out.debug("未登记的工具名 %r, 按原生 action 发出" % ident)
        return ident, ident, "unknown", inputs
    die(EX_USAGE, "无效工具名: %r" % ident)


def _parse_inputs(args):
    data = {}
    if getattr(args, "args_file", None):
        path = Path(args.args_file)
        if not path.exists():
            die(EX_USAGE, "--args-file 不存在: %s" % path)
        try:
            data.update(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            die(EX_USAGE, "--args-file 不是合法 JSON: %s (%s)" % (path, exc))
        except OSError as exc:
            die(EX_USAGE, "--args-file 读不出来: %s (%s)" % (path, exc))
    if getattr(args, "args", None):
        try:
            parsed = json.loads(args.args)
        except json.JSONDecodeError as exc:
            die(EX_USAGE, "--args 不是合法 JSON: %s (%s)" % (args.args[:100], exc))
        if not isinstance(parsed, dict):
            die(EX_USAGE, "--args 必须是 JSON 对象")
        data.update(parsed)
    for kv in (getattr(args, "set", None) or []):
        if "=" not in kv:
            die(EX_USAGE, "--set 需要 k=v 形式: %r" % kv)
        k, v = kv.split("=", 1)
        try:
            data[k] = json.loads(v)
        except json.JSONDecodeError:
            data[k] = v
    return data


def _is_write(level, action, inputs) -> bool:
    """写判定: 级别 + 少数工具的只读子操作。unknown 一律按写处理(保守)。"""
    if level in READ_LEVELS:
        return False
    ops = SUBOP_READONLY.get(action)
    if ops:
        op = (inputs or {}).get("operation")
        if "*" in ops or op in ops:
            return False
    return True


def cmd_call(args, cfg, out):
    mcp, action, level, inputs = _prepare_tool(args, cfg, out)
    b = _resolve_call(args, cfg, out)
    guard_paths(inputs, cfg, out, allow_outside=args.allow_outside,
                allow_inside=args.allow_inside,
                project_root=cfg_path(cfg, "project_root"))
    if action in DANGEROUS and not args.yes and not args.dry_run:
        die(EX_GUARD, "%s 属于不可逆/全局动作, 需要 --yes 确认 (可先 --dry-run 看请求)" % action)

    write = _is_write(level, action, inputs)
    out.debug("%s -> action=%s level=%s write=%s" % (mcp, action, level, write))

    lock_key = lock_key_for(cfg)
    lock_enabled = (write or args.serialize_all) and not args.no_lock and not args.dry_run
    lock = FileLock(Path(cfg["state_dir"]), lock_key,
                    float(args.lock_timeout or cfg.get("lock_timeout", 180)),
                    enabled=lock_enabled)
    try:
        with lock:
            if write and not args.no_preflight:
                preflight(b, cfg, out)
            if args.checkpoint:
                b.call("blockbench_checkpoint", {"label": args.checkpoint})
                out.debug("已建 checkpoint: %s" % args.checkpoint)
            started = time.time()
            result = b.call(action, inputs, dry_run=args.dry_run)
            elapsed = time.time() - started
    except TimeoutError as exc:
        die(EX_LOCK, str(exc))

    if args.out:
        outp = guard_output(args.out, cfg, out, bool(opt(args, "allow_inside")), "--out 目标")
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        out.info("已写入 %s" % outp)

    payload = {"mcp": mcp, "action": action, "level": level,
               "elapsed_s": round(elapsed, 2), "result": result}
    if out.as_json:
        out.json(payload)
        return
    if args.raw:
        print(json.dumps(result, ensure_ascii=False))
        return
    text = json.dumps(result, ensure_ascii=False, indent=1)
    print(_truncate(text, opt(args, "max_chars", out.max_chars)))


def cmd_state(args, cfg, out):
    args.name = "modmind_blockbench_project_state"
    args.args = None
    args.args_file = None
    args.set = []
    args.checkpoint = None
    cmd_call(args, cfg, out)


def cmd_capture(args, cfg, out):
    outdir = guard_output(args.out_dir or cfg.get("output_dir") or (SCRIPT_DIR / "shots"),
                          cfg, out, bool(opt(args, "allow_inside")), "截图")
    b = _resolve_call(args, cfg, out)
    views = args.views or ["north", "south", "east", "west", "isometric_right", "initial"]
    inputs = {"views": views, "width": args.width, "height": args.height}
    res = b.call("blockbench_capture_views", inputs)
    caps = res.get("captures") if isinstance(res, dict) else None
    if not caps:
        out.result({"views": views, "written": [], "raw": res},
                   "没拿到 captures 字段。原始返回:\n" + json.dumps(res, ensure_ascii=False)[:800])
        return
    outdir.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    for i, cap in enumerate(caps):
        url = (cap or {}).get("dataUrl") or ""
        view = (cap or {}).get("view") or (cap or {}).get("name") or views[i] if i < len(views) else str(i)
        m = re.match(r"^data:image/(png|jpeg|webp);base64,(.+)$", url, re.S)
        if not m:
            skipped.append(view)
            continue
        ext = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}[m.group(1)]
        p = outdir / ("%s%s" % (re.sub(r"[^A-Za-z0-9_.-]", "_", str(view)), ext))
        p.write_bytes(base64.b64decode(m.group(2)))
        written.append(str(p))
    out.result({"written": written, "skipped": skipped},
               "\n".join(written) + ("\n跳过(非 dataUrl): %s" % skipped if skipped else ""))


def _png_size(raw: bytes):
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        die(EX_USAGE, "不是 PNG 文件")
    w, h = struct.unpack(">II", raw[16:24])
    return w, h


def cmd_bind_texture(args, cfg, out):
    png = Path(args.png)
    if not png.exists():
        die(EX_USAGE, "找不到 PNG: %s" % png)
    raw = png.read_bytes()
    w, h = _png_size(raw)
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
    guard_paths({"path": str(png)}, cfg, out, allow_outside=args.allow_outside,
                allow_inside=True, project_root=cfg_path(cfg, "project_root"))
    b = _resolve_call(args, cfg, out)
    plan = {"png": str(png), "size": [w, h], "name": args.name}
    if args.dry_run:
        out.result({"plan": plan, "dry_run": True}, "将注入 %dx%d 贴图并绑定" % (w, h))
        return

    lock_key = lock_key_for(cfg)
    lock = FileLock(Path(cfg["state_dir"]), lock_key,
                    float(args.lock_timeout or cfg.get("lock_timeout", 180)),
                    enabled=not args.no_lock)
    try:
        with lock:
            if not args.no_preflight:
                preflight(b, cfg, out)
            state = b.call("blockbench_project_state", {})
            rev = _find_revision(state)
            if args.checkpoint:
                b.call("blockbench_checkpoint", {"label": args.checkpoint})
            r1 = b.call("blockbench_actions", {"expectedRevision": rev, "actions": [
                {"type": "create-texture", "name": args.name, "width": w, "height": h,
                 "dataUrl": data_url}]})
            _ensure_ok(r1, "create-texture")

            state2 = b.call("blockbench_project_state", {})
            tex_uuid = _find_texture_uuid(state2, args.name)
            if not tex_uuid:
                die(EX_TOOL, "创建了贴图但读不回 uuid; project_state 摘要: %s"
                    % _truncate(json.dumps(state2, ensure_ascii=False), 600))
            cubes = [c.get("uuid") for c in (state2.get("cubes") or []) if c.get("uuid")]
            if args.cubes:
                cubes = [c for c in cubes if c in set(args.cubes)]
            if not cubes:
                die(EX_TOOL, "工程里没有 cube 可绑定")
            rev2 = _find_revision(state2)
            done, batch = 0, int(args.chunk)
            for i in range(0, len(cubes), batch):
                chunk = cubes[i:i + batch]
                r = b.call("blockbench_actions", {"expectedRevision": rev2, "actions": [
                    {"type": "set-cube-texture", "cubeUuid": u, "textureUuid": tex_uuid}
                    for u in chunk]})
                _ensure_ok(r, "set-cube-texture[%d:%d]" % (i, i + batch))
                rev2 = _find_revision(r) or rev2
                done += len(chunk)
                out.debug("绑定 %d/%d" % (done, len(cubes)))
            final = b.call("blockbench_project_state", {})
    except TimeoutError as exc:
        die(EX_LOCK, str(exc))
    out.result({"texture_uuid": tex_uuid, "cubes": len(cubes), "bound": done,
                "counts": (final or {}).get("counts")},
               "贴图 %s (%s) 已绑定到 %d/%d 个 cube" % (args.name, tex_uuid, done, len(cubes)))


def _find_revision(obj):
    if isinstance(obj, dict):
        for k in ("revision", "contentRevision", "expectedRevision"):
            if isinstance(obj.get(k), str) and obj[k].startswith("sha256:"):
                return obj[k]
        for v in obj.values():
            r = _find_revision(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_revision(v)
            if r:
                return r
    return None


def _find_texture_uuid(state, name):
    found = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("uuid") and (o.get("name") == name or o.get("source") is not None):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(state.get("textures") if isinstance(state, dict) else state)
    for t in found:
        if t.get("name") == name:
            return t.get("uuid")
    return found[-1]["uuid"] if found else None


def _ensure_ok(resp, what):
    if isinstance(resp, dict):
        if resp.get("error"):
            die(EX_TOOL, "%s 失败: %s" % (what, json.dumps(resp["error"], ensure_ascii=False)[:400]))
        results = resp.get("results")
        if isinstance(results, list):
            bad = [r for r in results if isinstance(r, dict) and r.get("success") is False]
            if bad:
                die(EX_TOOL, "%s 有 %d 个动作失败, 首个: %s"
                    % (what, len(bad), json.dumps(bad[0], ensure_ascii=False)[:300]))


# ---------------------------------------------------------------- 游戏实例 / 启动器
# 为什么自己实现启动: PCL 没有命令行接口, 但它点"启动"时做的就是把一条 java 命令
# 写进 LatestLaunch.bat 再执行。这条命令完全可以从实例的 version json 重建出来,
# 于是"进真实世界"这一步就能脱离 GUI。重建结果可与那个 bat 逐参数比对来验证。
CURRENT_OS = {"nt": "windows", "posix": "linux"}.get(os.name, "windows")


def _default_java() -> str:
    home = os.environ.get("APPDATA") or ""
    return str(Path(home) / ".minecraft/runtime/java-runtime-gamma-snapshot/bin/java.exe") if home else ""


def _rules_allow(rules, features=None):
    """评估 version json 的 rules (os.name / os.arch / features), 语义与官方启动器一致。"""
    if not rules:
        return True
    allowed = False
    for r in rules:
        ok = True
        o = r.get("os") or {}
        if o.get("name") and o["name"] != CURRENT_OS:
            ok = False
        if ok and o.get("arch"):
            # x86 规则只对 32 位 JVM 生效; 本机按 64 位处理
            if o["arch"] != "x86_64":
                ok = False
        if ok and r.get("features"):
            for k, v in r["features"].items():
                if bool((features or {}).get(k, False)) != bool(v):
                    ok = False
        if ok:
            allowed = (r.get("action") == "allow")
    return allowed


def _lib_path(lib, libs_root):
    art = ((lib.get("downloads") or {}).get("artifact") or {})
    p = art.get("path")
    if not p and lib.get("name"):
        parts = str(lib["name"]).split(":")
        if len(parts) >= 3:
            g, a, v = parts[0], parts[1], parts[2]
            p = "%s/%s/%s/%s-%s.jar" % (g.replace(".", "/"), a, v, a, v)
    return Path(libs_root) / p if p else None


def list_instances(cfg):
    root = Path(str(cfg.get("versions_root") or ""))
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        vj = d / (d.name + ".json")
        mods = d / "mods"
        out.append({"name": d.name, "path": str(d), "version_json": str(vj),
                    "has_version_json": vj.exists(),
                    "mods": sorted(p.name for p in mods.glob("*.jar")) if mods.is_dir() else [],
                    "mods_dir": str(mods)})
    return out


def build_launch(instance, cfg, overrides=None):
    """从 version json 重建完整启动命令。纯函数, 不发任何请求、不启动任何东西。"""
    ov = overrides or {}

    def _posint(label, raw, default):
        """None 才算"没给"; 给了就必须是正整数 —— 0/负数不能被静默替换成默认值。"""
        if raw is None:
            return default
        try:
            val = int(raw)
        except (TypeError, ValueError):
            die(EX_USAGE, "%s 必须是整数 (收到 %r)" % (label, raw))
        if val <= 0:
            die(EX_USAGE, "%s 必须是正数 (收到 %s)" % (label, val))
        return val

    _mem = _posint("内存(MB)", ov.get("memory_mb") if ov.get("memory_mb") is not None
                   else cfg.get("memory_mb"), 4096)
    if _mem < 512:
        die(EX_USAGE, "内存至少 512 MB (收到 %s)" % _mem)
    _w = _posint("窗口宽", ov.get("width"), 854)
    _h = _posint("窗口高", ov.get("height"), 480)

    d = require_cfg_path(cfg, "versions_root", "启动实例需要它指向游戏的 versions 目录") / instance
    vj = d / (instance + ".json")
    if not vj.exists():
        die(EX_USAGE, "找不到版本 json: %s" % vj)
    try:
        v = json.loads(vj.read_text(encoding="utf-8"))
    except OSError as exc:
        die(EX_USAGE, "版本 json 读不出来: %s (%s)" % (vj, exc))
    except json.JSONDecodeError as exc:
        die(EX_USAGE, "版本 json 不是合法 JSON: %s (%s)" % (vj, exc))
    main_class = v.get("mainClass")
    if not main_class or not isinstance(main_class, str):
        die(EX_USAGE, "版本 json 里没有可用的 mainClass: %s" % vj)
    libs_root = Path(str(cfg.get("libraries_root") or ""))
    assets_root = Path(str(cfg.get("assets_root") or ""))
    natives = d / (instance + "-natives")

    features = {"is_demo_user": False, "has_custom_resolution": True,
                "has_quick_plays_support": True,
                "is_quick_play_singleplayer": bool(ov.get("quick_play")),
                "is_quick_play_multiplayer": False, "is_quick_play_realms": False}
    missing = []
    classpath = []
    for lib in v.get("libraries") or []:
        if not _rules_allow(lib.get("rules"), features):
            continue
        p = _lib_path(lib, libs_root)
        if p is None:
            continue
        if p.exists():
            classpath.append(str(p))
        else:
            missing.append(str(p))
    version_jar = d / (instance + ".jar")
    if version_jar.exists():
        classpath.append(str(version_jar))

    subst = {
        "natives_directory": str(natives),
        "launcher_name": "mmcli",
        "launcher_version": VERSION,
        "classpath": ";".join(classpath),
        "classpath_separator": ";",
        "library_directory": str(libs_root),
        "version_name": instance,
        "game_directory": str(d),
        "assets_root": str(assets_root),
        "assets_index_name": str((v.get("assetIndex") or {}).get("id") or ""),
        "auth_player_name": ov.get("username") or cfg.get("player_name") or "Player",
        "auth_uuid": ov.get("uuid") or "00000000000000000000000000000000",
        "auth_access_token": ov.get("token") or "0",
        "auth_session": ov.get("token") or "0",
        "user_type": "legacy",
        "version_type": "mmcli",
        "resolution_width": str(_w),
        "resolution_height": str(_h),
        "quickPlayPath": "",
        "quickPlaySingleplayer": str(ov.get("quick_play") or ""),
        "quickPlayMultiplayer": "",
        "clientid": "",
        "auth_xuid": "",
    }

    def flat(seq):
        out = []
        for item in seq or []:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                if _rules_allow(item.get("rules"), features):
                    val = item.get("value")
                    out.extend(val if isinstance(val, list) else [val])
        return out

    def sub(s):
        # version json 的占位符语法是 美元符+花括号+名字;
        # 这里用字符串拼接而不是字面量, 免得被外层模板字符串/构建工具吃掉
        for k, val in subst.items():
            s = s.replace("${" + k + "}", val)
        return s

    args = v.get("arguments") or {}
    jvm = [sub(a) for a in flat(args.get("jvm"))]
    game = [sub(a) for a in flat(args.get("game"))]
    if not jvm and v.get("minecraftArguments"):
        jvm = []
        game = [sub(a) for a in str(v["minecraftArguments"]).split()]

    exe = str(ov.get("java") or cfg.get("java_exe") or _default_java())
    mem = _mem
    jvm = ["-Xmx%dm" % mem, "-Xmn%dm" % max(512, mem // 4)] + jvm
    qp = ov.get("quick_play")
    if qp:
        # 有些版本 json 自带 --quickPlaySingleplayer 占位符; 直接追加会重复传参
        if "--quickPlaySingleplayer" in game:
            game[game.index("--quickPlaySingleplayer") + 1] = str(qp)
        else:
            game += ["--quickPlaySingleplayer", str(qp)]
    return {
        "exe": exe, "args": jvm + [main_class] + game,
        "instance": instance, "cwd": str(d), "natives": str(natives),
        "classpath_entries": len(classpath), "missing_libraries": missing,
        "assets_root": str(assets_root), "version_json": str(vj),
        "memory_mb": mem,
    }


def cmd_instance(args, cfg, out):
    sub = args.instance_cmd
    insts = list_instances(cfg)
    if sub == "list":
        want = args.name
        rows = [i for i in insts if not want or i["name"] == want]
        if out.as_json:
            out.json({"versions_root": cfg.get("versions_root"), "instances": rows})
            return
        print("实例根: %s" % cfg.get("versions_root"))
        for i in rows:
            print("\n  %s" % i["name"])
            print("    目录   : %s" % i["path"])
            print("    version: %s" % ("有" if i["has_version_json"] else "缺失"))
            print("    mods   : %d 个" % len(i["mods"]))
            for m in i["mods"]:
                print("       - %s" % m)
        return

    # sync: 把构建产物装进实例
    name = args.name or cfg.get("default_instance")
    inst = next((i for i in insts if i["name"] == name), None)
    if not inst:
        die(EX_USAGE, "找不到实例 %r; 现有: %s" % (name, [i["name"] for i in insts]))
    idir = resolve_instance(cfg, name)
    src = Path(str(args.jar)) if args.jar else newest_build_jar(cfg)
    if not src or not Path(src).exists():
        die(EX_USAGE, "找不到要装的 jar (用 --jar 指定, 或先 mm call modmind_build_project)")
    if _JAR_EXCLUDE.search(Path(src).name):
        die(EX_USAGE, "%s 看着是附属产物(-sources/-javadoc/...), 不是 mod 主 jar" % src)
    mods = idir / "mods"
    target = mods / Path(src).name

    # 冲突判定按 modid, 不按文件名: 文件名猜法会误删同前缀的其它 mod,
    # 又会漏掉真正同 modid 的旧版本 (留下两个同 modid 的 jar, Forge 起不来)。
    new_id = jar_modid(src)
    conflicts = []
    if mods.is_dir():
        for old in sorted(mods.glob("*.jar")):
            if old.name == target.name:
                conflicts.append((old, "同名"))
            elif new_id and jar_modid(old) == new_id:
                conflicts.append((old, "同 modid " + new_id))
    plan = {"instance": name, "source": str(src), "target": str(target),
            "size": Path(src).stat().st_size, "modid": new_id,
            "already_installed": bool(target.exists()
                                     and _sha256(target) == _sha256(src)),
            "will_remove": [{"name": o.name, "reason": w} for o, w in conflicts],
            "removed": []}
    if not new_id:
        plan["warning"] = ("读不出新 jar 的 modid, 冲突判定退回按文件名, 可能漏判同 modid 的旧版本")
    if args.dry_run:
        out.result({"plan": plan, "dry_run": True},
                   "将把 %s (%d 字节, modid=%s) 装到 %s\n  会先移走: %s\n  (dry-run: 一个字节都没动)"
                   % (Path(src).name, plan["size"], new_id or "?", mods,
                      [c["name"] for c in plan["will_remove"]] or "无"))
        return
    if plan["already_installed"] and not args.force:
        out.result(plan, "目标已是同一份内容 (sha256 相同), 未做改动; 要强制重装加 --force")
        return

    mods.mkdir(parents=True, exist_ok=True)
    if not args.no_backup:
        # 秒级时间戳会撞车: 同一秒第二次同步会覆盖第一次的备份, 原始内容永久丢失。
        # 加上 pid 与序号, 保证每次都是全新目录。
        base = "%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid())
        bdir = idir / "mod-backups" / base
        n = 1
        while bdir.exists():
            n += 1
            bdir = idir / "mod-backups" / (base + "-%d" % n)
        bdir.mkdir(parents=True, exist_ok=False)
        for old, _why in conflicts:
            shutil.copy2(old, bdir / old.name)
        plan["backup"] = str(bdir)

    # 先移走冲突项; 有任何一个移不动就整体中止, 避免"旧的删了新的没装"的半残状态
    failed = []
    for old, why in conflicts:
        try:
            old.unlink()
            plan["removed"].append("%s (%s)" % (old.name, why))
        except OSError as exc:
            failed.append("%s: %s" % (old.name, exc))
    if failed:
        die(EX_TOOL, "有 jar 移不走, 已中止安装 (备份在 %s):\n  %s\n"
                     "常见原因: 游戏正在运行占用了文件。" % (plan.get("backup"), "\n  ".join(failed)))
    try:
        shutil.copy2(src, target)
    except OSError as exc:
        die(EX_TOOL, "复制失败: %s (被移走的旧 jar 已备份在 %s, 可手工还原)"
                     % (exc, plan.get("backup")))
    plan["installed"] = str(target)
    out.result(plan, "已装到 %s\n  modid    : %s\n  备份     : %s\n  移走旧版本: %s"
               % (target, new_id or "?", plan.get("backup"), plan["removed"] or "无"))


# Gradle 会同时产出 -sources / -javadoc / -dev / -slim 等附属 jar;
# 它们和主产物在同一个目录、mtime 往往更新, 按 mtime 裸取会把它们当成 mod 装进游戏。
_JAR_EXCLUDE = re.compile(r"-(sources|javadoc|dev|slim|api|tests?)\.jar$", re.I)


def newest_build_jar(cfg):
    d = Path(str(cfg.get("build_libs") or ""))
    if not d.is_dir():
        return None
    jars = [p for p in d.glob("*.jar") if not _JAR_EXCLUDE.search(p.name)]
    jars.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return jars[0] if jars else None


def jar_modid(path):
    """从 jar 里读 modid (Forge mods.toml / Fabric / Quilt)。读不出返回 None。

    用 modid 判断"是不是同一个 mod", 而不是靠文件名前缀猜 —— 文件名猜法
    会误删同前缀的其它 mod, 又漏掉真正同 modid 的旧版本。
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for meta in ("META-INF/mods.toml", "fabric.mod.json", "quilt.mod.json"):
                if meta not in names:
                    continue
                text = z.read(meta).decode("utf-8", "replace")
                m = re.search(r'modId\s*=\s*"([^"]+)"', text)
                if not m:
                    m = re.search(r'"id"\s*:\s*"([^"]+)"', text)
                if m:
                    return m.group(1)
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def _sha256(path, limit=8 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1 << 16)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def resolve_instance(cfg, name):
    """按名字取实例, 并拒绝名字里的路径分隔符/穿越。"""
    if not name or "/" in str(name) or "\\" in str(name) or ".." in str(name):
        die(EX_USAGE, "实例名非法: %r" % name)
    root = Path(str(cfg.get("versions_root") or "")).resolve()
    d = (root / str(name)).resolve()
    if d.parent != root:
        die(EX_USAGE, "实例路径越出 versions_root: %s" % d)
    return d


def cmd_launch(args, cfg, out):
    name = args.instance or cfg.get("default_instance")
    plan = build_launch(name, cfg, {"quick_play": args.quick_play,
                                    "width": args.width, "height": args.height,
                                    "memory_mb": args.memory,
                                    "username": args.username})
    if args.compare_pcl:
        cmp = _compare_with_pcl(plan, cfg)
        if out.as_json and not args.launch:
            out.json({"plan": {"exe": plan["exe"], "classpath_entries": plan["classpath_entries"],
                               "missing_libraries": plan["missing_libraries"]},
                      "compare": cmp})
            return
        for line in cmp["lines"]:
            _say(line)
        if args.strict:
            if cmp["ok"] is None:
                die(EX_USAGE, "无法与 PCL 比对 (缺少参照), --strict 下视为失败;"
                              " 注意这不等于「不一致」")
            if cmp["ok"] is False:
                die(EX_USAGE, "重建的启动命令与 PCL 有实质差异 (见上)")
    if args.dry_run or not args.launch:
        text = "\n".join([
            "实例    : %s" % plan["instance"],
            "java    : %s" % plan["exe"],
            "cwd     : %s" % plan["cwd"],
            "natives : %s" % plan["natives"],
            "classpath: %d 项 (缺失 %d)" % (plan["classpath_entries"], len(plan["missing_libraries"])),
            "内存    : %d MB" % plan["memory_mb"],
            "参数数  : %d" % len(plan["args"]),
        ])
        if args.dry_run:
            out.result({"plan": {k: v for k, v in plan.items() if k != "args"},
                        "argv_preview": plan["args"][:8] + ["...", plan["args"][-6:]]},
                       text + "\n(命令已重建; 要真启动加 --launch)")
            return
        out.info(text)
        out.info("(dry-run; 要真启动加 --launch)")
        return

    if plan["missing_libraries"] and not args.allow_missing:
        die(EX_USAGE,
            "有 %d 个库文件缺失, 拒绝启动 (缺库只会得到一个诡异的崩溃, 很难归因):\n  %s\n"
            "  缺的库通常意味着实例没装完整 —— 先在 PCL 里把它启动一次补齐, "
            "或显式加 --allow-missing 强行尝试。"
            % (len(plan["missing_libraries"]),
               "\n  ".join(Path(x).name for x in plan["missing_libraries"][:8])))
    logdir = Path(cfg["state_dir"]) / "launch"
    logdir.mkdir(parents=True, exist_ok=True)
    logp = logdir / ("%s-%s.log" % (plan["instance"], time.strftime("%Y%m%d-%H%M%S")))
    flags = 0x00000008 if os.name == "nt" else 0     # DETACHED_PROCESS
    with logp.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen([plan["exe"]] + plan["args"], cwd=plan["cwd"],
                                stdout=fh, stderr=subprocess.STDOUT,
                                creationflags=flags, env=clean_env())
    out.result({"launched": True, "pid": proc.pid, "instance": plan["instance"],
                "log": str(logp)},
               "已启动 %s (pid=%s)\n  日志: %s\n  下一步: mm shot 截图, 或 mm test 用 ModMind 的托管客户端"
               % (plan["instance"], proc.pid, logp))


# 这些参数"不同"是设计使然: 账号类型/版本类型/内存由我们的配置决定,
# 它们不参与 ok 判定, 但必须如实列出来 (否则"一致 ✅"是骗人的)。
_EXPECTED_DIFF = ("--userType", "--versionType", "--username", "-Xmx", "--uuid",
                  "--accessToken", "--clientId", "--xuid")


def _compare_with_pcl(plan, cfg):
    """把重建出来的命令与 PCL 生成的 LatestLaunch.bat 比对。

    这是**不需要 OpenGL 就能验证启动器正确性**的办法。
    三个返回值要分清:
      ok=True  -> classpath 与关键启动参数都对得上
      ok=False -> 有实质差异
      ok=None  -> **无法比对** (没有参照物), 这跟"不一致"是两码事, 不能混报。
    """
    bat = Path(str(cfg.get("pcl_dir") or "")) / "LatestLaunch.bat"
    if not bat.exists():
        return {"ok": None, "lines": ["(找不到 %s —— 无法比对, 注意这不等于不一致)" % bat]}
    txt = bat.read_text(encoding="utf-8", errors="replace")
    jline = next((l for l in txt.splitlines() if "java.exe" in l), None)
    if not jline:
        return {"ok": None, "lines": ["(bat 里没有 java 命令行 —— 无法比对)"]}
    ref_args = [p.strip('"') for p in re.findall(r'"[^"]*"|\S+', jline)][1:]

    def cp_of(argv):
        for i, a in enumerate(argv):
            if a == "-cp" and i + 1 < len(argv):
                return [c for c in argv[i + 1].split(";") if c]
        return []

    def val_of(argv, flag):
        idx = [k for k, a in enumerate(argv) if a == flag]
        return argv[idx[0] + 1] if idx and idx[0] + 1 < len(argv) else None

    ref_cp, my_cp = cp_of(ref_args), cp_of(plan["args"])
    if not ref_cp or not my_cp:
        # 关键: 空对空绝不能报"一致" —— 那样任何两条完全不同的命令都会"通过"
        return {"ok": None, "lines": ["与 PCL 的 LatestLaunch.bat 比对:",
                "  -cp 有一侧为空 (参照 %d 项 / 重建 %d 项) —— 无法比对"
                % (len(ref_cp), len(my_cp))]}

    lines = ["与 PCL 的 LatestLaunch.bat 比对:"]
    blocking, info = [], []
    only_ref = sorted(set(ref_cp) - set(my_cp))
    only_mine = sorted(set(my_cp) - set(ref_cp))
    if only_ref or only_mine or len(ref_cp) != len(my_cp):
        blocking.append("classpath 不一致 (参照 %d / 重建 %d; 仅 PCL %d; 仅重建 %d)"
                        % (len(ref_cp), len(my_cp), len(only_ref), len(only_mine)))
    lines.append("  classpath : %s"
                 % ("一致 (%d 项)" % len(ref_cp) if not only_ref and not only_mine
                    and len(ref_cp) == len(my_cp) else "不一致"))
    for flag in ("--gameDir", "--version", "--assetsDir", "--assetIndex",
                 "--launchTarget", "--fml.forgeVersion"):
        rv, mv = val_of(ref_args, flag), val_of(plan["args"], flag)
        if rv != mv:
            (info if flag in _EXPECTED_DIFF else blocking).append(
                "%s 参照=%s 重建=%s" % (flag, rv, mv))
    for flag in _EXPECTED_DIFF:
        rv, mv = val_of(ref_args, flag), val_of(plan["args"], flag)
        if rv != mv:
            info.append("%s 参照=%s 重建=%s (预期不同, 由本地配置决定)" % (flag, rv, mv))
    for d in blocking:
        lines.append("  ✗ " + d)
    for d in info:
        lines.append("  · " + d)
    lines.append("  结论    : %s" % ("classpath 与关键启动参数一致" if not blocking
                                    else "有 %d 处实质差异" % len(blocking)))
    return {"ok": not blocking, "lines": lines,
            "differences": blocking, "informational": info}


def cmd_shot(args, cfg, out):
    """截取窗口 (默认找游戏窗口), 落到 PNG。"""
    target = args.window or "Minecraft"
    outp = guard_output(args.out or (Path(cfg["state_dir"]) / "shots" /
                                     ("shot-%s.png" % time.strftime("%Y%m%d-%H%M%S"))),
                        cfg, out, bool(opt(args, "allow_inside")), "截图")
    outp.parent.mkdir(parents=True, exist_ok=True)
    ps = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
public struct RECT { public int Left, Top, Right, Bottom; }
"@ -Name Shot -Namespace MM
# Title/proc/out come from environment variables only. Never interpolate user
# input into this script: doing so allows injection via --window.
# Keep this file pure ASCII; PowerShell 5.1 reads .ps1 as ANSI without a BOM.
# Windows deletes an environment variable when its value is empty, so an unset
# MM_PROC arrives as $null. Casting makes it '' and truthiness tests stay honest
# ($null -ne '' is TRUE in PowerShell, which would match every window).
$title = [string]$env:MM_TITLE
$procName = [string]$env:MM_PROC
$outPng = [string]$env:MM_OUT
$win = $null
Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -ne '' } | ForEach-Object {
  if ($win -ne $null) { return }
  if ($procName -and $_.ProcessName -like ('*' + $procName + '*')) { $win = $_; return }
  if ($title -and $_.MainWindowTitle -like ('*' + $title + '*')) { $win = $_; return }
}
# Fall back to "any window" only when the caller asked for nothing specific.
if ($win -eq $null -and $env:MM_FALLBACK -eq '1') {
  $win = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -ne '' } | Select-Object -First 1
}
if ($win -eq $null) { Write-Output 'NOWINDOW'; exit 2 }
[MM.Shot]::ShowWindow($win.MainWindowHandle, 9) | Out-Null
[MM.Shot]::SetForegroundWindow($win.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 600
$r = New-Object MM.Shot+RECT
[MM.Shot]::GetWindowRect($win.MainWindowHandle, [ref]$r) | Out-Null
$w = $r.Right - $r.Left; $h = $r.Bottom - $r.Top
if ($w -le 0 -or $h -le 0) { Write-Output 'BOUNDS'; exit 3 }
$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
$bmp.Save($outPng, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output ('OK ' + $win.ProcessName + ' ' + $win.MainWindowTitle + ' ' + $w + 'x' + $h)
'''
    # 参数走环境变量, 脚本本身是常量 —— 这样用户输入没有任何机会变成代码
    for label, val in (("--window", target), ("--process", args.process or "")):
        if "\n" in val or "\r" in val or len(val) > 256:
            die(EX_USAGE, "%s 含非法字符或过长" % label)
    script = ps
    tmp = Path(cfg["state_dir"]) / "shot.ps1"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: 让 PowerShell 5.1 明确按 UTF-8 解码, 不靠 ANSI 猜
    tmp.write_text(script, encoding="utf-8-sig")
    exe = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not exe:
        die(EX_USAGE, "找不到 powershell")
    env = clean_env()
    # 只有用户没指定任何筛选条件时才允许"随便抓一个窗口";
    # 一旦指定了 --window/--process 却匹配不上, 必须报错 —— 否则会静默截到无关窗口(隐私泄漏)。
    explicit = bool(args.window or args.process)
    env.update({"MM_TITLE": target, "MM_PROC": args.process or "", "MM_OUT": str(outp),
                "MM_FALLBACK": "0" if explicit else "1"})
    r = subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=90, env=env)
    line = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
    if outp.exists() and outp.stat().st_size > 1000:
        out.result({"saved": str(outp), "size": outp.stat().st_size, "window": line},
                   "已截图: %s (%d 字节)\n  窗口: %s" % (outp, outp.stat().st_size, line))
    else:
        die(EX_TOOL, "截图失败: %s\n%s" % (line or r.stderr[:200],
                                       "提示: 目标窗口要可见(不能最小化); 用 --window 指定标题关键字, "
                                       "或 --process 指定进程名"))


TEST_OPS = {
    "start": lambda a: {"operation": "start", "mode": a.mode or "rendered",
                        "offline": True, "username": a.username or "Tester",
                        "acceptEula": True, "hidden": bool(a.hidden)},
    "stop": lambda a: {"operation": "stop", "sessionId": a.session},
    "state": lambda a: {"operation": "state", "sessionId": a.session},
    "capabilities": lambda a: {"operation": "capabilities"},
    "observe": lambda a: {"operation": "observe", "sessionId": a.session},
    "shot": lambda a: {"operation": "capture", "sessionId": a.session},
    "cmd": lambda a: {"operation": "command", "sessionId": a.session, "command": a.value},
    "key": lambda a: {"operation": "key", "sessionId": a.session, "key": a.value},
    "text": lambda a: {"operation": "text", "sessionId": a.session, "text": a.value,
                       "pressEnter": bool(a.enter)},
    "click": lambda a: {"operation": "click", "sessionId": a.session,
                        "x": a.x, "y": a.y, "button": a.button or 0},
    "look": lambda a: {"operation": "look", "sessionId": a.session,
                       "yaw": a.yaw, "pitch": a.pitch},
    "scenario": lambda a: {"operation": "scenario", "sessionId": a.session,
                           "steps": json.loads(Path(a.file).read_text(encoding="utf-8"))},
}


def cmd_test(args, cfg, out):
    """ModMind 托管的真实客户端测试 (rendered, 带原生截图)。"""
    sub = args.test_cmd
    action = {"start": "test_session", "stop": "test_session", "state": "test_session",
              "capabilities": "test_session", "observe": "test_observe",
              "shot": "test_capture", "cmd": "test_action", "key": "test_action",
              "text": "test_action", "click": "test_action", "look": "test_action",
              "scenario": "test_scenario"}[sub]
    inputs = TEST_OPS[sub](args)
    inputs = {k: v for k, v in inputs.items() if v is not None}
    b = Bridge(cfg, out, getattr(args, "transport", "http"), False,
               getattr(args, "timeout", None) or 900)
    res = b.call(action, inputs)
    if sub == "shot":
        caps = (res or {}).get("captures") or []
        saved = []
        for i, cap in enumerate(caps):
            url = (cap or {}).get("dataUrl") or ""
            m = re.match(r"^data:image/(png|jpeg|webp);base64,(.+)$", url, re.S)
            if not m:
                continue
            d = Path(args.out_dir or (Path(cfg["state_dir"]) / "shots"))
            d.mkdir(parents=True, exist_ok=True)
            p = d / ("test-%s-%d.png" % (time.strftime("%Y%m%d-%H%M%S"), i))
            p.write_bytes(base64.b64decode(m.group(2)))
            saved.append(str(p))
        out.result({"saved": saved, "raw": res if not saved else None},
                   "\n".join(saved) or json.dumps(res, ensure_ascii=False)[:400])
        return
    out.result(res if not out.as_json else {"action": action, "input": inputs, "result": res},
               _truncate(json.dumps(res, ensure_ascii=False, indent=1),
                         opt(args, "max_chars", out.max_chars)))


def _say(msg):
    """进度一律走 stderr: 这样 --json 时 stdout 仍是可被 json.loads 的纯 JSON。"""
    print(msg, file=sys.stderr)


def cmd_verify(args, cfg, out):
    """把 构建 → 装机 → 启动 → 等 → 截图 串成一条命令。

    失败必须反映到退出码 —— 否则 CI/调用方会把失败当成功继续往下走。
    """
    steps = []
    skipped = [k for k, v in (("build", args.skip_build), ("sync", args.skip_sync),
                              ("launch", args.skip_launch), ("shot", args.skip_shot)) if v]
    if len(skipped) == 4:
        die(EX_USAGE, "四个步骤都被 --skip-* 掉了, 没有可执行的事")

    def step(name, fn):
        t0 = time.time()
        try:
            r = fn()
            steps.append({"step": name, "ok": True, "elapsed_s": round(time.time() - t0, 1),
                          "result": r})
            _say("  [%s] OK (%.1fs)" % (name, time.time() - t0))
            return r
        except SystemExit as exc:
            steps.append({"step": name, "ok": False, "code": exc.code,
                          "elapsed_s": round(time.time() - t0, 1)})
            _say("  [%s] FAILED (exit %s)" % (name, exc.code))
            raise
        except Exception as exc:                       # noqa: BLE001
            steps.append({"step": name, "ok": False, "error": repr(exc)[:200],
                          "elapsed_s": round(time.time() - t0, 1)})
            _say("  [%s] ERROR %r" % (name, exc))
            raise

    _say("mm verify: 构建 → 装机 → 启动 → 截图%s"
         % (" (跳过: %s)" % ",".join(skipped) if skipped else ""))
    failed = False
    try:
        if not args.skip_build:
            step("build", lambda: _verify_build(args, cfg, out))
        if not args.skip_sync:
            step("sync", lambda: _verify_sync(args, cfg, out))
        if not args.skip_launch:
            step("launch", lambda: _verify_launch(args, cfg, out))
            if args.wait:
                _say("  等待 %ss 让游戏加载..." % args.wait)
                time.sleep(float(args.wait))
        if not args.skip_shot:
            step("shot", lambda: _verify_shot(args, cfg, out))
    except (SystemExit, Exception):
        failed = True

    report = {"ok": not failed, "skipped": skipped, "steps": steps}
    if failed:
        out.result(report, "中断: 见上面的步骤结果")
        raise SystemExit(EX_TOOL)
    out.result(report, "全链路完成。截图与日志见 %s" % cfg["state_dir"])


def _verify_build(args, cfg, out):
    b = Bridge(cfg, out, "http", False, getattr(args, "timeout", None) or 1800)
    r = b.call("build_project", {})
    return {"artifact": (r or {}).get("artifact")}


def _verify_sync(args, cfg, out):
    class A:
        pass
    a = A()
    a.name = args.instance
    a.jar = None
    a.no_backup = False
    a.dry_run = False
    cmd_instance(_with(a, {"instance_cmd": "sync"}), cfg, mm_out_quiet(out))
    return {"instance": args.instance or cfg.get("default_instance")}


def _verify_launch(args, cfg, out):
    a = type("A", (), {})()
    a.instance = args.instance
    a.launch = True
    a.dry_run = False
    a.quick_play = args.quick_play
    a.width = None
    a.height = None
    a.memory = None
    a.username = None
    a.compare_pcl = False
    a.strict = False
    a.json = False
    a.quiet = True
    a.verbose = False
    a.max_chars = 400
    cmd_launch(a, cfg, mm_out_quiet(out))
    return {"launched": True}


def _verify_shot(args, cfg, out):
    a = type("A", (), {})()
    a.window = args.window or "Minecraft"
    a.process = args.process
    a.out = args.out
    a.allow_inside = False
    a.json = False
    a.quiet = True
    a.verbose = False
    a.max_chars = 400
    cmd_shot(a, cfg, mm_out_quiet(out))
    return {"shot": a.out}


def mm_out_quiet(out):
    o = Out(as_json=False, quiet=True, verbose=False, max_chars=out.max_chars)
    return o


def _with(obj, extra):
    for k, v in extra.items():
        setattr(obj, k, v)
    return obj


# ---------------------------------------------------------------- geo.json 原生导入
def _geo_pick(geo, which=None):
    """从 geo.json 里挑出要导入的那份几何。"""
    if not isinstance(geo, dict) or "minecraft:geometry" not in geo:
        die(EX_USAGE, "不是合法的 Bedrock 几何文件 (缺 minecraft:geometry)")
    geos = geo["minecraft:geometry"]
    if not isinstance(geos, list) or not geos:
        die(EX_USAGE, "minecraft:geometry 为空")
    if which is None:
        return geos[0]
    if isinstance(which, int) or (isinstance(which, str) and which.isdigit()):
        i = int(which)
        if not 0 <= i < len(geos):
            die(EX_USAGE, "几何索引越界: %d (共 %d 份)" % (i, len(geos)))
        return geos[i]
    for g in geos:
        if (g.get("description") or {}).get("identifier") == which:
            return g
    die(EX_USAGE, "找不到 identifier=%r 的几何; 现有: %s"
        % (which, [ (x.get("description") or {}).get("identifier") for x in geos ]))


def _geo_bone_order(bones):
    """按 parent 依赖排序 (父先于子); 检测环与悬空父。"""
    by_name = {b.get("name"): b for b in bones if b.get("name")}
    order, state = [], {}

    def visit(name, stack):
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            die(EX_USAGE, "骨层级有环: %s" % " -> ".join(stack + [name]))
        b = by_name.get(name)
        if b is None:
            return
        state[name] = 1
        parent = b.get("parent")
        if parent and parent in by_name:
            visit(parent, stack + [name])
        state[name] = 2
        order.append(b)

    for b in bones:
        if b.get("name"):
            visit(b["name"], [])
    return order


def geo_to_actions(geometry, model_name, tex_wh=None, texture_name=None, format_id=None):
    """把 Bedrock 几何翻译成 Blockbench 动作。

    坐标映射逐条复刻 Blockbench 自己的 Codecs.bedrock.parseBone/parseCube
    (bundle.js 里的 S5 / w5) —— 这是"原生等价"的依据, 不是自创约定:
        group.origin   = [-pivot.x,  pivot.y,  pivot.z]
        group.rotation = [-rot.x,   -rot.y,    rot.z]
        cube.from      = [-(ox+sx),  oy,       oz]
        cube.to        = [-ox,       oy+sy,    oz+sz]
        cube.origin    = [-cpivot.x, cpivot.y, cpivot.z]
        cube.rotation  = [-rot.x,   -rot.y,    rot.z]
    """
    desc = geometry.get("description") or {}
    bones = geometry.get("bones") or []
    # 工程 UV 空间取"贴图真实尺寸"(没有贴图时退回 geo 声明值)。
    # geo 的 UV 空间是 description.texture_width/height, 两者常常不等 —— 这里按比例换算,
    # 保证归一化后的采样位置与原文件完全一致 (117/128 == 234/256)。
    geo_tw = int(desc.get("texture_width") or (tex_wh[0] if tex_wh else 16)) or 16
    geo_th = int(desc.get("texture_height") or (tex_wh[1] if tex_wh else 16)) or 16
    tw = int(tex_wh[0]) if tex_wh else geo_tw
    th = int(tex_wh[1]) if tex_wh else geo_th
    uv_scale = (tw / float(geo_tw), th / float(geo_th))

    # 格式必须是 bedrock_block 而不是 bedrock: 两者的 Format.box_uv 不同
    # (bedrock=true, bedrock_block=false), 而 add-cube 建出的 cube 会继承工程的 box_uv。
    # box_uv=true 的 cube 会**静默丢弃** update-cube-faces 写进去的逐面 UV
    # (实测: 写 [234,198,235,199] 回读变成盒式推导值 [8,8,16,16])。
    # 用 bedrock_block 才能让逐面 UV 生效, 采样结果与 GUI 原生导入完全一致。
    actions = [{"type": "new-model", "format": (format_id or "bedrock_block"),
                "name": model_name, "textureWidth": tw, "textureHeight": th}]
    stats = {"bones": 0, "cubes": 0, "faces_with_uv": 0, "uv_attempts": []}

    for bone in _geo_bone_order(bones):
        bname = bone.get("name")
        if not bname:
            continue
        p = bone.get("pivot") or [0, 0, 0]
        r = bone.get("rotation") or [0, 0, 0]
        g = {"type": "add-group", "name": bname,
             "origin": [-p[0], p[1], p[2]], "rotation": [-r[0], -r[1], r[2]]}
        if bone.get("parent"):
            g["parentGroupName"] = bone["parent"]
        actions.append(g)
        stats["bones"] += 1

        for idx, cube in enumerate(bone.get("cubes") or []):
            ox, oy, oz = cube["origin"]
            sx, sy, sz = cube["size"]
            cpiv = cube.get("pivot") or bone.get("pivot") or [0, 0, 0]
            crot = cube.get("rotation") or [0, 0, 0]
            cname = cube.get("name") or ("%s_%d" % (bname, idx))
            a = {"type": "add-cube", "name": cname,
                 "from": [-(ox + sx), oy, oz],
                 "to": [-ox, oy + sy, oz + sz],
                 "origin": [-cpiv[0], cpiv[1], cpiv[2]],
                 "rotation": [-crot[0], -crot[1], crot[2]],
                 "parentGroupName": bname}
            if cube.get("inflate") is not None:
                a["inflate"] = cube["inflate"]
            elif bone.get("inflate") is not None:
                a["inflate"] = bone["inflate"]
            if texture_name:
                a["textureName"] = texture_name
            actions.append(a)
            stats["cubes"] += 1

            uv = cube.get("uv")
            if isinstance(uv, dict):     # 逐面 UV
                faces = {}
                for fname, f in uv.items():
                    if not isinstance(f, dict) or "uv" not in f:
                        continue
                    u, v = f["uv"][0], f["uv"][1]
                    uw, vh = (f.get("uv_size") or [0, 0])[0], (f.get("uv_size") or [0, 0])[1]
                    # 面 UV 是**像素单位**(不是 0~1 归一化), 且必须落在工程的 UV 空间里。
                    # geo 自己的 UV 空间是 description.texture_width(常与贴图真实尺寸不同),
                    # 所以要按比例换算到工程空间 —— 这样归一化后与原文件逐位一致。
                    faces[fname] = {"uv": [u * uv_scale[0], v * uv_scale[1],
                                           (u + uw) * uv_scale[0], (v + vh) * uv_scale[1]]}
                    if f.get("uv_rotation"):
                        faces[fname]["rotation"] = f["uv_rotation"]
                    stats["faces_with_uv"] += 1
                if faces:
                    stats["uv_attempts"].append({"cubeName": cname, "faces": faces})
            elif isinstance(uv, list) and len(uv) >= 2:   # 盒式 UV
                stats["uv_attempts"].append({"cubeName": cname, "boxUv": [uv[0], uv[1]]})
    return actions, {"textureWidth": tw, "textureHeight": th,
                     "bones": stats["bones"], "cubes": stats["cubes"],
                     "facesWithUv": stats["faces_with_uv"]}, stats["uv_attempts"]


def _geo_shape(geometry):
    """把几何抽成可比对的结构, 用于导入后校验。"""
    out = {}
    for bone in geometry.get("bones") or []:
        cubes = []
        for c in bone.get("cubes") or []:
            cubes.append({"origin": [round(float(x), 5) for x in c["origin"]],
                          "size": [round(float(x), 5) for x in c["size"]],
                          "pivot": ([round(float(x), 5) for x in c["pivot"]]
                                    if c.get("pivot") else None),
                          "rotation": ([round(float(x), 5) for x in c["rotation"]]
                                       if c.get("rotation") else None)})
        out[bone.get("name")] = {"pivot": ([round(float(x), 5) for x in bone["pivot"]]
                                           if bone.get("pivot") else None),
                                 "parent": bone.get("parent"), "cubes": cubes}
    return out


def cmd_import_geo(args, cfg, out):
    geo_path = Path(args.geo)
    if not geo_path.exists():
        die(EX_USAGE, "找不到 geo.json: %s" % geo_path)
    try:
        geo = json.loads(geo_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(EX_USAGE, "geo.json 不是合法 JSON: %s" % exc)
    geometry = _geo_pick(geo, args.geometry)
    model_name = args.name or (geometry.get("description") or {}).get("identifier", "model")
    model_name = model_name.replace("geometry.", "")

    tex_wh = None
    data_url = None
    texture_name = None
    if args.texture:
        png = Path(args.texture)
        if not png.exists():
            die(EX_USAGE, "找不到贴图: %s" % png)
        raw = png.read_bytes()
        tex_wh = _png_size(raw)
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
        texture_name = png.stem
        guard_paths({"path": str(png)}, cfg, out, allow_outside=args.allow_outside,
                    allow_inside=True, project_root=cfg_path(cfg, "project_root"))

    actions, meta, uv_plan = geo_to_actions(geometry, model_name, tex_wh, texture_name)
    if data_url:
        # 必须紧跟 new-model: add-cube 里的 textureName 要求贴图此刻已存在
        actions.insert(1, {"type": "create-texture", "name": texture_name,
                           "width": tex_wh[0], "height": tex_wh[1], "dataUrl": data_url})

    plan = {"geo": str(geo_path), "model": model_name, "format": "bedrock",
            "texture": str(args.texture) if args.texture else None,
            "projectTextureSize": [meta["textureWidth"], meta["textureHeight"]],
            "actions": len(actions), "bones": meta["bones"], "cubes": meta["cubes"],
            "facesWithUv": meta["facesWithUv"]}
    if args.dry_run:
        out.result({"plan": plan, "preview": actions[:6], "dry_run": True},
                   json.dumps(plan, ensure_ascii=False, indent=1))
        return

    b = _resolve_call(args, cfg, out)
    lock = FileLock(Path(cfg["state_dir"]), lock_key_for(cfg),
                    float(args.lock_timeout or cfg.get("lock_timeout", 180)),
                    enabled=not args.no_lock)
    try:
        with lock:
            if not args.no_preflight:
                preflight(b, cfg, out)
            if args.checkpoint:
                b.call("blockbench_checkpoint", {"label": args.checkpoint})
            r = b.call("blockbench_actions", {"actions": actions})
            _ensure_ok(r, "import-geo(建模型)")

            state = b.call("blockbench_project_state", {})
            cube_uuids = [c["uuid"] for c in state.get("cubes") or []]
            plan["cubesInProject"] = len(cube_uuids)
            plan["groupsInProject"] = state.get("counts", {}).get("groups")
    except TimeoutError as exc:
        die(EX_LOCK, str(exc))

    # ---- UV: 说出来而不是假装成功 (见 --why-uv)
    uv_applied = 0
    if uv_plan and not args.skip_uv:
        by_name = {c.get("name"): c["uuid"] for c in (state.get("cubes") or [])}
        faces_actions = []
        for item in uv_plan:
            uid = by_name.get(item["cubeName"])
            if uid and item.get("faces"):
                faces_actions.append({"type": "update-cube-faces", "cubeUuid": uid,
                                      "faces": item["faces"]})
        if faces_actions:
            rr = b.call("blockbench_actions", {"actions": faces_actions})
            uv_applied = len(faces_actions)
            plan["uvAttempt"] = {"cubes": uv_applied}
    plan["uv"] = ("逐面 UV 已按像素写入; 可行性取决于格式 —— bedrock_block(box_uv=false) 才生效, "
                  "bedrock(box_uv=true) 会静默丢弃。见 --why-uv")

    if args.verify:
        plan["verify"] = _verify_roundtrip(b, cfg, out, geometry, args)
    out.result(plan, json.dumps(plan, ensure_ascii=False, indent=1))


def _verify_roundtrip(b, cfg, out, geometry, args):
    """用 Blockbench 自己的 compile() 反向导出, 与源文件逐字段比对。

    这是"导入是否原生等价"的唯一硬证据 —— 不是我说像, 是原生 codec 自己吐回来的一致。
    """
    rel = args.verify_path or ("local-reference-assets/_mmcli_verify_%s.geo.json"
                               % re.sub(r"[^A-Za-z0-9_.-]", "_", str(args.name or "model")))
    b.call("blockbench_actions", {"actions": [{"type": "export-model", "relativePath": rel}]})
    root = cfg_path(cfg, "project_root") or Path(".")
    exported = root / rel
    if not exported.exists():
        return {"ok": False, "error": "导出文件未生成: %s" % exported}
    try:
        got = json.loads(exported.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": "导出内容不是 JSON: %s" % exc}
    exp_geo = _geo_pick(got) if "minecraft:geometry" in got else None
    if exp_geo is None:
        return {"ok": False, "error": "导出内容里没有 minecraft:geometry"}

    want = _geo_shape(geometry)
    have = _geo_shape(exp_geo)
    diffs = []
    for name in sorted(set(want) | set(have)):
        w, h = want.get(name), have.get(name)
        if w is None:
            diffs.append("多出的骨: %s" % name); continue
        if h is None:
            diffs.append("丢失的骨: %s" % name); continue
        if w["pivot"] != h["pivot"]:
            diffs.append("骨 %s pivot %s != %s" % (name, w["pivot"], h["pivot"]))
        if (w["parent"] or None) != (h["parent"] or None):
            diffs.append("骨 %s parent %s != %s" % (name, w["parent"], h["parent"]))
        if len(w["cubes"]) != len(h["cubes"]):
            diffs.append("骨 %s cube 数 %d != %d" % (name, len(w["cubes"]), len(h["cubes"])))
            continue
        for i, (wc, hc) in enumerate(zip(w["cubes"], h["cubes"])):
            for key in ("origin", "size", "pivot", "rotation"):
                if wc[key] != hc[key]:
                    diffs.append("骨 %s cube#%d %s %s != %s" % (name, i, key, wc[key], hc[key]))
    exported.unlink(missing_ok=True)
    return {"ok": not diffs, "exportPath": rel, "bones": len(want),
            "cubes": sum(len(v["cubes"]) for v in want.values()),
            "differences": diffs[:40], "differenceCount": len(diffs),
            "note": "几何/层级/枢轴/旋转 与原文件比对; UV 不在此列 (公开动作集无法设置)"}


# ---------------------------------------------------------------- bedrock 动画导入
ANIM_CHANNELS = ("rotation", "position", "scale")


def _anim_snapping(times):
    """逐条复刻 Blockbench 的 calculateSnappingFromKeyframes:
    从 10 往上找到第一个 n, 使所有时间点 n*t 都近似整数; 找不到就保持默认 20。
    (注意是"最小的可行 n"而不是最大 —— 后者会算出 120 这种不匹配的值。)
    """
    ts = [float(t) for t in times]
    if len(ts) <= 1:
        return 20
    for n in range(10, 101):
        ok = True
        for t in ts:
            r = (t * n) % 1
            if 0.01 < r < 0.99:
                ok = False
                break
        if ok:
            return n
    return 20


def _anim_value(channel, v):
    """逐条复刻 AnimationCodec.codecs.bedrock 的数值变换 (bundle 里 T1.loadFile 的 E()):

        position: x 取反
        rotation: x, y 取反
        scale   : 不变
    数字/字符串 => 三轴同值。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float, list, tuple)):
        return None                      # molang 表达式等, 公开动作集只能收数字
    if isinstance(v, (int, float)):
        return [float(v)] * 3
    if len(v) < 3:
        return None
    x, y, z = (float(v[0]), float(v[1]), float(v[2]))
    if channel == "position":
        return [-x, y, z]
    if channel == "rotation":
        return [-x, -y, z]
    return [x, y, z]


def _anim_keyframes(channel, spec, fps=20):
    """把一条通道的 spec 展开成 [(time, value, interpolation)]。

    三种形态 (与 Blockbench 的 loadFile 一致):
      - 数组/数字        -> 时间 0 的单帧
      - {post:[..]}      -> 时间 0 的瞬时帧 (pre/post 表达"到这一刻才变")
      - {"0.5": [..]}    -> 每个时间键一帧
    pre/post 在 Blockbench 里是同一关键帧的两个 data_point, 而公开动作集一帧只收一个
    value, 所以拆成 t-1/fps (step) 与 t 两帧 —— 语义等价, 帧数多一个。
    """
    out = []
    if isinstance(spec, (list, tuple)) or isinstance(spec, (int, float)) and not isinstance(spec, bool):
        v = _anim_value(channel, spec)
        if v:
            out.append((0.0, v, "linear"))
        return out
    if not isinstance(spec, dict):
        return out
    if "pre" in spec or "post" in spec:
        pre = _anim_value(channel, spec.get("pre")) if spec.get("pre") is not None else None
        post = _anim_value(channel, spec.get("post")) if spec.get("post") is not None else None
        lerp = spec.get("lerp_mode") or "linear"
        if pre is not None:
            out.append((0.0, pre, "step"))
        if post is not None:
            t = 0.0 if pre is None else 0.0
            out.append((t, post, lerp))
        return out
    for key, val in spec.items():
        try:
            t = float(key)
        except (TypeError, ValueError):
            continue
        if isinstance(val, dict):
            sub = val.get("post", val.get("pre"))
            v = _anim_value(channel, sub)
            lerp = val.get("lerp_mode") or "linear"
        else:
            v = _anim_value(channel, val)
            lerp = "linear"
        if v:
            out.append((t, v, lerp if lerp in ("linear", "catmullrom", "step", "bezier") else "linear"))
    return out


def _load_anim_files(paths):
    anims = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            die(EX_USAGE, "找不到动画文件: %s" % path)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            die(EX_USAGE, "%s 不是合法 JSON: %s" % (path, exc))
        block = doc.get("animations")
        if not isinstance(block, dict):
            die(EX_USAGE, "%s 里没有 animations 对象 (是 bedrock 动画文件吗?)" % path)
        for name, spec in block.items():
            anims.append({"name": name, "spec": spec, "file": str(path)})
    return anims


def _anim_plan(a, groups_by_name):
    """把一份动画展开成 (animation 头, 关键帧列表, 跳过项)。"""
    spec = a.get("spec") or {}
    loop_raw = spec.get("loop")
    loop = "loop" if loop_raw is True else ("hold" if loop_raw == "hold_on_last_frame" else None)
    if loop_raw in ("loop", "once", "hold"):
        loop = loop_raw
    length = spec.get("animation_length")
    kfs, skipped, missing = [], [], set()
    times = []
    for bone_name, channels in (spec.get("bones") or {}).items():
        g = groups_by_name.get(str(bone_name).lower())
        if not g:
            missing.add(bone_name)
            continue
        for ch, chspec in (channels or {}).items():
            if ch not in ANIM_CHANNELS:
                continue
            for (t, v, lerp) in _anim_keyframes(ch, chspec):
                kfs.append({"groupName": g, "channel": ch, "time": t,
                            "value": v, "interpolation": lerp})
                times.append(t)
            if isinstance(chspec, dict) and not all(
                    str(k).replace(".", "", 1).isdigit() for k in chspec):
                skipped.append("%s.%s" % (bone_name, ch))
    head = {"name": a["name"], "length": float(length or 1.0),
            "loop": loop or "once", "snapping": _anim_snapping(times)}
    if spec.get("override_previous_animation"):
        head["_override"] = True
    return head, kfs, skipped, sorted(missing)


def cmd_import_anim(args, cfg, out):
    files = args.anim if isinstance(args.anim, list) else [args.anim]
    anims = _load_anim_files(files)
    if args.name:
        keep = set(args.name if isinstance(args.name, list) else [args.name])
        anims = [a for a in anims if a["name"] in keep]
        if not anims:
            die(EX_USAGE, "过滤后没有动画可导入 (--name %s)" % ",".join(keep))

    b = _resolve_call(args, cfg, out)
    lock = FileLock(Path(cfg["state_dir"]), lock_key_for(cfg),
                    float(args.lock_timeout or cfg.get("lock_timeout", 180)),
                    enabled=not args.no_lock)
    try:
        with lock:
            if not args.no_preflight:
                preflight(b, cfg, out)
            state = b.call("blockbench_project_state", {})
            groups_by_name = {str(g.get("name", "")).lower(): g.get("name")
                              for g in (state.get("groups") or [])}

            plans = []
            for a in anims:
                head, kfs, skipped, missing = _anim_plan(a, groups_by_name)
                plans.append({"head": head, "kfs": kfs, "file": a["file"],
                              "skipped": skipped, "missing": missing})

            plan_summary = {"file": files, "project": state.get("project", {}).get("name"),
                            "existingAnims": state.get("counts", {}).get("animations"),
                            "animations": [
                                {"name": p["head"]["name"], "length": p["head"]["length"],
                                 "loop": p["head"]["loop"], "snapping": p["head"]["snapping"],
                                 "keyframes": len(p["kfs"]),
                                 "skippedChannels": p["skipped"],
                                 "missingGroups": p["missing"][:12],
                                 "missingGroupCount": len(p["missing"])}
                                for p in plans]}
            if args.dry_run:
                out.result({"plan": plan_summary, "dry_run": True},
                           json.dumps(plan_summary, ensure_ascii=False, indent=1))
                return

            if not any(p["kfs"] for p in plans):
                die(EX_TOOL, "没有任何可导入的关键帧。缺失的骨: %s; 请先用 mm import-geo 建好同名分组"
                    % (plans[0]["missing"][:10] if plans else []))

            if args.checkpoint:
                b.call("blockbench_checkpoint", {"label": args.checkpoint})

            created = []
            for p in plans:
                head = {"name": p["head"]["name"], "length": p["head"]["length"],
                        "loop": p["head"]["loop"], "snapping": p["head"]["snapping"]}
                r = b.call("blockbench_actions", {"actions": [
                    {"type": "add-animation", **head}]})
                _ensure_ok(r, "add-animation(%s)" % head["name"])
                uid = None
                for res in r.get("results", []):
                    uid = (res.get("data") or {}).get("uuid") or uid
                created.append({"name": head["name"], "uuid": uid, "keyframes": len(p["kfs"])})

                # add-keyframe 的校验器要求"两个都用 UUID"或"两个都用名字", 不许混用。
                # 这里统一用名字: 动画名刚刚创建, 分组名来自工程回读, 都唯一可解析。
                batch = []
                for k in p["kfs"]:
                    item = {"type": "add-keyframe", **k}
                    item["animationName"] = head["name"]
                    batch.append(item)
                for i in range(0, len(batch), 400):
                    chunk = batch[i:i + 400]
                    rr = b.call("blockbench_actions", {"actions": chunk})
                    _ensure_ok(rr, "add-keyframe[%d:%d]" % (i, i + len(chunk)))

            after = b.call("blockbench_project_state", {})
    except TimeoutError as exc:
        die(EX_LOCK, str(exc))

    result = {"imported": created,
              "projectAnimations": after.get("counts", {}).get("animations"),
              "plan": plan_summary}
    if args.verify:
        result["verify"] = _verify_anim(b, plans, after)
    out.result(result, json.dumps(result, ensure_ascii=False, indent=1))


def _verify_anim(b, plans, state):
    """用工程回读的 animator 统计校验: 每个骨每条通道的帧数是否与计划一致。"""
    want = {}
    for p in plans:
        for k in p["kfs"]:
            key = (p["head"]["name"], k["groupName"], k["channel"])
            want[key] = want.get(key, 0) + 1
    anims = {a.get("name"): a for a in (state.get("animations") or [])}
    diffs = []
    by_uuid = {g.get("uuid"): g.get("name") for g in (state.get("groups") or [])}
    for (aname, gname, ch), n in sorted(want.items()):
        a = anims.get(aname)
        if not a:
            diffs.append("动画缺失: %s" % aname)
            continue
        got = 0
        for animator in a.get("animators") or []:
            if by_uuid.get(animator.get("targetUuid")) == gname:
                got = animator.get("%sKeyframes" % ch, 0)
        if got != n:
            diffs.append("%s / %s / %s: 计划 %d 帧, 工程里 %d 帧" % (aname, gname, ch, n, got))
    return {"ok": not diffs, "animationsChecked": len(want),
            "differenceCount": len(diffs), "differences": diffs[:20],
            "note": "project_state 只暴露每骨每通道的帧数, 不含具体数值; 这里是结构性校验。"}


def cmd_plugins(args, cfg, out):
    """插件工具: 目前唯一能从 CLI 调到的官方扩展点。"""
    b = _resolve_call(args, cfg, out)
    sub = args.plugins_cmd
    if sub == "list":
        r = b.call("plugin_tools", {})
        tools = (r or {}).get("tools") if isinstance(r, dict) else None
        if not tools:
            out.result({"tools": [], "raw": r},
                       "没有已启用的插件工具。\n"
                       "  插件目录: %APPDATA%/ModMind/plugins (或用 modmind_plugins_scaffold 建一个)\n"
                       "  装好后运行 mm plugins reload")
            return
        out.result({"tools": tools},
                   "\n".join("%-44s %s" % (t.get("name"), (t.get("description") or "")[:70])
                              for t in tools))
        return
    if sub == "call":
        name = args.tool
        inputs = {}
        if args.set:
            for kv in args.set:
                if "=" not in kv:
                    die(EX_USAGE, "--set 需要 k=v: %r" % kv)
                k, v = kv.split("=", 1)
                try:
                    inputs[k] = json.loads(v)
                except json.JSONDecodeError:
                    inputs[k] = v
        if args.args:
            try:
                inputs = json.loads(args.args)
            except json.JSONDecodeError as exc:
                die(EX_USAGE, "--args 不是合法 JSON: %s" % exc)
        # plugin_tool_call 的入参形状与其它动作不同: {tool, input}
        r = b.call("plugin_tool_call", {"tool": name, "input": inputs})
        out.result({"tool": name, "input": inputs, "result": r},
                   _truncate(json.dumps(r, ensure_ascii=False, indent=1),
                             opt(args, "max_chars", out.max_chars)))
        return
    # reload
    r = b.call("plugin_reload", {})
    out.result({"reloaded": True, "result": r},
               "插件已重载: " + json.dumps(r, ensure_ascii=False)[:200])


def cmd_why_uv(args, cfg, out):
    """讲清 UV 这件事。⚠️ 本文档早期版本在这里给过错误结论, 见下方 correction。"""
    payload = {
        "answer": "UV 可以设。必须用 bedrock_block 格式建工程; 用 bedrock 会被静默丢弃。",
        "correction": (
            "早期结论曾是「公开动作集无法设置 UV, 是硬边界」。那个结论错了 ——"
            "错在同一类错误上: 只验了一条路径(默认 bedrock 格式)就把「我的实现受限」"
            "当成了「工具的能力边界」。真相是 face.uv 写入本身有效, 只是对 box_uv=true 的 cube 不生效,"
            "而 box_uv 由工程的 Format 决定。"
        ),
        "how": [
            "new-model 的 format 用 bedrock_block (Format.box_uv=false), 不要用 bedrock (true)。",
            "add-cube 建出的 cube 继承工程 box_uv; box_uv=true 时逐面 UV 被忽略。",
            "face.uv 是**像素单位**的 4 个数字 (x1,y1,x2,y2), 不是 0~1 归一化小数 ——"
            " 传小数会把面钉在左上角, 表现就是整片糊掉。",
            "face.uv 必须落在工程的 UV 空间内。工程 UV 空间由 new-model 的 textureWidth/Height 决定;"
            " 若取贴图真实尺寸, geo 里的 UV 需按 (贴图尺寸 / geo 声明尺寸) 等比放大。",
        ],
        "proof": [
            "实测 format=bedrock       : 写 [234,198,235,199] -> 回读 [8,8,16,16]     (被丢弃)",
            "实测 format=bedrock_block : 写 [234,198,235,199] -> 回读 [234,198,235,199] (保留)",
            "采样等价性: GUI 导入 uv_width=128 + uv=117 -> 117/128=0.9141;"
            " CLI uv_width=256 + uv=234 -> 234/256=0.9141 —— 归一化后相同, 落在同一像素。",
            "完整回归: mm import-geo --verify 导入 normal 形态 -> 350 cube / 26 骨 / 0 差异,"
            " 逐面 UV 全保留, 离屏渲染与原生导入外观一致。",
        ],
        "what_is_still_unreachable": {
            "channel": "ModMind 内部动作 load-source-document -> Codecs[bedrock].load()",
            "status": "被 validateAction 白名单拒绝: Unsupported Blockbench action: load-source-document",
            "note": "只由 GUI 的「资源包 -> 打开模型」触发。但它是**另一种实现方式**,"
                    " 不是**另一种结果** —— 上面的翻译层能产出等价的工程状态, 且可被原生编译码反向验证。",
        },
    }
    out.result(payload, json.dumps(payload, ensure_ascii=False, indent=1))


def cmd_explain(args, cfg, out):
    p, cands = resolve_bridge_path(cfg, None, must_exist=False)
    info = None
    if p is not None:
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            info = None
    payload = {
        "protocol": {
            "method": "POST",
            "url": "http://127.0.0.1:<port>/tool",
            "headers": {"Content-Type": "application/json",
                        "x-modmind-token": "<bridge.json 的 token, 必带>"},
            "body": {"action": "<动作名, 不是 MCP 工具名>", "input": {}},
        },
        "status_codes": {
            "404": "method/url/token 任一不符 -> 文本 'Not found' (即 token 过期或端口错了)",
            "400": "动作抛错 -> text/plain 错误正文 (未知动作/AI Review 拒绝/功能被禁都走这里)",
            "200": "成功 -> application/json, 就是该动作的原始返回值",
        },
        "bridge_json_schema": {
            "port": "本机监听端口 (随机)",
            "token": "UUID, 每次启动都换",
            "version": "ModMind 版本",
            "sourceFingerprint": "sha256:... 仅回显, 不参与校验",
            "hasPluginTools": "是否有插件工具",
            "hiddenToolPrefixes": "被隐藏的工具前缀; 公开桥通常为空数组",
        },
        "bridge_json": {"resolved": str(p) if p else None,
                        "exists": bool(p and p.exists()),
                        "candidates": [{"path": str(x), "why": w, "exists": x.exists()}
                                       for x, w in cands],
                        "content": ({k: ("<hidden>" if k == "token" else v)
                                     for k, v in (info or {}).items()} if info else None)},
        "project_open": {
            "how": "ModMind.exe --mcp-bridge --project <项目根>",
            "stop": "ModMind.exe --mcp-bridge-stop",
            "cli": "mmcli project bind <路径>",
            "requirement": "<项目根>/modmind.project.json 必须存在 (readProjectInfo 只认这个)",
            "note": "已在跑 -> 单实例 second-instance 接收 argv; 没在跑 -> 冷启动并直接起桥",
            "not_equivalent_to_gui": "GUI 打开项目还会 offerProjectVersionMigration (弹版本迁移对话框 "
                                     "并可能改写 Gradle 模板); --mcp-bridge 走的是 startPublicMcpBridge, "
                                     "不触发迁移。老版本项目请先用 GUI 打开一次。",
        },
        "review_gate": sorted(REVIEWED_ACTIONS),
        "example": {"mcp": "modmind_blockbench_project_state",
                    "action": TOOLS["modmind_blockbench_project_state"]["action"]},
        "note": "MCP 转发器 = stdio -> HTTP 的瘦包装; 直连省掉每次调用一个 Electron 进程。",
        "tool_count": len(TOOLS),
    }
    out.result(payload, json.dumps(payload, ensure_ascii=False, indent=2))


# ------------------------------------------------- project 绑定 (原先只能点 GUI)
def _modmind_exe(cfg):
    exe = require_cfg_path(cfg, "modmind_exe", "需要 ModMind.exe 的完整路径")
    if not exe.exists():
        die(EX_USAGE, "找不到 ModMind.exe: %s (改 config 的 modmind_exe)" % exe)
    return str(exe)


def clean_env():
    """洗掉从 Electron 宿主继承来的变量。

    关键坑: DSH / ZCode / VS Code 这类宿主自己就是 Electron, 环境里带着
    ELECTRON_RUN_AS_NODE=1。子进程继承后, ModMind.exe 会退化成 Node 解释器:
    既不会启动 GUI, 也会把 --mcp-bridge 当成非法 Node 选项而报
        "ModMind.exe: bad option: --mcp-bridge"  并 exit 9 (Node 的退出码)
    现象极具误导性 —— 看起来像"ModMind 不支持这个开关/应用坏了"。
    必须清掉, 让它变回真正的 Electron 应用。
    """
    env = dict(os.environ)
    for key in ("ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "ELECTRON_NO_ATTACH_CONSOLE",
                "ELECTRON_FORCE_IS_PACKAGED"):
        env.pop(key, None)
    return env


def _launch_modmind(cfg, extra_argv, out):
    """把 argv 交给 ModMind: 已在跑 -> 单实例转发; 没在跑 -> 冷启动。

    这是一次性的参数投递, 不是"每次调用起一个进程"那种泄漏 —— 转发进程投递完就退出。
    """
    exe = _modmind_exe(cfg)
    if os.environ.get("ELECTRON_RUN_AS_NODE"):
        out.debug("检测到宿主 ELECTRON_RUN_AS_NODE=%s, 已为子进程清除"
                  % os.environ["ELECTRON_RUN_AS_NODE"])
    flags = (0x08000000 | 0x00000008) if os.name == "nt" else 0   # NO_WINDOW | DETACHED
    out.debug("spawn(one-shot):", exe, extra_argv)
    # 落盘日志: 冷启动/转发失败时唯一能看到原因的地方
    logdir = Path(cfg["state_dir"]) / "spawns"
    logdir.mkdir(parents=True, exist_ok=True)
    log = (logdir / ("modmind-%d.log" % int(time.time()))).open("w", encoding="utf-8",
                                                                errors="replace")
    return subprocess.Popen([exe] + list(extra_argv), creationflags=flags,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            env=clean_env())


def _bridge_file(root):
    return Path(root) / ".modmind" / "external-agents" / "bridge.json"


def _wait_bridge(root, seconds, out, want=True):
    bj = _bridge_file(root)
    deadline = time.time() + float(seconds)
    while time.time() < deadline:
        if want:
            if bj.exists():
                try:
                    data = json.loads(bj.read_text(encoding="utf-8"))
                    if data.get("port") and data.get("token") and _port_listening(data["port"]):
                        return data
                except Exception:                      # noqa: BLE001
                    pass
        else:
            if not bj.exists():
                return True
        time.sleep(0.5)
    return None


def modmind_recent_log(n=10):
    """回读 ModMind 自己的诊断日志 —— 绑定失败时唯一能说清原因的地方。"""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    hits = []
    try:
        dirs = [d for d in Path(appdata).iterdir() if d.is_dir()]
    except OSError:
        return []
    for d in dirs:
        f = d / "logs" / "diagnostic-events.jsonl"
        if not f.exists():
            continue
        try:
            tail = f.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        except OSError:
            continue
        for line in tail:
            if any(k in line for k in ('"mcp-bridge"', '"project"', '"error"', '"critical"')):
                try:
                    rec = json.loads(line)
                except Exception:                      # noqa: BLE001
                    continue
                hits.append("%s  %s/%s  %s  %s" % (
                    rec.get("time", "")[11:19], rec.get("subsystem"), rec.get("operation"),
                    rec.get("phase"), (rec.get("message") or "")[:110]))
    return hits[-n:]


def _userdata_dirs():
    out = []
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return out
    base = Path(appdata)
    for name in ("ModMind", "modmind", "ModMind-mcp"):
        d = base / name
        if d.exists():
            out.append(d)
    try:
        for d in base.iterdir():
            if d.is_dir() and (d / "recent-projects.json").exists() and d not in out:
                out.append(d)
    except OSError:
        pass
    return out


def _read_recent_projects():
    for d in _userdata_dirs():
        f = d / "recent-projects.json"
        if f.exists():
            try:
                return d, json.loads(f.read_text(encoding="utf-8"))
            except Exception:                          # noqa: BLE001
                return d, None
    return None, None


def _read_manifest(root):
    f = Path(root) / "modmind.project.json"
    if not f.exists():
        return None, f
    try:
        return json.loads(f.read_text(encoding="utf-8")), f
    except Exception as exc:                           # noqa: BLE001
        return {"_parse_error": str(exc)}, f


def cmd_project_status(args, cfg, out):
    cands = bridge_candidates(cfg)
    rows = []
    for p, why in cands:
        row = {"path": str(p), "why": why, "exists": p.exists()}
        if row["exists"]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                row["port"] = d.get("port")
                row["token_present"] = bool(d.get("token"))
                row["listening"] = _port_listening(d.get("port"))
                row["hiddenToolPrefixes"] = d.get("hiddenToolPrefixes")
            except Exception as exc:                   # noqa: BLE001
                row["error"] = str(exc)
        rows.append(row)
    live = [r for r in rows if r.get("listening")]
    root = cfg_path(cfg, "project_root") or Path(".")
    manifest, mpath = _read_manifest(root)
    status_file = root / ".modmind" / "external-agents" / "mcp-bridge.json"
    status = None
    if status_file.exists():
        try:
            status = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            pass
    ud, recent = _read_recent_projects()
    rep = {"project_root": str(root), "manifest_path": str(mpath),
           "manifest": manifest, "manifest_exists": mpath.exists(),
           "active_bridge": live[0]["path"] if live else None,
           "candidates": rows, "mcp_bridge_status": status,
           "recent_projects_file": str(ud / "recent-projects.json") if ud else None,
           "recent_projects": recent}

    lines = ["项目绑定状态",
             "  配置项目根 : %s (manifest %s)" % (root, "存在" if mpath.exists() else "缺失"),
             "  当前活桥   : %s" % (rep["active_bridge"] or "(没有活着的桥)")]
    if manifest and not manifest.get("_parse_error"):
        lines.append("  manifest   : name=%s namespace=%s loader=%s mc=%s"
                     % (manifest.get("name"), manifest.get("namespace"),
                        manifest.get("loader"), manifest.get("minecraftVersion")))
    if status:
        lines.append("  mcp-bridge : project=%s startedAt=%s"
                     % (status.get("projectPath"), status.get("startedAt")))
    lines.append("  候选 bridge.json:")
    for r in rows:
        lines.append("      %s  exists=%s listening=%s  (%s)"
                     % (r["path"], r["exists"], r.get("listening"), r["why"]))
    if recent:
        lines.append("  最近项目:")
        for item in recent[:6]:
            p = item.get("path") if isinstance(item, dict) else item
            lines.append("      %s" % p)
    out.result(rep, "\n".join(lines))


def cmd_project_bind(args, cfg, out):
    target = Path(str(args.path or cfg.get("project_root") or "")).resolve()
    if not str(args.path or cfg.get("project_root") or ""):
        die(EX_USAGE, "没给项目路径, 配置里也没有 project_root") 
    if not target.is_dir():
        die(EX_USAGE, "项目根不存在或不是目录: %s" % target)
    exe_path = cfg_path(cfg, "modmind_exe") or Path("")
    if exe_path.exists() and _under(target, exe_path.resolve().parent):
        die(EX_GUARD, "拒绝把 ModMind 安装目录当项目: %s" % target)
    if not exe_path.exists():
        out.debug("ModMind.exe 不存在, 跳过安装目录守卫 (dry-run 仍可用): %s" % exe_path)

    mpath = target / "modmind.project.json"
    created_manifest = False
    init_manifest = bool(getattr(args, "init_manifest", None))
    from_manifest = getattr(args, "from_manifest", None)
    dry_run = bool(getattr(args, "dry_run", None))
    if not mpath.exists():
        if not init_manifest:
            die(EX_USAGE,
                "%s 下没有 modmind.project.json, ModMind 认不出这个项目。" % target,
                "  说明: ModMind 的 readProjectInfo(root) 只认 <root>/modmind.project.json,",
                "        且会把 manifest 里的 path 强制覆盖成 <root>。",
                "  处置: 加 --init-manifest 从模板生成 (来源: %s),"
                % cfg.get("manifest_template"),
                "        或手工放一份进去。",
                "  注: 现有那份 manifest 在 mod_byqe79/ 下, 但它 path 指向的正是 %s —— "
                "位置不对, 需要复制过来。" % target)
        tpl_path = Path(str(from_manifest or cfg.get("manifest_template") or ""))
        if not str(tpl_path):
            die(EX_USAGE, "--init-manifest 需要 --from-manifest 或配置里的 manifest_template")
        if not tpl_path.exists():
            die(EX_USAGE, "找不到 manifest 模板: %s (用 --from-manifest 指定)" % tpl_path)
        tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
        tpl["path"] = str(target)
        tpl.setdefault("toolDataDirectory", ".modmind")
        if not getattr(args, "yes", None) and not dry_run:
            die(EX_GUARD, "要写入 %s, 加 --yes 确认 (可先 --dry-run)" % mpath)
        if dry_run:
            out.result({"would_write": str(mpath), "manifest": tpl, "dry_run": True},
                       "将写入 %s:\n%s" % (mpath, json.dumps(tpl, ensure_ascii=False, indent=1)))
            return
        mpath.write_text(json.dumps(tpl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        created_manifest = True
        out.info("已写入 manifest: %s" % mpath)

    # .modmind 一定会落在项目根下 (readProjectInfo 强制 toolDataDirectory=".modmind"),
    # 所以顺手确认它被 .gitignore 忽略, 否则会凭空多出一堆 untracked 文件。
    gi = target / ".gitignore"
    gi_text = gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else ""
    ignores = any(l.strip().rstrip("/") == ".modmind" for l in gi_text.splitlines())
    if not ignores:
        if getattr(args, "fix_gitignore", None) and not dry_run:
            gi.write_text(gi_text.rstrip("\n") + "\n.modmind/\n", encoding="utf-8")
            out.info("已在 %s 追加 .modmind/" % gi)
        else:
            out.info("提示: %s 没忽略 .modmind/ —— 绑定后工具数据目录会出现在 git status。"
                     "要自动追加就加 --fix-gitignore。" % gi)

    others = []
    for cand in bridge_candidates(cfg):
        if cand[0].exists() and cand[0] != _bridge_file(target):
            others.append(str(cand[0]))
    plan = {"target": str(target), "manifest": str(mpath),
            "bridge_will_be": str(_bridge_file(target)),
            "created_manifest": created_manifest,
            "argv": ["--mcp-bridge", "--project", str(target)],
            "stale_bridge_files": others}
    if dry_run:
        out.result({"plan": plan, "dry_run": True},
                   "将执行: ModMind.exe --mcp-bridge --project %s" % target)
        return

    out.info("请求 ModMind 绑定项目 (官方 --mcp-bridge --project):")
    out.info("  %s" % target)
    _launch_modmind(cfg, ["--mcp-bridge", "--project", str(target)], out)
    wait_s = getattr(args, "wait", None) or cfg.get("bind_wait_seconds", 45)
    data = _wait_bridge(target, wait_s, out)
    if not data:
        log_lines = modmind_recent_log(10)
        die(EX_NO_BRIDGE,
            "等待 %ss 后 %s 仍未出现 (或端口不通)。" % (wait_s, _bridge_file(target)),
            "常见原因:",
            "  1) ModMind 弹了对话框等确认 (项目版本迁移 / 首次信任) -> 去看窗口",
            "  2) manifest 内容不合法 -> 检查 modmind.project.json 的 loader/minecraftVersion",
            "  3) 路径在 ModMind 内部目录下, 被 assertAgentProjectAllowed 拒绝",
            "  4) 桥起来过又随应用退出被清掉 (ModMind 退出时会删 bridge.json)",
            "",
            "ModMind 侧最近日志:",
            *(["  " + l for l in log_lines] or ["  (读不到诊断日志)"]),
            "",
            "原始日志: %APPDATA%/modmind/logs/diagnostic-events.jsonl",
            "启动日志: %s/spawns/" % cfg["state_dir"])
    plan["port"] = data.get("port")
    plan["version"] = data.get("version")
    # 用新 bridge 做一次真实探针
    cfg2 = dict(cfg, _bridge=str(_bridge_file(target)))
    b = Bridge(cfg2, out, "http", False, cfg.get("_timeout"))
    probe = b.call("mcp_probe", {})
    plan["probe"] = probe
    info = b.call("project_info", {})
    plan["project_info"] = info
    out.result(plan,
               "\n".join([
                   "绑定成功",
                   "  项目根   : %s" % target,
                   "  bridge   : %s" % _bridge_file(target),
                   "  端口     : %s" % data.get("port"),
                   "  探针     : %s" % json.dumps(probe, ensure_ascii=False)[:200],
                   "  项目信息 : %s" % _truncate(json.dumps(info, ensure_ascii=False), 600)]))


def cmd_project_unbind(args, cfg, out):
    root = Path(str(args.path or cfg.get("project_root") or "")).resolve()
    _launch_modmind(cfg, ["--mcp-bridge-stop"], out)
    gone = _wait_bridge(root, getattr(args, "wait", None) or 20, out, want=False)
    out.result({"project_root": str(root), "stopped": True},
               "已请求停止桥接; %s %s" % (
                   _bridge_file(root), "已消失" if gone is not None else "仍在 (稍后再看)"))


# ---------------------------------------------------------------- main
def add_common(p, suppress=False):
    """公共开关。subparser 上必须用 SUPPRESS, 否则子命令的默认值会把
    'mmcli --json call ...' 这种写在前面的全局开关冲掉。"""
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("--config", default=d, help="配置文件路径 (默认 mmcli.config.json)")
    p.add_argument("--bridge", default=d, help="直接指定 bridge.json")
    p.add_argument("--json", action="store_true", default=d, help="机器可读输出")
    p.add_argument("-q", "--quiet", action="store_true", default=d)
    p.add_argument("-v", "--verbose", action="store_true", default=d)
    p.add_argument("--timeout", type=float, default=d, help="单次调用超时秒")
    p.add_argument("--lock-timeout", type=float, default=d)
    p.add_argument("--project-root", default=d)
    p.add_argument("--no-lock", action="store_true", default=d)
    p.add_argument("--serialize-all", action="store_true", default=d,
                   help="连读操作也加锁")
    p.add_argument("--allow-outside", action="store_true", default=d,
                   help="放行项目根之外的路径")
    p.add_argument("--allow-inside", action="store_true", default=d,
                   help="允许把输出写进项目树 (默认禁止)")
    p.add_argument("--max-chars", type=int, default=d, help="控制台输出截断长度")


def build_parser():
    p = argparse.ArgumentParser(
        prog="mmcli", description="ModMind 直连 CLI (HTTP /tool, 默认零子进程)")
    add_common(p)
    p.add_argument("--version", action="version", version="mmcli " + VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_text):
        s = sub.add_parser(name, help=help_text)
        add_common(s, suppress=True)
        return s

    s = add("init", "写出默认配置文件")
    s.add_argument("--force", action="store_true", default=None)

    add("doctor", "环境体检: 进程/端口/bridge.json/锁/孤儿目录")

    s = add("reap", "清理泄漏的 stdio 转发器进程 (默认 dry-run)")
    s.add_argument("--yes", action="store_true", default=None)
    s.add_argument("--dry-run", action="store_true", default=None)
    s.add_argument("--include-gui", action="store_true", default=None,
                   help="列表里连主进程/辅助进程一起显示 (只影响展示; 可杀集合恒为 kind=forwarder)")

    s = add("lock", "查看/清理锁")
    s.add_argument("--force-unlock", action="store_true", default=None)
    s.add_argument("--force", action="store_true", default=None)

    s = add("list", "列出 %d 个工具" % len(TOOLS))
    s.add_argument("--level", action="append", default=None,
                   help="read/readremote/safe/managed/unknown")
    s.add_argument("--readonly", action="store_true", default=None)
    s.add_argument("--writes", action="store_true", default=None)
    s.add_argument("--grep", default=None)

    s = add("actions", "打印 MCP名->action 全表")
    s.add_argument("--verify", action="store_true", default=None,
                   help="与 audit/tool_names.json 核对")

    s = add("describe", "打印某工具的原始定义 (从安装版 .mjs 抽取)")
    s.add_argument("name")

    add("explain", "打印协议真相与当前 bridge 状态")

    s = add("call", "调用工具")
    s.add_argument("name", help="MCP 工具名或 action 名")
    s.add_argument("--args", default=None, help="JSON 对象")
    s.add_argument("--args-file", default=None)
    s.add_argument("--set", action="append", default=None, help="k=v (可重复)")
    s.add_argument("--dry-run", action="store_true", default=None)
    s.add_argument("--raw", action="store_true", default=None)
    s.add_argument("--out", default=None, help="把返回 JSON 写到文件")
    s.add_argument("--yes", action="store_true", default=None)
    s.add_argument("--no-preflight", action="store_true", default=None)
    s.add_argument("--checkpoint", default=None, help="调用前先建同名 checkpoint")
    s.add_argument("--transport", choices=["http", "stdio"], default="http")
    s.add_argument("--allow-stdio", action="store_true", default=None)

    add("probe", "连通性自检 (mcp_probe)")
    add("preflight", "项目绑定校验")

    s = add("state", "Blockbench 工程状态")
    s.add_argument("--dry-run", action="store_true", default=None)
    s.add_argument("--raw", action="store_true", default=None)
    s.add_argument("--out", default=None)
    s.add_argument("--yes", action="store_true", default=None)
    s.add_argument("--no-preflight", action="store_true", default=None)
    s.add_argument("--checkpoint", default=None)
    s.add_argument("--transport", choices=["http", "stdio"], default="http")
    s.add_argument("--allow-stdio", action="store_true", default=None)

    s = add("capture", "离屏多视角截图 -> PNG 文件")
    s.add_argument("--views", action="append", default=None)
    s.add_argument("--width", type=int, default=512)
    s.add_argument("--height", type=int, default=512)
    s.add_argument("--out-dir", default=None)
    s.add_argument("--transport", choices=["http", "stdio"], default="http")
    s.add_argument("--allow-stdio", action="store_true", default=None)

    s = add("instance", "游戏实例 (PCL versions 目录下的实例)")
    isub = s.add_subparsers(dest="instance_cmd", required=True)
    si = isub.add_parser("list", help="列出实例与各自的 mods")
    add_common(si, suppress=True)
    si.add_argument("name", nargs="?", default=None)
    si = isub.add_parser("sync", help="把构建出来的 jar 装进实例 mods/ (自动备份旧 jar)")
    add_common(si, suppress=True)
    si.add_argument("name", nargs="?", default=None)
    si.add_argument("--jar", default=None, help="指定 jar (默认取 build/libs 里最新的)")
    si.add_argument("--no-backup", action="store_true", default=None)
    si.add_argument("--force", action="store_true", default=None,
                    help="目标内容相同时也重装")
    si.add_argument("--dry-run", action="store_true", default=None)

    s = add("launch", "启动真实游戏实例 (从 version json 重建命令, 不依赖 PCL GUI)")
    s.add_argument("--instance", default=None)
    s.add_argument("--launch", action="store_true", default=None, help="真的启动 (默认只重建并展示)")
    s.add_argument("--dry-run", action="store_true", default=None)
    s.add_argument("--quick-play", default=None, help="直接进入指定单人存档 (1.20+ 的 --quickPlaySingleplayer)")
    s.add_argument("--width", type=int, default=None)
    s.add_argument("--height", type=int, default=None)
    s.add_argument("--memory", type=int, default=None, help="最大堆 MB")
    s.add_argument("--username", default=None)
    s.add_argument("--allow-missing", action="store_true", default=None,
                   help="缺库时仍然启动 (默认拒绝)")
    s.add_argument("--compare-pcl", action="store_true", default=None,
                   help="与 PCL 的 LatestLaunch.bat 逐参数比对 (不需要真的启动)")
    s.add_argument("--strict", action="store_true", default=None, help="比对不一致就报错退出")

    s = add("shot", "截取窗口/游戏画面 -> PNG")
    s.add_argument("--window", default=None, help="窗口标题关键字 (默认 Minecraft)")
    s.add_argument("--process", default=None, help="按进程名找窗口 (优先于标题)")
    s.add_argument("--out", default=None)

    s = add("test", "ModMind 托管的真实客户端测试 (rendered + 原生截图)")
    tsub = s.add_subparsers(dest="test_cmd", required=True)
    def tadd(nm, hlp):
        sp = tsub.add_parser(nm, help=hlp)
        add_common(sp, suppress=True)
        sp.add_argument("--session", default=None, help="sessionId")
        sp.add_argument("--transport", choices=["http", "stdio"], default="http")
        return sp
    sp = tadd("capabilities", "探测支持的模式与后端")
    sp = tadd("start", "启动真实渲染客户端")
    sp.add_argument("--mode", choices=["rendered", "headless"], default="rendered")
    sp.add_argument("--username", default=None)
    sp.add_argument("--hidden", action="store_true", default=None)
    sp = tadd("state", "会话状态")
    sp = tadd("stop", "停止会话")
    sp = tadd("observe", "读 GUI / 日志 / 玩家世界状态")
    sp = tadd("shot", "原生截图")
    sp.add_argument("--out-dir", default=None)
    for nm, hlp, val in (("cmd", "在游戏里执行命令", "command"),
                         ("key", "按键 (如 key.keyboard.g)", "key"),
                         ("text", "输入文本", "text")):
        sp = tadd(nm, hlp)
        sp.add_argument("value", help=val)
        if nm == "text":
            sp.add_argument("--enter", action="store_true", default=None)
    sp = tadd("click", "在坐标点击 GUI")
    sp.add_argument("--x", type=int, required=True)
    sp.add_argument("--y", type=int, required=True)
    sp.add_argument("--button", type=int, default=None)
    sp = tadd("look", "转视角")
    sp.add_argument("--yaw", type=float, required=True)
    sp.add_argument("--pitch", type=float, default=0)
    sp = tadd("scenario", "跑一个场景脚本 (JSON 步骤数组)")
    sp.add_argument("--file", required=True)

    s = add("verify", "全链路: 构建 → 装机 → 启动 → 截图")
    s.add_argument("--instance", default=None)
    s.add_argument("--quick-play", default=None, help="启动后直接进这个存档")
    s.add_argument("--wait", type=float, default=25, help="启动后等多少秒再截图")
    s.add_argument("--skip-build", action="store_true", default=None)
    s.add_argument("--skip-sync", action="store_true", default=None)
    s.add_argument("--skip-launch", action="store_true", default=None)
    s.add_argument("--skip-shot", action="store_true", default=None)
    s.add_argument("--window", default=None)
    s.add_argument("--process", default=None)
    s.add_argument("--out", default=None)

    s = add("project", "项目绑定 (起桥并切换项目; 不触发 GUI 的版本迁移对话框)")
    psub = s.add_subparsers(dest="project_cmd", required=True)
    sp = psub.add_parser("status", help="当前绑定/候选 bridge/最近项目")
    add_common(sp, suppress=True)
    sp = psub.add_parser("bind", help="绑定并启动桥 (ModMind.exe --mcp-bridge --project)")
    add_common(sp, suppress=True)
    sp.add_argument("path", nargs="?", default=None)
    sp.add_argument("--init-manifest", action="store_true", default=None)
    sp.add_argument("--from-manifest", default=None)
    sp.add_argument("--wait", type=float, default=None)
    sp.add_argument("--dry-run", action="store_true", default=None)
    sp.add_argument("--yes", action="store_true", default=None)
    sp.add_argument("--fix-gitignore", action="store_true", default=None)
    sp = psub.add_parser("unbind", help="停止桥 (ModMind.exe --mcp-bridge-stop)")
    add_common(sp, suppress=True)
    sp.add_argument("path", nargs="?", default=None)
    sp.add_argument("--wait", type=float, default=None)

    s = add("bridge", "桥的开关/状态 (project 的别名)")
    bsub = s.add_subparsers(dest="bridge_cmd", required=True)
    for nm, hlp in (("start", "启动桥"), ("stop", "停止桥")):
        sb = bsub.add_parser(nm, help=hlp)
        add_common(sb, suppress=True)
        sb.add_argument("path", nargs="?", default=None)
        sb.add_argument("--wait", type=float, default=None)
        sb.add_argument("--yes", action="store_true", default=None)
        sb.add_argument("--fix-gitignore", action="store_true", default=None)
    sb = bsub.add_parser("status", help="状态")
    add_common(sb, suppress=True)

    s = add("import-geo", "导入原生 Bedrock geo.json (默认带 --verify 往返校验)")
    s.add_argument("--geo", required=True, help="geo.json 路径")
    s.add_argument("--texture", default=None, help="配套 PNG (会被注入并绑定)")
    s.add_argument("--geometry", default=None, help="几何索引或 identifier (文件含多份几何时)")
    s.add_argument("--name", default=None, help="工程名 (默认用 description.identifier)")
    s.add_argument("--checkpoint", default=None)
    s.add_argument("--no-preflight", action="store_true", default=None)
    s.add_argument("--skip-uv", action="store_true", default=None)
    s.add_argument("--verify", action="store_true", default=None,
                   help="用 Blockbench 自己的 compile() 反向导出并逐字段比对")
    s.add_argument("--verify-path", default=None, help="校验用导出路径 (项目相对)")
    s.add_argument("--dry-run", action="store_true", default=None)
    s.add_argument("--transport", choices=["http", "stdio"], default="http")
    s.add_argument("--allow-stdio", action="store_true", default=None)

    s = add("import-anim", "导入 Bedrock .animation.json (与 import-geo 同套翻译层)")
    s.add_argument("--anim", action="append", required=True, help=".animation.json (可重复)")
    s.add_argument("--name", action="append", default=None, help="只导入指定动画名 (可重复)")
    s.add_argument("--checkpoint", default=None)
    s.add_argument("--no-preflight", action="store_true", default=None)
    s.add_argument("--verify", action="store_true", default=None)
    s.add_argument("--dry-run", action="store_true", default=None)
    s.add_argument("--transport", choices=["http", "stdio"], default="http")
    s.add_argument("--allow-stdio", action="store_true", default=None)

    add("why-uv", "讲清 UV 怎么设 (含一次错误结论的更正与实测证据)")

    s = add("plugins", "插件工具 (目前唯一能从 CLI 调到的官方扩展点)")
    psub2 = s.add_subparsers(dest="plugins_cmd", required=True)
    sp2 = psub2.add_parser("list", help="列出已注册的插件工具")
    add_common(sp2, suppress=True)
    sp2.add_argument("--transport", choices=["http", "stdio"], default="http")
    sp2.add_argument("--allow-stdio", action="store_true", default=None)
    sp2 = psub2.add_parser("call", help="调用一个插件工具")
    add_common(sp2, suppress=True)
    sp2.add_argument("tool")
    sp2.add_argument("--args", default=None)
    sp2.add_argument("--set", action="append", default=None, help="k=v (可重复)")
    sp2.add_argument("--transport", choices=["http", "stdio"], default="http")
    sp2.add_argument("--allow-stdio", action="store_true", default=None)
    sp2 = psub2.add_parser("reload", help="重载插件后端")
    add_common(sp2, suppress=True)
    sp2.add_argument("--transport", choices=["http", "stdio"], default="http")
    sp2.add_argument("--allow-stdio", action="store_true", default=None)

    s = add("bind-texture", "把 PNG 作为 dataUrl 注入并绑定到 cube")
    s.add_argument("--png", required=True)
    s.add_argument("--name", default="texture")
    s.add_argument("--cubes", action="append", default=None)
    s.add_argument("--chunk", type=int, default=120)
    s.add_argument("--dry-run", action="store_true", default=None)
    s.add_argument("--no-preflight", action="store_true", default=None)
    s.add_argument("--checkpoint", default=None)
    s.add_argument("--transport", choices=["http", "stdio"], default="http")
    s.add_argument("--allow-stdio", action="store_true", default=None)
    return p


def opt(args, name, default=None):
    v = getattr(args, name, None)
    return default if v is None else v


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:                                 # noqa: BLE001
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(opt(args, "config"))
    if opt(args, "bridge"):
        cfg["_bridge"] = opt(args, "bridge")
    if opt(args, "project_root"):
        cfg["project_root"] = opt(args, "project_root")
    cfg["_transport"] = getattr(args, "transport", "http")
    cfg["_allow_stdio"] = bool(opt(args, "allow_stdio", False))
    cfg["_timeout"] = opt(args, "timeout")
    out = Out(as_json=bool(opt(args, "json", False)),
              quiet=bool(opt(args, "quiet", False)),
              verbose=bool(opt(args, "verbose", False)),
              max_chars=opt(args, "max_chars", 4000))

    if args.cmd == "shot":
        return cmd_shot(args, cfg, out) or EX_OK
    if args.cmd == "test":
        return cmd_test(args, cfg, out) or EX_OK
    if args.cmd == "verify":
        return cmd_verify(args, cfg, out) or EX_OK
    if args.cmd == "instance":
        return {"list": cmd_instance, "sync": cmd_instance}[args.instance_cmd](args, cfg, out) or EX_OK
    if args.cmd == "launch":
        return cmd_launch(args, cfg, out) or EX_OK
    if args.cmd == "plugins":
        return cmd_plugins(args, cfg, out) or EX_OK
    if args.cmd == "project":
        return {"status": cmd_project_status, "bind": cmd_project_bind,
                "unbind": cmd_project_unbind}[args.project_cmd](args, cfg, out) or EX_OK
    if args.cmd == "bridge":
        if args.bridge_cmd == "status":
            return cmd_project_status(args, cfg, out) or EX_OK
        if args.bridge_cmd == "start":
            return cmd_project_bind(args, cfg, out) or EX_OK
        return cmd_project_unbind(args, cfg, out) or EX_OK

    handler = {
        "init": cmd_init, "doctor": cmd_doctor, "reap": cmd_reap, "lock": cmd_lock,
        "list": cmd_list, "actions": cmd_actions, "describe": cmd_describe,
        "explain": cmd_explain, "call": cmd_call, "probe": cmd_probe,
        "preflight": cmd_preflight, "state": cmd_state, "capture": cmd_capture,
        "bind-texture": cmd_bind_texture, "import-geo": cmd_import_geo,
        "import-anim": cmd_import_anim, "why-uv": cmd_why_uv,
    }[args.cmd]
    return handler(args, cfg, out) or EX_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
