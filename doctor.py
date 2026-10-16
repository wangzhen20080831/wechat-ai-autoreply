# -*- coding: utf-8 -*-
"""环境自检：一条命令告诉你「哪里还没通」。

    python doctor.py            全部检查（包含一次真实的 DeepSeek 调用）
    python doctor.py --no-net   跳过联网测试

检查顺序就是出问题的概率顺序：Python 位数 → 依赖 → 配置 → Key → 微信 → 白名单。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    os.system("chcp 65001 >nul 2>&1")
except Exception:
    pass
try:
    # line_buffering=True：wechatauto 导入时会丢掉未 flush 的输出缓冲
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except AttributeError:
    pass

OK = "✅"
BAD = "❌"
WARN = "⚠️ "

_problems: list = []


def check(title: str, ok: bool, detail: str = "", fatal_fix: str = "") -> bool:
    mark = OK if ok else BAD
    print("%s %s" % (mark, title))
    if detail:
        for line in str(detail).splitlines():
            print("      " + line)
    if not ok and fatal_fix:
        print("      → %s" % fatal_fix)
        _problems.append("%s：%s" % (title, fatal_fix))
    return ok


def warn(title: str, detail: str = "") -> None:
    print("%s %s" % (WARN, title))
    if detail:
        for line in str(detail).splitlines():
            print("      " + line)


def section(name: str) -> None:
    print("\n" + "─" * 64)
    print("  " + name)
    print("─" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="环境自检")
    parser.add_argument("--no-net", action="store_true", help="跳过 DeepSeek 联网测试")
    parser.add_argument("--timeout", type=int, default=30, help="联网测试超时秒数")
    args = parser.parse_args()

    print("DeepSeek × 微信 自动回复 · 环境自检")
    print("项目目录：%s" % ROOT)

    # ------------------------------------------------------------ 1 Python
    section("1/5 Python 环境")
    bits = struct.calcsize("P") * 8
    check("Python 位数 = %d 位" % bits, bits == 64,
          "解释器：%s" % sys.executable,
          "必须是 64 位 Python。32 位读不到微信进程内存里的数据库密钥。")
    check("Python 版本 = %s" % sys.version.split()[0], sys.version_info >= (3, 9),
          "", "需要 Python 3.9 及以上。")

    # ------------------------------------------------------------ 2 依赖
    section("2/5 依赖库")
    wechatauto_ok = False
    try:
        import wechatauto                                    # noqa: F401
        from wechatauto import __version__ as wa_version
        wechatauto_ok = True
        check("wechatauto-replica 已安装（%s）" % wa_version, True)
    except Exception as exc:
        check("wechatauto-replica 已安装", False, str(exc),
              "在项目目录执行：.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    if wechatauto_ok:
        for mod in ("uiautomation", "pyperclip", "winsdk", "zstandard", "psutil"):
            try:
                __import__(mod)
                print("   %s %s" % (OK, mod))
            except Exception as exc:
                print("   %s %s（%s）" % (WARN, mod, exc))
        # 通知材料库的增强项：缺了照样能跑（读不了 PDF、中文分词糙一点）
        missing = []
        for mod in ("jieba", "openpyxl", "pypdf"):
            try:
                __import__(mod)
            except Exception:
                missing.append(mod)
        if missing:
            print("   %s 通知材料库少了 %s" % (WARN, "、".join(missing)))
            print("      影响：PDF 读不了、xlsx 解析退化、中文分词更糙。装上更好：")
            print("      .venv\\Scripts\\python.exe -m pip install %s" % " ".join(missing))
        else:
            print("   %s jieba / openpyxl / pypdf（通知材料库增强）" % OK)

    # ------------------------------------------------------------ 3 配置
    section("3/5 配置文件")
    from dsbot import load_config

    if not (ROOT / "config.json").is_file():
        warn("还没有 config.json",
             "正在用默认配置。先执行：copy config.example.json config.json")
    cfg = load_config(ROOT)
    check("配置文件读取成功", True, "模型：%s\n接口：%s\n白名单模式：%s" % (
        cfg.model.name, cfg.model.base_url, cfg.trigger.mode))

    if not cfg.model.api_key or "填入" in cfg.model.api_key:
        check("DEEPSEEK_API_KEY 已填写", False,
              "读到的值：%s" % (cfg.model.api_key or "（空）"),
              "复制 .env.example 为 .env，填入 sk- 开头的 Key（platform.deepseek.com/api_keys）")
    else:
        masked = cfg.model.api_key[:6] + "…" + cfg.model.api_key[-4:] if len(cfg.model.api_key) > 12 else "（太短）"
        check("DEEPSEEK_API_KEY 已填写", True, "读到：%s" % masked)

    if cfg.trigger.mode == "whitelist" and not cfg.trigger.whitelist:
        check("白名单不是空的", False, "",
              "白名单为空时谁都不会回（这是故意的安全设计）。先跑 python main.py --list-sessions 挑人。")
    elif cfg.trigger.mode == "whitelist":
        check("白名单共 %d 人" % len(cfg.trigger.whitelist), True, "、".join(cfg.trigger.whitelist))

    # 通知材料库（读文件回答通知类问题）
    if not cfg.knowledge.enabled:
        warn("通知材料库已关闭（knowledge.enabled = false）",
             "同学问「体测几点在哪集合」这类问题，AI 就只能自己发挥。")
    else:
        kdb = cfg.abspath(cfg.knowledge.db_path)
        if not kdb.is_file():
            warn("通知材料库还没建索引（%s）" % cfg.knowledge.db_path,
                 "跑一次 run.bat --index，它会把 knowledge\\ 和微信收到的文件读进库。")
        else:
            try:
                import sqlite3

                from dsbot.knowledge import Tokenizer

                conn = sqlite3.connect(str(kdb))
                try:
                    docs, chars = conn.execute(
                        "SELECT COUNT(*), COALESCE(SUM(chars),0) FROM docs").fetchone()
                finally:
                    conn.close()
                if docs:
                    check("通知材料库：%d 份材料、约 %d 万字" % (docs, chars // 10000), True,
                          "分词方式：%s\n看库里有什么：.venv\\Scripts\\python.exe tools\\index_notices.py --list\n"
                          "试一句话能不能搜到：… index_notices.py --search 体测" % Tokenizer().name)
                else:
                    warn("通知材料库是空的",
                         "把通知文件丢进 knowledge\\ 再跑 run.bat --index。")
            except Exception as exc:
                warn("读通知材料库失败", "%r" % (exc,))

        # 扫描件 OCR：打印后拍照/扫描的 PDF 没有文字层，靠 Windows 自带 OCR 兜底
        if not cfg.knowledge.ocr.enabled:
            print("   · 扫描件 OCR 已关闭（knowledge.ocr.enabled=false），扫描件会被跳过")
        else:
            try:
                from dsbot.knowledge import ocr_available

                usable, why = ocr_available(cfg.knowledge.ocr.lang)
            except Exception as exc:
                usable, why = False, "调用失败：%r" % (exc,)
            if usable:
                print("   %s 扫描件 OCR：%s" % (OK, why))
            else:
                warn("扫描件 OCR 用不了",
                     "%s\n影响：打印后拍照/扫描的通知（没有文字层的 PDF）读不出文字，会被跳过。\n"
                     "解决：设置 → 时间和语言 → 语言和区域 → 中文(简体) → 语言选项 →"
                     "在「光学字符识别」那一项点安装。" % why)

    # ------------------------------------------------------------ 4 大模型
    section("4/5 DeepSeek 连通性")
    if args.no_net:
        warn("已跳过（--no-net）")
    elif not cfg.model.api_key or "填入" in cfg.model.api_key:
        check("调用 DeepSeek", False, "", "先填好 Key 再测。")
    else:
        from dsbot import DeepSeekClient
        try:
            client = DeepSeekClient(
                api_key=cfg.model.api_key, base_url=cfg.model.base_url,
                model=cfg.model.name, timeout=max(5, args.timeout),
            )
            reply = client.ping()
            check("调用 DeepSeek 成功（%s）" % cfg.model.name, True,
                  "它回了：%s\n计费：本次约 20 个 token" % reply.replace("\n", " "))
        except Exception as exc:
            check("调用 DeepSeek", False, str(exc),
                  "确认 Key、余额和网络（必要时设置 HTTPS_PROXY 走代理）。")

    # ------------------------------------------------------------ 5 微信
    section("5/5 微信客户端")
    if not wechatauto_ok:
        warn("依赖没装好，跳过微信检查")
    else:
        # 进程
        running = []
        try:
            import psutil
            for proc in psutil.process_iter(["name"]):
                name = (proc.info.get("name") or "").lower()
                if name in ("weixin.exe", "wechat.exe"):
                    running.append(" ".join([proc.info["name"], "pid=%d" % proc.pid]))
        except Exception as exc:
            warn("进程枚举失败：%s" % exc)
        check("微信进程在运行", bool(running), "\n".join(running) or "没找到 Weixin.exe / WeChat.exe",
              "先启动并登录 PC 微信（4.x）。")

        # 主窗口
        hwnd = 0
        try:
            import ctypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            for cls, title in (("Qt51514QWindowIcon", None), (None, "微信"),
                               ("WeChatMainWndForPC", None)):
                hwnd = user32.FindWindowW(cls, title)
                if hwnd:
                    break
        except Exception as exc:
            warn("窗口探测失败：%s" % exc)
        check("微信主窗口存在（hwnd=%s）" % (hwnd or "0"), bool(hwnd), "",
              "微信窗口被关掉了，或者还停在登录/扫码界面。")

        # 数据库
        if running:
            try:
                from dsbot.wechat import WeChatIO, list_accounts
                accounts = list_accounts()
                if len(accounts) > 1:
                    warn("这台机器上有 %d 个微信号的数据目录" % len(accounts),
                         "\n".join("%s%s" % (n, "（最近活跃）" if i == 0 else "（更早）")
                                   for i, (n, _) in enumerate(accounts))
                         + "\n程序默认读最近活跃的那个；要读另一个：main.py --account <目录名>")
                io = WeChatIO(account=cfg.wechat.account)
                info = io.connect()
                check("读取本地数据库成功", True, "账号：%s (%s)" % (
                    info.get("nick_name") or "?", info.get("username") or "?"))
                if cfg.wechat.account:
                    check("账号按配置锁定", (info.get("account_dir") or "") == cfg.wechat.account,
                          "配置里写的是 %s，实际读的是 %s" % (
                              cfg.wechat.account, info.get("account_dir") or "?"))
                sessions = io.list_sessions(limit=500)
                check("读到会话列表（%d 个）" % len(sessions), len(sessions) > 0)
                if cfg.trigger.mode == "whitelist" and cfg.trigger.whitelist:
                    # 白名单里的人可能从没跟你私聊过（还没有会话），所以判定范围
                    # 不能只看会话列表，要放进整个通讯录一起找。
                    contacts = io.name_map()
                    names = set()
                    for s in sessions:
                        username = str(s.get("username") or "")
                        names.add(username)
                        names.add(io.display_name(username))
                    for username, disp in contacts.items():
                        names.add(username)
                        names.add(disp)
                    missing = [w for w in cfg.trigger.whitelist if w not in names]
                    check("白名单里的名字都能对应上会话", not missing,
                          "对不上的：%s" % "、".join(missing) if missing else "",
                          "名字要和微信里显示的备注/昵称/群名完全一致；"
                          "也可以先跑 python main.py --list-sessions 看真实名字。")

                # 群聊 @ 我 用的名字（拿不到微信 @ 名单时的兜底）
                if cfg.trigger.groups != "ignore":
                    try:
                        groups = [str(s.get("username") or "") for s in sessions
                                  if str(s.get("username") or "").endswith("@chatroom")]
                        pairs = [(gid, io.my_group_name(gid)) for gid in groups]
                        pairs.sort(key=lambda item: (not item[1],))   # 有群昵称的排前面
                        lines = ["%s → %s" % (io.display_name(gid),
                                              gnick or "（没设群昵称，按微信昵称）")
                                 for gid, gnick in pairs[:8]]
                        check("群聊策略 = %s" % (
                            {"at": "at（被 @ 才回，@所有人 不算）",
                             "all": "all（群里全回）"}.get(cfg.trigger.groups, cfg.trigger.groups)),
                              True,
                              "别人 @ 你时用的名字（前几个群）：\n" + "\n".join(lines or ["（没发现群）"]))
                    except Exception as exc:
                        warn("群昵称读取失败：%s" % exc)
            except Exception as exc:
                check("读取本地数据库", False, str(exc),
                      "确认微信已登录；若提示密钥相关错误，直接退出微信重新登录再试。")

    # ------------------------------------------------------------ 总结
    section("结论")
    if _problems:
        print("还有 %d 个问题需要处理：" % len(_problems))
        for i, p in enumerate(_problems, 1):
            print("  %d) %s" % (i, p))
        print("\n处理完重新跑一次：python doctor.py")
        return 1
    print("全部通过。可以直接启动：")
    print("    run.bat           （或 .venv\\Scripts\\python.exe main.py）")
    print("\n第一次建议先用演练模式确认不会乱发：")
    print("    .venv\\Scripts\\python.exe main.py --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
