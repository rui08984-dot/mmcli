#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核验 mmcli_selftest.py 是不是真检查: 逐个植入已知坏行为, 看自检是否变红。

一个断言若在对应变异体下仍然全绿, 它就是恒真断言 —— 那样的检查等于没写。
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable

MUTATIONS = [
    ("M7  reap 默认就杀 (去掉 dry-run 闸门)",
     "    if args.dry_run or not args.yes:",
     "    if False:"),
    ("M8  doctor 永远报健康",
     '    rep["ok"] = not rep["problems"]',
     '    rep["ok"] = True'),
    ("M9  reap 连主进程一起当可杀对象",
     '    rows = [p for p in allp if p["kind"] == "forwarder"]   # 只可能杀转发器',
     "    rows = list(allp)"),
    ("M10 回到命令行子串判定转发器 (H1 原样)",
     '''    if len(rest) == 1:
        cand = rest[0]
        if (os.path.basename(cand) == "modmind-mcp-server.mjs"
                and os.path.basename(os.path.dirname(cand)) == "external-agents"):
            return "forwarder", None''',
     '''    if "modmind-mcp-server.mjs" in (command_line or ""):
        return "forwarder", None'''),
    ("M11 路径守卫退回旧键名集合 (漏 paths/otherPath)",
     '''PATH_KEYS = {
    "path", "paths", "relativepath", "relativepaths", "projectpath", "outputdir",
    "outputdirectory", "targetpath", "otherpath", "sourcepath", "file", "files",
    "dir", "directory", "directories", "folder", "folders", "location", "locations",
}''',
     'PATH_KEYS = {"path", "relativePath", "projectPath", "outputDirectory", "targetPath"}'),
    ("M12 去掉 x-modmind-token 头",
     'headers = {"Content-Type": "application/json", "x-modmind-token": info["token"]}',
     'headers = {"Content-Type": "application/json"}'),
    ("M13 锁键退回哈希客户端字符串",
     '    return "mmcli-" + hashlib.sha1(lock_identity(cfg).encode("utf-8")).hexdigest()[:12]',
     '    return "mmcli-" + hashlib.sha1(str(cfg["project_root"]).encode("utf-8")).hexdigest()[:12]'),
    ("M14 关掉 --out 的项目树闸门",
     "        outp = guard_output(args.out, cfg, out, bool(opt(args, \"allow_inside\")), \"--out 目标\")",
     "        outp = Path(args.out).resolve()"),
]

SCRATCH = Path(tempfile.mkdtemp(prefix="mmcli-mutation-"))


def run(d, label):
    p = subprocess.run([PY, str(d / "mmcli_selftest.py")], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=900)
    out = p.stdout
    m = re.search(r"总计 (\d+), 通过 (\d+), 跳过 (\d+), 失败 (\d+)", out)
    if not m:
        return label, None, out[-500:]
    total, ok, skip, fail = (int(x) for x in m.groups())
    return label, (total, ok, skip, fail), out


def stage(d):
    """把 mmcli.py + 自检 + audit 数据放进一个独立目录 (自检按脚本所在目录找这些)。

    只拷自检真正需要的那一个文件, 不整目录拷贝 —— 否则别的进程(并发 agent)
    正在同一目录里建删文件时, copytree 会撞上 WinError 2 而整个测试崩掉。
    """
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "mmcli_selftest.py", d / "mmcli_selftest.py")
    src = HERE / "audit" / "tool_names.json"
    if src.exists():
        (d / "audit").mkdir(exist_ok=True)
        shutil.copy2(src, d / "audit" / "tool_names.json")


base = SCRATCH / "baseline"
stage(base)
shutil.copy2(HERE / "mmcli.py", base / "mmcli.py")

label, res, _ = run(base, "baseline (未变异)")
print("对照组: %s -> %s" % (label, res))
if not res or res[3] != 0:
    print("对照组本身就没通过, 变异测试无意义")
    raise SystemExit(1)

print()
print("%-46s %s" % ("变异体", "自检结果"))
print("-" * 78)
caught, missed = [], []
for i, (name, old, new) in enumerate(MUTATIONS):
    d = SCRATCH / ("m%d" % i)
    stage(d)
    src = (HERE / "mmcli.py").read_text(encoding="utf-8")
    if old not in src:
        print("%-46s SKIP (找不到锚点, 代码已变)" % name)
        missed.append(name)
        continue
    (d / "mmcli.py").write_text(src.replace(old, new, 1), encoding="utf-8")
    label, res, tail = run(d, name)
    if res is None:
        print("%-46s 崩溃/无输出" % name)
        caught.append(name)
    elif res[3] > 0:
        print("%-46s 变红 (失败 %d) ✔ 是真检查" % (name, res[3]))
        caught.append(name)
    else:
        print("%-46s 仍全绿 ✘ 恒真断言!" % name)
        missed.append(name)

print("-" * 78)
print("被抓住 %d / %d" % (len(caught), len(caught) + len(missed)))
if missed:
    print("漏网: ")
    for m in missed:
        print("  - " + m)
shutil.rmtree(SCRATCH, ignore_errors=True)
raise SystemExit(1 if missed else 0)
