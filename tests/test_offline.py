# -*- coding: utf-8 -*-
"""离线自测：不联网、不碰微信，验证大脑部分的逻辑。

    .venv\\Scripts\\python.exe tests\\test_offline.py

装了 pytest 的话也可以直接 pytest tests/。
"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dsbot import Knowledge, Memory, MockLLM, Policy, ReplyEngine, load_config
from dsbot.config import QuietHours, ReplyConfig, TriggerConfig
from dsbot.engine import clean_reply
from dsbot.policy import _in_quiet_hours

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass


def test_clean_reply():
    assert clean_reply("  「好的，我看看。」  ") == "好的，我看看。"
    assert clean_reply("**在的**") == "在的"
    assert clean_reply("assistant: 嗯嗯") == "嗯嗯"
    assert clean_reply("") == ""
    long_text = "这是一句测试。" * 40
    out = clean_reply(long_text, max_chars=100)
    assert len(out) <= 100, len(out)
    assert out.endswith("。")
    no_punct = "啊" * 200
    assert len(clean_reply(no_punct, max_chars=50)) <= 50
    print("  OK clean_reply")


def test_quiet_hours():
    def at(hh, mm):
        return time.mktime(datetime.datetime(2026, 5, 20, hh, mm).timetuple())

    assert _in_quiet_hours(at(23, 45), "23:30", "07:30") is True
    assert _in_quiet_hours(at(3, 0), "23:30", "07:30") is True
    assert _in_quiet_hours(at(12, 0), "23:30", "07:30") is False
    assert _in_quiet_hours(at(13, 0), "12:00", "14:00") is True
    assert _in_quiet_hours(at(15, 0), "12:00", "14:00") is False
    print("  OK 免打扰时段（含跨零点）")


def make_policy(**kw):
    base = dict(mode="whitelist", whitelist=["张三"], groups="ignore", cooldown=0)
    base.update(kw)
    return Policy(TriggerConfig(**base), self_names=["小杨"])


def test_policy_basic():
    p = make_policy()
    assert p.decide(conv_id="wxid_a", conv_name="张三", text="在吗").allow
    assert not p.decide(conv_id="wxid_b", conv_name="李四", text="在吗").allow
    assert not p.decide(conv_id="wxid_a", conv_name="张三", text="在吗", is_self=True).allow
    assert not p.decide(conv_id="wxid_a", conv_name="张三", text="在吗", kind="image").allow
    assert not p.decide(conv_id="wxid_a", conv_name="张三", text="   ").allow
    empty = make_policy(whitelist=[])
    assert not empty.decide(conv_id="wxid_a", conv_name="张三", text="在吗").allow
    print("  OK 白名单 / 自己发的 / 非文本 / 空消息")


def test_policy_groups():
    ignore = make_policy(groups="ignore")
    assert not ignore.decide(conv_id="123@chatroom", conv_name="同事群",
                             text="@小杨 在吗", is_group=True).allow
    # 群也要显式写进白名单才生效——避免"加个群就开始乱回"
    at_only = make_policy(groups="at", whitelist=["同事群"])
    assert at_only.decide(conv_id="123@chatroom", conv_name="同事群",
                          text="@小杨 帮我看看", is_group=True).allow
    assert not at_only.decide(conv_id="123@chatroom", conv_name="同事群",
                              text="今天天气不错", is_group=True).allow
    assert not at_only.decide(conv_id="456@chatroom", conv_name="别的群",
                              text="@小杨 帮我看看", is_group=True).allow
    d = at_only.decide(conv_id="123@chatroom", conv_name="同事群",
                       text="@小杨 帮我看看", is_group=True)
    assert d.allow and "帮我看看" in d.text and "@小杨" not in d.text
    all_in = make_policy(groups="all", whitelist=["同事群"])
    assert all_in.decide(conv_id="123@chatroom", conv_name="同事群",
                         text="随便说点什么", is_group=True).allow
    print("  OK 群聊 ignore / at / all，并正确剥离 @我")


def test_policy_group_at_exact():
    """群里 @ 判定：优先用微信记的 @ 名单（wxid），重名也不会认错。"""
    p = Policy(
        TriggerConfig(mode="whitelist", whitelist=["班群"], groups="at", cooldown=0),
        self_names=["我的微信昵称"],
        self_wxid="wxid_me",
    )
    # 微信 @ 名单里有我 —— 正文里连 "@我" 都没有，也要回
    d = p.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                 text="已改", is_group=True, at_usernames=["wxid_me"])
    assert d.allow and d.text == "已改", d

    # @所有人 不算 @ 我
    d = p.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                 text="@所有人\u2005今晚交作业", is_group=True, at_usernames=["notify@all"])
    assert not d.allow and "所有人" in d.reason, d

    # 有人 @ 一个跟我同名的同学：名单里没有我，就只能不回（不然会回错人）
    d = p.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                 text="@我的昵称\u2005帮我交一下", is_group=True, at_usernames=["wxid_other"])
    assert not d.allow, d

    # 我和别人一起被 @ —— 要回，并且把我那半截 @ 剥掉
    d = p.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                 text="@我的昵称 @我的微信昵称 你俩看一下", is_group=True,
                 at_usernames=["wxid_other", "wxid_me"], alias_names=["我的昵称"])
    assert d.allow and "看一下" in d.text and "@" not in d.text, d

    # 白名单按「谁 @ 我」算：名单里的人是 甲，不是这个群 → 校外的人 @ 我不回
    only_jia = Policy(
        TriggerConfig(mode="whitelist", whitelist=["甲"], groups="at", cooldown=0),
        self_names=["我的微信昵称"], self_wxid="wxid_me",
    )
    assert only_jia.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                           text="在吗", is_group=True, at_usernames=["wxid_me"]).allow
    d = only_jia.decide(conv_id="1@chatroom", conv_name="班群", sender_name="校外的人",
                        text="加我一下", is_group=True, at_usernames=["wxid_me"])
    assert not d.allow and "白名单" in d.reason, d
    # 反过来：把群名本身放进白名单 = 这个群里谁 @ 我都回
    d = p.decide(conv_id="1@chatroom", conv_name="班群", sender_name="校外的人",
                 text="加我一下", is_group=True, at_usernames=["wxid_me"])
    assert d.allow, d

    # 拿不到 @ 名单（老消息 / 解不开）时退回按名字判断
    d = p.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                 text="@我的微信昵称 在吗", is_group=True, at_usernames=None)
    assert d.allow and d.text == "在吗", d

    # 同一个人的第二个号被 @ —— 在 extra_at_ids 里登记过才算我
    two = Policy(
        TriggerConfig(mode="whitelist", whitelist=["班群"], groups="at", cooldown=0,
                      extra_at_ids=["wxid_my_second"]),
        self_names=["我的微信昵称"], self_wxid="wxid_me",
    )
    d = two.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                   text="帮我交一下", is_group=True, at_usernames=["wxid_my_second"])
    assert d.allow, d
    d = p.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                 text="帮我交一下", is_group=True, at_usernames=["wxid_my_second"])
    assert not d.allow, d

    # 有人只是手打「@我的昵称」而没从列表里选：默认不回，开了兜底才回
    d = p.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                 text="@我的昵称 帮我交一下", is_group=True, at_usernames=[],
                 alias_names=["我的昵称"])
    assert not d.allow, d
    loose = Policy(
        TriggerConfig(mode="whitelist", whitelist=["班群"], groups="at", cooldown=0,
                      at_text_fallback=True),
        self_names=["我的微信昵称"], self_wxid="wxid_me",
    )
    d = loose.decide(conv_id="1@chatroom", conv_name="班群", sender_name="甲",
                     text="@我的昵称 帮我交一下", is_group=True, at_usernames=[],
                     alias_names=["我的昵称"])
    assert d.allow and d.text == "帮我交一下", d
    print("  OK 群 @ 精确判定（wxid 名单 / @所有人 / 重名 / 白名单）")


def test_policy_group_nickname():
    """群里 @ 用的是**群昵称**，不是微信昵称——认不出来就永远不回。"""
    p = Policy(
        TriggerConfig(mode="whitelist", whitelist=["班群"], groups="at", cooldown=0),
        self_names=["我的微信昵称"],
        self_wxid="wxid_me",
    )
    d = p.decide(conv_id="1@chatroom", conv_name="班群", text="@我的昵称\u2005已改",
                 is_group=True, alias_names=["我的昵称"])
    assert d.allow and d.text == "已改", d
    # 群昵称要靠调用方传进来；不传就该认不出来（说明 alias_names 这条路真的在用）
    d = p.decide(conv_id="1@chatroom", conv_name="班群", text="@我的昵称\u2005已改", is_group=True)
    assert not d.allow, d
    # 配置里的 at_names 和微信昵称仍然认
    p2 = Policy(
        TriggerConfig(mode="whitelist", whitelist=["班群"], groups="at",
                      cooldown=0, at_names=["老王"]),
        self_names=["我的微信昵称"], self_wxid="wxid_me",
    )
    assert p2.decide(conv_id="1@chatroom", conv_name="班群", text="@老王 在吗",
                     is_group=True).allow
    print("  OK 群 @ 用群昵称也能认出来（at_names 照旧生效）")


def test_msgsource_at_list():
    """消息里的 @ 名单：source 是 zstd 压缩的 <msgsource>。"""
    from dsbot.wechat import decode_msgsource, extract_at_usernames, parse_at_usernames

    real = ('<msgsource>\n\t<atuserlist><![CDATA[wxid_myself]]></atuserlist>\n'
            '\t<pua>1</pua>\n\t<membercount>35</membercount>\n</msgsource>')
    assert parse_at_usernames(real) == ["wxid_myself"]
    # @所有人
    assert parse_at_usernames(
        "<msgsource><atuserlist>notify@all</atuserlist></msgsource>") == ["notify@all"]
    # 多个 wxid，逗号 / 空格 / 分号都见过
    assert parse_at_usernames(
        "<msgsource><atuserlist>wxid_a,wxid_b wxid_c;wxid_d</atuserlist></msgsource>"
    ) == ["wxid_a", "wxid_b", "wxid_c", "wxid_d"]
    # 没人被 @
    assert parse_at_usernames("<msgsource><signature>xx</signature></msgsource>") == []
    assert parse_at_usernames("") == []

    # 压缩态（微信真实存储形式）
    try:
        import zstandard
        blob = zstandard.ZstdCompressor().compress(real.encode("utf-8"))
        assert extract_at_usernames(blob) == ["wxid_myself"]
        assert extract_at_usernames(zstandard.ZstdCompressor().compress(b"<msgsource/>")) == []
    except ImportError:
        print("  （没装 zstandard，跳过压缩态用例）")

    # 拿不到名单时要返回 None，让上层退回按名字猜；空列表才是「确实没 @ 人」
    assert extract_at_usernames(b"") is None
    assert extract_at_usernames(None) is None
    assert extract_at_usernames(b"\x00\x01\x02\x03") is None
    assert decode_msgsource("not xml") is None
    print("  OK 消息 @ 名单解析（含 zstd 压缩态）")


def test_policy_guards():
    p = make_policy(blacklist=["李四"], ignore_keywords=["别回"])
    assert not p.decide(conv_id="wxid_a", conv_name="张三", text="别回了").allow
    assert not p.decide(conv_id="wxid_c", conv_name="李四", text="在吗").allow
    pref = make_policy(require_prefix="AI")
    assert not pref.decide(conv_id="wxid_a", conv_name="张三", text="你好").allow
    d = pref.decide(conv_id="wxid_a", conv_name="张三", text="AI 帮我写句话")
    assert d.allow and d.text == "帮我写句话"
    quiet = Policy(
        TriggerConfig(mode="whitelist", whitelist=["张三"], cooldown=0,
                      quiet_hours=QuietHours(enabled=True, start="00:00", end="23:59")),
        self_names=["小杨"],
    )
    assert not quiet.decide(conv_id="wxid_a", conv_name="张三", text="在吗").allow
    print("  OK 黑名单 / 忽略词 / 前缀 / 免打扰")


def test_policy_cooldown():
    p = make_policy(cooldown=30)
    now = 1000000.0
    assert p.decide(conv_id="wxid_a", conv_name="张三", text="在吗", now_ts=now).allow
    p.note_reply("wxid_a", ts=now)
    assert not p.decide(conv_id="wxid_a", conv_name="张三", text="还在吗", now_ts=now + 5).allow
    assert p.decide(conv_id="wxid_a", conv_name="张三", text="还在吗", now_ts=now + 31).allow
    print("  OK 冷却时间")


def test_memory():
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(Path(tmp) / "m.sqlite3", max_turns=3, ttl_hours=0)
        assert mem.history("A") == []
        mem.append("A", "user", "你好")
        mem.append("A", "assistant", "在的")
        mem.append("B", "user", "另一个人的消息")
        hist = mem.history("A")
        assert [m["content"] for m in hist] == ["你好", "在的"], hist
        assert [m["content"] for m in mem.history("B")] == ["另一个人的消息"]
        for i in range(10):
            mem.append("A", "user", "u%d" % i)
            mem.append("A", "assistant", "a%d" % i)
        hist = mem.history("A")
        assert len(hist) <= 6, len(hist)
        assert hist[-1]["content"] == "a9"
        mem.mark_reply("A", ts=12345.0)
        assert mem.last_reply_at("A") == 12345.0
        mem.clear("A")
        assert mem.history("A") == []
        mem.close()
    print("  OK 记忆：会话隔离 / 轮数裁剪 / 冷却时间戳")


def test_memory_ttl():
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(Path(tmp) / "m.sqlite3", max_turns=5, ttl_hours=1)
        mem.append("A", "user", "很久以前")
        old = time.time() - 4 * 3600
        mem._conn.execute("UPDATE messages SET ts=? WHERE conv_id='A'", (old,))
        mem._conn.commit()
        assert mem.history("A") == []
        mem.close()
    print("  OK 记忆过期")


def test_engine_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = load_config(ROOT)
        cfg.reply = ReplyConfig(delay_min=0, delay_max=0, max_reply_chars=200)
        mem = Memory(Path(tmp) / "m.sqlite3", max_turns=6, ttl_hours=0)
        engine = ReplyEngine(cfg, MockLLM(), mem)
        r1 = engine.reply("wxid_a", "你好", conv_name="张三")
        assert r1.startswith("[offline]") and "你好" in r1
        msgs = engine.build_messages("wxid_a", "第二句", conv_name="张三")
        assert msgs[0]["role"] == "system"
        assert "张三" in msgs[0]["content"]
        assert [m["role"] for m in msgs[1:]] == ["user", "assistant", "user"]
        mem.close()
    print("  OK 端到端：人设 + 历史 + 本轮输入")


def test_config_loads():
    cfg = load_config(ROOT)
    assert cfg.model.name and cfg.model.base_url.startswith("http")
    assert cfg.memory.max_turns >= 1
    assert len(cfg.system_prompt()) > 20
    print("  OK 配置加载")


# ------------------------------------------------------------------ 通知材料库


def _write_docx(path, paragraphs):
    """手搓一个最小 docx（zip + word/document.xml），用来测解析，不装任何库。"""
    import zipfile

    xml = ("<?xml version='1.0' encoding='UTF-8'?>"
           "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
           "<w:body>"
           + "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p for p in paragraphs)
           + "</w:body></w:document>")
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("word/document.xml", xml)


def test_notice_extract():
    from dsbot.knowledge import Unsupported, extract_text

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        docx = tmp / "体测通知.docx"
        _write_docx(docx, ["体测通知", "集合地点：东一体育场", "时间：10月19日"])
        text, kind = extract_text(docx)
        assert kind == "docx"
        assert "东一体育场" in text and "10月19日" in text, repr(text[:120])
        assert "<w:" not in text, "XML 标签没清干净"

        txt = tmp / "通知.txt"
        txt.write_text("关于医保缴费的通知\n截止到11月30日。", encoding="utf-8")
        assert "11月30日" in extract_text(txt)[0]

        broken = tmp / "坏文件.docx"
        broken.write_bytes(b"this is not a zip")
        try:
            extract_text(broken)
        except Unsupported:
            pass
        else:
            raise AssertionError("坏 docx 应该抛 Unsupported，而不是静默返回空")

        try:
            extract_text(tmp / "照片.xyz")
        except Unsupported as exc:
            assert "不支持" in str(exc)
        else:
            raise AssertionError("不支持的格式应该抛 Unsupported")
    print("  OK 材料库：docx / txt 抽文本，坏文件明确报错")


def test_notice_privacy_gate():
    from dsbot.knowledge import roster_like_name, sensitive_reason

    # 身份证号 —— 一个都不放过
    assert sensitive_reason("姓名 张三 身份证号 130203199001011234") != ""
    assert sensitive_reason("证件号码 13020319900101123X") != ""
    # 手机号要成规模才拦：一两个是通知里的咨询电话，几十个才是名单
    few = "咨询电话 13800000001、13800000002"
    assert sensitive_reason(few) == ""
    many = " ".join("138000000%02d" % i for i in range(6))
    assert "手机号" in sensitive_reason(many)
    # 学号一类的长编号成规模出现 = 名单（学费账单库、考生名单那种）
    assert "名单" in sensitive_reason(" ".join("2026140401%02d" % i for i in range(25)))
    assert sensitive_reason("学号 202614040101，姓名 张三，请核对。") == ""
    # 正常通知别误伤
    assert sensitive_reason("体测通知：10月19日在东一体育场集合，请提前十分钟到。") == ""

    # 文件名像花名册的（只对微信自动收集的生效）
    assert roster_like_name("2026级新生身高体重无信息人员名单.xlsx") == "名单"
    assert roster_like_name("示例一班假期离校返校情况汇总表.xlsx") == "汇总表"
    assert roster_like_name("XX班级 学生住宿情况摸底核实.xlsx") == "摸底"
    for ok_name in ("华北理工大学2026年秋季《国家学生体质健康标准》学生测试通知.docx",
                    "关于2026级新生教材征订的通知.pdf",
                    "2026级校级劳动学分班级值日区安排表20260922.xlsx",
                    "第七届新生运动会秩序册.pdf",
                    "华北理工大学2026年入团积极分子及发展团员考试题库.docx"):
        assert roster_like_name(ok_name) == "", ok_name
    print("  OK 材料库：隐私闸门（身份证 / 手机号 / 学号名单 / 花名册文件名）")


def test_notice_chunking():
    from dsbot.knowledge import chunk_text

    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []
    body = "\n".join("第%d段：这是一句用来凑长度的通知内容。" % i for i in range(80))
    chunks = chunk_text(body, size=100, overlap=20)
    assert chunks, "长文本应该切成多块"
    assert all(len(c) <= 100 for c in chunks), max(len(c) for c in chunks)
    assert any("第79段" in c for c in chunks), "最后一段不能丢"
    assert len(chunks) > 1
    # 单段就超长时按 size 硬切，不能吐出超长块
    long_one = "啊" * 500
    assert all(len(c) <= 100 for c in chunk_text(long_one, size=100, overlap=10))
    print("  OK 材料库：切块（不超长 / 不丢尾段 / 硬切保护）")


def test_notice_index_and_search():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "knowledge"
        src.mkdir()
        (src / "体测通知.txt").write_text(
            "关于2026年秋季体测的通知\n集合地点：东一体育场。时间：10月19日至10月23日，"
            "请提前十分钟到足球场集合，项目为50米、800米。", encoding="utf-8")
        (src / "医保通知.txt").write_text(
            "关于医保缴费的通知\n缴费截止到11月30日，地点在曹妃甸区医保局，逾期不再受理。",
            encoding="utf-8")
        (src / "医保通知 - 副本.txt").write_text(
            (src / "医保通知.txt").read_text(encoding="utf-8"), encoding="utf-8")
        (src / "学生基础信息表.txt").write_text(
            "示例2601班学生基础信息表\n姓名 身份证号\n张三 130203199001011234\n"
            "李四 130203199002022345\n", encoding="utf-8")

        know = Knowledge(tmp / "k.sqlite3", dirs=[str(src)], root=tmp)
        try:
            report = know.index()
            assert report.added == 2, report.summary()
            reasons = " / ".join(why for _n, why in report.skipped)
            assert "身份证" in reasons, reasons
            assert "重复" in reasons, reasons

            hits = know.search("体测在哪儿集合")
            assert hits, "应该能搜到体测通知"
            assert "体测" in hits[0].title, [h.title for h in hits]
            assert "东一体育场" in hits[0].text
            assert all("医保" not in h.title for h in hits), "不该把医保通知凑进来"

            # 库里没有的主题，宁可搜不到，也不要硬凑一份不相关的
            assert know.search("出国留学签证怎么办") == []
            # 只有一两个实词的短问句也要能搜到
            one_word = know.search("医保")
            assert one_word and "医保" in one_word[0].title, [h.title for h in one_word]

            # 材料原文要能拼成给模型看的那段话
            ctx = know.context("体测在哪儿集合", k=2, max_chars=800, recent=5)
            assert "体测" in ctx and "东一体育场" in ctx

            # 没动过的文件不该每次启动重读一遍（名册 + 重复副本都记下来了）
            again = know.index()
            assert again.skipped_unchanged == 2, again.summary()
            assert again.skipped == [], again.skipped
            assert again.unchanged == 2, again.summary()

            # 文件从磁盘上没了，库里也要跟着少一份
            (src / "医保通知.txt").unlink()
            (src / "医保通知 - 副本.txt").unlink()
            assert know.index().removed == 1
            assert know.stats()["docs"] == 1
        finally:
            know.close()          # Windows 上库没关掉，临时目录就删不干净
    print("  OK 材料库：建库 / 去重 / 拦隐私 / 检索 / 删文件同步")


def test_notice_narrow_scope_keeps_docs():
    """只看一部分目录时，不在范围里的材料不能当成「文件被删了」清掉。

    （以前踩过：跑一次 index_notices.py --no-wechat，微信收来的材料全没了。）
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        local = tmp / "knowledge"
        wechat = tmp / "wechat_files"
        local.mkdir()
        wechat.mkdir()
        (local / "本地通知.txt").write_text(
            "关于教材领取的通知\n请各班班长统计本班人数后，到图书馆一楼教材科领取，"
            "截止9月20日，逾期不再办理。", encoding="utf-8")
        (wechat / "微信通知.txt").write_text(
            "关于医保缴费的通知\n缴费截止到11月30日，地点在曹妃甸区医保局，"
            "带身份证和学生证，逾期不再受理。", encoding="utf-8")

        both = Knowledge(tmp / "k.sqlite3", dirs=[str(local), str(wechat)], root=tmp)
        try:
            assert both.index().added == 2
            # 换成只扫本地目录（相当于 --no-wechat）：微信那份文件还在，就该留着
            narrow = Knowledge(tmp / "k.sqlite3", dirs=[str(local)], root=tmp)
            try:
                rep = narrow.index()
                assert rep.removed == 0, rep.summary()
                assert narrow.stats()["docs"] == 2, narrow.stats()
            finally:
                narrow.close()
            # 文件真的没了，才该从库里消失
            (wechat / "微信通知.txt").unlink()
            again = Knowledge(tmp / "k.sqlite3", dirs=[str(local), str(wechat)], root=tmp)
            try:
                assert again.index().removed == 1
                assert again.stats()["docs"] == 1
            finally:
                again.close()
        finally:
            both.close()
    print("  OK 材料库：缩小扫描范围不会清掉范围外的材料")


def test_engine_injects_material():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "knowledge"
        src.mkdir()
        (src / "体测通知.txt").write_text(
            "关于2026年秋季体测的通知\n集合地点：东一体育场。时间：10月19日，"
            "请提前十分钟到足球场集合，项目为50米和800米。", encoding="utf-8")
        cfg = load_config(ROOT)
        know = Knowledge(tmp / "k.sqlite3", dirs=[str(src)], root=tmp)
        mem = Memory(tmp / "m.sqlite3", max_turns=4, ttl_hours=0)
        try:
            know.index()
            engine = ReplyEngine(cfg, MockLLM(), mem, knowledge=know)
            system = engine.build_messages(
                "wxid_a", "体测在哪儿集合", conv_name="张三")[0]["content"]
            assert "手头有的通知材料" in system
            assert "东一体育场" in system
            # 转述规则必须写到提示词里，不然模型会把原文整段抄出去
            assert "不要自己推" in system and "确认一下" in system

            # 关掉材料库，提示词里不该多出这一段
            plain = ReplyEngine(cfg, MockLLM(), mem)
            assert "手头有的通知材料" not in plain.build_messages(
                "wxid_a", "体测在哪儿集合")[0]["content"]
        finally:
            mem.close()
            know.close()
    print("  OK 材料库：原文进提示词（关掉则不进）")


def test_message_parsing():
    from dsbot.wechat import is_group_chat, kind_of, strip_sender_prefix

    # 库里返回的 type 是中文字符串
    assert kind_of("文本") == "text"
    assert kind_of("系统消息") == "system"
    assert kind_of("文件/链接/卡片") == "file"
    assert kind_of(1) == "text"          # 老格式的数字也兼容
    assert kind_of("莫名其妙") == "other"

    # 群消息正文带发送者前缀，必须剥掉
    assert strip_sender_prefix("wxid_abc123:\n你好啊", "wxid_abc123") == "你好啊"
    assert strip_sender_prefix("wxid_abc123:\n你好啊") == "你好啊"
    assert strip_sender_prefix("你好啊", "wxid_abc123") == "你好啊"
    assert strip_sender_prefix("https://x.com/a:b\n后面", "") == "https://x.com/a:b\n后面"

    assert is_group_chat("123@chatroom") and not is_group_chat("wxid_x")
    print("  OK 微信消息解析：类型 / 群前缀 / 群判断")


def test_ocr_join_spaces():
    """Windows OCR 在每个汉字之间插空格，合回去；英文单词之间的空格不许动。"""
    from dsbot.knowledge import join_ocr_spaces

    assert join_ocr_spaces("以 升 大 学 生") == "以升大学生"
    assert join_ocr_spaces("东 一 体 育 场 ， 10 月 19 日") == "东一体育场，10月19日"
    assert join_ocr_spaces("Hello world 你好") == "Hello world 你好"
    assert join_ocr_spaces("") == ""
    assert join_ocr_spaces("第一行\n第 二 行") == "第一行\n第二行"   # 换行要留住
    print("  OK OCR：汉字间空格合回（英文空格与换行不动）")


def test_ocr_exts_and_availability():
    """关掉 OCR 就不该再收图片；开着才收。可用性检查要给出人话。"""
    from dsbot.knowledge import IMG_EXTS, ocr_available

    with tempfile.TemporaryDirectory() as tmp:
        plain = Knowledge(Path(tmp) / "a.sqlite3", dirs=[], root=Path(tmp))
        smart = Knowledge(Path(tmp) / "b.sqlite3",
                          dirs=[], root=Path(tmp), ocr={"enabled": True})
        try:
            assert not any(e in plain._exts for e in IMG_EXTS)
            assert all(e in smart._exts for e in IMG_EXTS)
        finally:
            plain.close()
            smart.close()

    usable, why = ocr_available()
    assert isinstance(usable, bool) and why, why
    if usable:
        assert "OCR" in why, why
    print("  OK OCR：开关决定收不收图片；可用性 = %s" % why)


def test_ocr_image_end_to_end():
    """画一张中文通知图，真的走一遍 OCR。Windows 上没有中文 OCR 就跳过。"""
    from dsbot.knowledge import ocr_available, ocr_image

    usable, why = ocr_available()
    if not usable:
        print("  SKIP OCR 端到端（%s）" % why)
        return
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  SKIP OCR 端到端（没装 Pillow，画不出测试图）")
        return
    font_path = Path(r"C:\Windows\Fonts\msyh.ttc")
    if not font_path.is_file():
        font_path = Path(r"C:\Windows\Fonts\simhei.ttf")
    if not font_path.is_file():
        print("  SKIP OCR 端到端（找不到中文字体）")
        return

    with tempfile.TemporaryDirectory() as tmp:
        pic = Path(tmp) / "体测通知.png"
        img = Image.new("RGB", (900, 340), "white")
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(str(font_path), 40)
        draw.text((30, 40), "2026年秋季体测的通知", font=font, fill="black")
        draw.text((30, 120), "集合地点：东一体育场", font=font, fill="black")
        draw.text((30, 200), "时间是10月19日到10月23日", font=font, fill="black")
        draw.text((30, 280), "请提前十分钟到足球场集合", font=font, fill="black")
        img.save(pic)

        text = ocr_image(pic)
        assert "体测" in text, text
        assert "东一体育场" in text, text
        assert "体 测" not in text and "东 一" not in text, text   # 汉字间的空格要合掉

        # 走完整索引：这张图应该能被收进库，并且搜得到
        know = Knowledge(Path(tmp) / "k.sqlite3", dirs=[tmp], root=Path(tmp),
                         ocr={"enabled": True, "trigger_chars": 60})
        try:
            report = know.index()
            assert report.added == 1, report.summary()
            hits = know.search("体测在哪儿集合")
            assert hits and "东一体育场" in hits[0].text, [h.title for h in hits]
        finally:
            know.close()
    print("  OK OCR 端到端：画一张图 → 认成文字 → 入库 → 搜得到")


def test_single_instance_lock():
    """单实例锁：第二个进程抢不到，第一个退出后能重新抢到。"""
    import os
    from dsbot.lockfile import InstanceLock

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot.lock"
        first = InstanceLock(path)
        assert first.acquire() is True, "第一个进程应该能拿到锁"
        assert first.holder_pid() == str(os.getpid()), first.holder_pid()

        second = InstanceLock(path)
        assert second.acquire() is False, "第二个进程不该拿到锁"
        assert second.holder_pid() == str(os.getpid()), "读到的应该是占锁者的 PID"

        first.release()
        third = InstanceLock(path)
        assert third.acquire() is True, "上一个退出后应该能重新拿到锁"
        third.release()
    print("  OK 单实例锁：重复启动会被拦住，退出后能重新启动")


def test_session_sync_export_and_pull():
    """共享记忆：同学侧对话推出去 / 会话写的班级记忆读回来（含隐私闸门）。"""
    from dsbot.session_sync import SessionSync

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shared = tmp / "shared-memory"
        mem = Memory(tmp / "m.sqlite3", max_turns=5, ttl_hours=0)
        sync = SessionSync(shared, mem,
                           name_of=lambda c: {"wxid_a": "张三"}.get(c, c))
        sync.prepare()

        # 还没有对话：导出 0 条，但目录和空文件要建好（会话那边随时能读）
        assert sync.export() == 0
        assert (shared / "dialog.md").is_file()
        assert (shared / "bot-status.md").is_file()

        mem.append("wxid_a", "user", "值日怎么排的")
        mem.append("wxid_a", "assistant", "按劳动区块分的")
        assert len(mem.since(0)) == 2
        assert sync.export() == 2
        assert sync.export() == 0, "同一条对话不该推第二遍"

        lines = (shared / "dialog.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2, lines
        md = (shared / "dialog.md").read_text(encoding="utf-8")
        assert "张三" in md and "值日怎么排的" in md and "按劳动区块分的" in md, md

        # 班长本人手动回的，要跟机器人回的分开写（不然会话以为那是机器人说的）
        mem.append("wxid_a", "assistant", "我问过老师了", origin="human")
        assert sync.export() == 1
        md = (shared / "dialog.md").read_text(encoding="utf-8")
        assert "班长本人回的：    我问过老师了" in md, md
        assert "机器人（以班长身份）：    按劳动区块分的" in md, md
        assert json.loads(lines[0])["conv_name"] == "张三", lines[0]
        assert json.loads(lines[0])["role"] == "user"
        assert json.loads(lines[1])["role"] == "assistant"

        # 会话写回的知识 —— 立刻能读到
        (shared / "knowledge.md").write_text("值日按 D3 楼劳动区块排。", encoding="utf-8")
        first = sync.pull()
        assert "D3 楼" in first
        assert sync.pull() == first, "内容没变要命中缓存"

        # 改了 —— 下一条回复就该用新的
        (shared / "knowledge.md").write_text("值日改按宿舍楼排。", encoding="utf-8")
        assert "宿舍楼" in sync.pull()

        # inbox 里追加的补充记忆也要收
        (shared / "inbox").mkdir(exist_ok=True)
        (shared / "inbox" / "2026-10-14-教材.md").write_text("教材下周三领。", encoding="utf-8")
        text = sync.pull()
        assert "宿舍楼" in text and "教材下周三领" in text, text

        # 隐私闸门：整份名册（25 处学号）会被整份拦下，不许进提示词
        (shared / "inbox" / "2026-10-14-名册.md").write_text(
            " ".join("2026140401%02d" % i for i in range(25)), encoding="utf-8")
        text2 = sync.pull()
        assert "2026140401" not in text2, text2
        assert sync.status()["refused"], sync.status()
        mem.close()
    print("  OK 共享记忆：对话推出去 / 班级记忆读回来（含改动即时生效 + 隐私闸门）")


def test_engine_injects_shared_memory():
    """共享班级记忆要进提示词；回复完自动把本轮对话推给会话。"""
    from dsbot.session_sync import SessionSync

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shared = tmp / "shared-memory"
        shared.mkdir()
        (shared / "knowledge.md").write_text(
            "值日按 D3 楼劳动区块排；教材领取待确认。", encoding="utf-8")
        cfg = load_config(ROOT)
        mem = Memory(tmp / "m.sqlite3", max_turns=4, ttl_hours=0)
        sync = SessionSync(shared, mem, log=None)
        sync.prepare()
        try:
            engine = ReplyEngine(cfg, MockLLM(), mem, sync=sync)
            system = engine.build_messages(
                "wxid_a", "值日怎么排的", conv_name="张三")[0]["content"]
            assert "班级记忆" in system and "D3 楼" in system, system[-400:]
            assert "我问一下再回你" in system, "没写的事要让它说去确认"

            # 关掉同步，提示词里不该多出这一段
            plain = ReplyEngine(cfg, MockLLM(), mem)
            assert "班级记忆" not in plain.build_messages(
                "wxid_a", "值日怎么排的")[0]["content"]

            # 回复一次 → 这轮对话应该已经同步给会话了
            engine.reply("wxid_a", "值日怎么排的", conv_name="张三")
            pushed = (shared / "dialog.jsonl").read_text(encoding="utf-8").strip().splitlines()
            assert len(pushed) == 2, pushed
            assert "值日怎么排的" in (shared / "dialog.md").read_text(encoding="utf-8")
        finally:
            mem.close()
    print("  OK 共享记忆：进提示词（关掉则不进）+ 回复后自动推送")


THREAD = "01a133fd-8202-7a12-affe-c03e59c800d0"


def _rollout_record(kind, payload, ts="2026-10-14T14:00:00.000Z"):
    return {"timestamp": ts, "type": kind, "payload": payload}


def _started(turn, trigger):
    return _rollout_record("event_msg", {
        "type": "task_started", "turn_id": turn,
        "turn_attribution": {"turn_trigger": trigger}})


def _user_msg(turn, text):
    return _rollout_record("response_item", {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}],
        "internal_chat_message_metadata_passthrough": {"turn_id": turn}})


def _done(text, ts="2026-10-14T14:00:00.000Z"):
    return _rollout_record("event_msg", {"type": "task_complete", "last_agent_message": text}, ts)


def _write_rollout(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_session_memory_reader():
    """直接读那个会话的记忆：真人消息 / 心跳汇报 / 自动注入 / 脱敏 / 增量。"""
    from dsbot.session_memory import SessionMemoryReader, mask_personal

    # 脱敏本身
    masked = mask_personal("身份证 450981200503201427，手机 19110175423，卡 6222021234567890123")
    for bad in ("450981200503201427", "19110175423", "6222021234567890123"):
        assert bad not in masked, masked
    assert "已隐去" in masked

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "sessions"
        rollout = root / "2026" / "10" / "14" / ("rollout-20261014T120000-%s.jsonl" % THREAD)
        _write_rollout(rollout, [
            _started("T1", "composer"),
            _user_msg("T1", "周三值日加个人：同学丙"),
            _done("加上了，周三那组现在是 5 个人：同学丁、同学己、同学甲、同学戊、同学丙"),
            # 定时任务那一轮：用户消息是自动注入的提示词，不该当成班长说的话
            _started("T2", "automation"),
            _user_msg("T2", "你是示例一班班长的微信助理，本次是定时检查微信。只做两件事……"),
            _done("微信客户端没开，我先不管了。<heartbeat><automation_id>automation</automation_id>"
                  "<decision>DONT_NOTIFY</decision><message>没有需要转发的新通知</message></heartbeat>"),
            # 环境上下文也是自动注入的
            _started("T3", "composer"),
            _user_msg("T3", "<environment_context>\n<cwd>C:\\x</cwd>\n</environment_context>"),
            _user_msg("T3", "她学号多少"),
            _done("同学丙，学号 202314040325，身份证 450981200503201427，手机 19110175423。"),
        ])
        reader = SessionMemoryReader(THREAD, sessions_root=root)
        text = reader.poll()

        assert "周三值日加个人" in text, text
        assert "周三那组现在是 5 个人" in text, text
        assert "你是示例一班班长的微信助理" not in text, "心跳的提示词不该进记忆"
        assert "environment_context" not in text, "环境上下文不该进记忆"
        assert "没有需要转发的新通知" in text, "心跳的结论要留下"
        assert "DONT_NOTIFY" in text, text
        # 隐私：号码必须被抹掉
        assert "450981200503201427" not in text and "19110175423" not in text, text
        assert "已隐去" in text
        assert reader.status()["masked"] >= 1

        # 增量：文件里追加新一轮，再 poll 要能读到，且不重复读老内容
        before = len(text)
        _write_rollout(rollout, [
            _started("T4", "composer"),
            _user_msg("T4", "值日地点改 HB401 了吗"),
            _done("对，值日地点是 HB401，值日之前要在楼下签到。"),
        ])
        text2 = reader.poll()
        assert "HB401" in text2, text2
        assert text2 != text and len(text2) >= before - 1
        assert text2.count("周三值日加个人") <= 1, "老内容不该被重复读进来"
        # T1 两问两答、T2 只留结论、T3 只留真人那句：第一轮 5 条；T4 再 +2
        assert reader.status()["entries"] == 7, reader.status()

        # 成规模名册：整条丢掉，不进提示词
        _write_rollout(rollout, [
            _started("T5", "composer"),
            _user_msg("T5", "把名册发我"),
            _done(" ".join("2026140401%02d" % i for i in range(25))),
        ])
        text3 = reader.poll()
        assert "2026140401" not in text3, text3
        assert reader.status()["dropped"] >= 1
    print("  OK 会话记忆：真人消息 / 心跳汇报 / 自动注入过滤 / 脱敏 / 增量读")


def test_session_memory_rides_along_with_sync():
    """会话记忆要跟着共享记忆一起进提示词（文件那份也要脱敏）。"""
    from dsbot.session_memory import SessionMemoryReader
    from dsbot.session_sync import SessionSync

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "sessions"
        rollout = root / "2026" / "10" / "14" / ("rollout-20261014T120000-%s.jsonl" % THREAD)
        _write_rollout(rollout, [
            _started("T1", "composer"),
            _user_msg("T1", "值日的事定下来了"),
            _done("定了：每周三换班，值日地点 HB401。"),
        ])
        shared = tmp / "shared-memory"
        shared.mkdir()
        # 文件那份里塞个手机号，验证也走脱敏
        (shared / "knowledge.md").write_text(
            "值日地点 HB401。应急联系人手机 19110175423。", encoding="utf-8")
        mem = Memory(tmp / "m.sqlite3", max_turns=4, ttl_hours=0)
        sync = SessionSync(shared, mem, session=SessionMemoryReader(THREAD, sessions_root=root))
        sync.prepare()
        try:
            text = sync.pull(force=True)
            assert "【班长助理会话最近的记忆】" in text, text
            assert "每周三换班" in text and "HB401" in text, text
            assert "19110175423" not in text, text
            assert sync.status()["session"]["entries"] == 2, sync.status()

            cfg = load_config(ROOT)
            engine = ReplyEngine(cfg, MockLLM(), mem, sync=sync)
            system = engine.build_messages("wxid_a", "值日怎么排", conv_name="张三")[0]["content"]
            assert "每周三换班" in system and "不要自己答应" in system, system[-500:]
        finally:
            mem.close()
    print("  OK 会话记忆：和共享目录一起进提示词（两边都脱敏）")


# ------------------------------------------------- 生成失败：待办 + 自己补回来
def test_memory_pending():
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(Path(tmp) / "m.sqlite3", max_turns=4, ttl_hours=0)
        assert mem.list_pending() == []
        mem.add_pending("wxid_a", "他在不", conv_name="同学甲", now=100.0)
        assert mem.stats()["pending"] == 1
        # 同一个会话又失败一次：并进同一行，重复的行不重复出现，attempts 累加
        mem.add_pending("wxid_a", "他在不\n咋没跟我说王武墨", conv_name="同学甲",
                        reason="请求超时", now=200.0)
        rows = mem.list_pending()
        assert len(rows) == 1, rows
        assert rows[0]["body"] == "他在不\n咋没跟我说王武墨", rows[0]["body"]
        assert rows[0]["attempts"] == 2, rows[0]
        assert rows[0]["reason"] == "请求超时"
        # 另一个会话单独一行
        mem.add_pending("wxid_b", "在吗", conv_name="兰博睿", now=300.0)
        assert len(mem.list_pending()) == 2
        assert [r["conv_id"] for r in mem.list_pending(conv_id="wxid_b")] == ["wxid_b"]
        # 空内容不记账
        assert mem.add_pending("wxid_c", "   ") == 0
        assert mem.drop_pending([r["id"] for r in mem.list_pending(conv_id="wxid_a")]) == 1
        assert mem.stats()["pending"] == 1
        mem.close()
    print("  OK 待办消息：同会话合并去重 / attempts 累加 / 单独删除")


class _FakeTimer:
    """假定时器：只记下来，不真的等 20 秒。"""

    def __init__(self, sec, fn, args=()):
        self.sec = sec
        self.fn = fn
        self.args = args
        self.daemon = False
        self.running = False

    def start(self):
        self.running = True

    def cancel(self):
        self.running = False


def test_retry_queue():
    from dsbot.retryqueue import RetryQueue

    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(Path(tmp) / "m.sqlite3", max_turns=4, ttl_hours=0)
        clock = [1000.0]
        timers: list = []
        sent: list = []
        q = RetryQueue(
            mem, retry_delay=20, max_retries=2, flush_seconds=120, max_age_hours=2,
            deliver=lambda cid, body, meta: sent.append((cid, body, meta)),
            timer=lambda sec, fn, args=(): (timers.append(_FakeTimer(sec, fn, args))
                                           or timers[-1]),
            now_fn=lambda: clock[0],
        )

        # 第一次失败：记成待办 + 排一个 20 秒后的重试
        assert q.note_failure("wxid_a", "他在不", conv_name="同学甲") == 1
        assert len(timers) == 1 and timers[0].sec == 20.0 and timers[0].running
        assert q.pending_for("wxid_a")[0]["body"] == "他在不"

        # 第二次失败：并成一条，再排一次
        clock[0] += 25
        assert q.note_failure("wxid_a", "咋没跟我说王武墨", conv_name="同学甲") == 2
        assert len(timers) == 2
        assert q.pending_for("wxid_a")[0]["body"] == "他在不\n咋没跟我说王武墨"

        # 第三次：超过 max_retries，不再排定时器，交给后台慢慢试
        clock[0] += 25
        assert q.note_failure("wxid_a", "行", conv_name="同学甲") == 3
        assert len(timers) == 2, "超过 max_retries 不该再排定时器"

        # 后台补投：这时才真的发出去
        clock[0] += 200
        assert q.flush(force=True) == 1
        assert sent and sent[0][0] == "wxid_a"
        assert "咋没跟我说王武墨" in sent[0][1]
        assert sent[0][2]["attempts"] == 3

        # 补回来之后清掉，不会重复发
        assert q.clear("wxid_a") == 1
        assert q.pending_for("wxid_a") == []
        assert q.flush(force=True) == 0

        # 越失败越慢：attempts=1 时间隔 max(20*2, 120)=120 秒，没到点不投
        mem.add_pending("wxid_b", "在吗", conv_name="我的昵称", now=clock[0])
        clock[0] += 40
        assert q.flush() == 0, "还没到 _gap 不该重投"
        clock[0] += 200
        assert q.flush() == 1
        q.clear("wxid_b")

        # 拖太久就放弃（记日志，不再留着）
        mem.add_pending("wxid_c", "老师问的事", conv_name="同学乙",
                        now=clock[0] - 3 * 3600)
        assert q.flush(force=True) == 0, "过期的不该投"
        assert q.pending_for("wxid_c") == []
        assert q.stats()["given_up"] == 1
        assert q.stats()["redelivered"] == 2

        # deliver 抛异常不能把后台线程带崩
        def boom(cid, body, meta):
            raise RuntimeError("微信窗口不可用")

        q.deliver = boom
        mem.add_pending("wxid_d", "在吗", conv_name="兰博睿", now=clock[0])
        assert q.flush(force=True) == 1        # 投过了，只是失败了
        assert q.pending_for("wxid_d"), "投递失败要继续留着"
        mem.close()
    print("  OK 待办补投：合并 / 退避节流 / 过期放弃 / 投递失败不崩")


def test_llm_timeout_is_wrapped():
    """Python 3.9 里 socket.timeout 不是 TimeoutError 的子类，必须单独接住。"""
    import socket
    import urllib.request

    from dsbot import llm as llm_mod

    client = llm_mod.DeepSeekClient(api_key="sk-test", timeout=7, retries=2)
    LLMError = llm_mod.LLMError
    calls = {"n": 0}
    slept: list = []
    real_open, real_sleep = urllib.request.urlopen, llm_mod.time.sleep

    def fake_open(req, timeout=None):
        calls["n"] += 1
        raise socket.timeout("The read operation timed out")

    urllib.request.urlopen = fake_open
    llm_mod.time.sleep = lambda s: slept.append(s)
    try:
        try:
            client._post({"model": "m", "messages": []})
        except LLMError as exc:
            assert "超时" in str(exc), str(exc)
        else:
            raise AssertionError("socket.timeout 应该被包成 LLMError")
        calls["n"] = 0
        try:
            client.chat([{"role": "user", "content": "在吗"}])
        except LLMError as exc:
            assert "超时" in str(exc), str(exc)
        else:
            raise AssertionError("超时应该最终抛 LLMError")
        assert calls["n"] == 3, "1 次 + 重试 2 次 = 3 次调用，实际 %d" % calls["n"]
        assert len(slept) == 2, slept
    finally:
        urllib.request.urlopen = real_open
        llm_mod.time.sleep = real_sleep
    print("  OK 超时被包成 LLMError 并真的重试（含 3.9 的 socket.timeout）")


def test_requeue_tool():
    """手工补排工具：把忘了回的话排进待办（--write 才动数据库）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("requeue_tool", ROOT / "tools" / "requeue.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db = tmp_path / "m.sqlite3"
        names = tmp_path / "names.json"
        names.write_text(json.dumps({"wxid_x": "同学甲", "wxid_y": "李四",
                                     "123@chatroom": "示例班级群"}, ensure_ascii=False),
                         encoding="utf-8")
        # 不加 --write 只预览，数据库里不该多出东西
        assert tool.main(["--db", str(db), "--names", str(names),
                          "--conv", "李四", "--text", "在吗"]) == 0
        mem = Memory(db)
        assert mem.list_pending() == []
        # 用 wxid 就明确
        assert tool.main(["--db", str(db), "--names", str(names),
                          "--conv", "wxid_x", "--text", "他在不", "--write"]) == 0
        rows = mem.list_pending()
        assert len(rows) == 1, rows
        assert rows[0]["conv_id"] == "wxid_x" and rows[0]["conv_name"] == "同学甲"
        assert rows[0]["body"] == "他在不"
        # 名字撞车（两个人用了同一个备注）时要拦住，让人改用 wxid
        names.write_text(json.dumps({"wxid_x": "同学甲", "wxid_y": "同学甲"},
                                    ensure_ascii=False), encoding="utf-8")
        assert tool.main(["--db", str(db), "--names", str(names),
                          "--conv", "同学甲", "--text", "在吗", "--write"]) == 2
        assert len(mem.list_pending()) == 1, mem.list_pending()
        # 名字不存在也要说清楚
        assert tool.main(["--db", str(db), "--names", str(names),
                          "--conv", "查无此人", "--text", "在吗", "--write"]) == 2
        # --drop 撤掉
        assert tool.main(["--db", str(db), "--names", str(names),
                          "--conv", "wxid_x", "--drop", "--write"]) == 0
        assert mem.list_pending() == []
        mem.close()
    print("  OK 手工补排工具：预览不动库 / 重名拦住 / 排入 / 撤回")


def test_memory_edit_helpers():
    """补历史用的几个小接口：带时间戳记一条 / 查重 / 删掉那条没发出去的草稿。"""
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(Path(tmp) / "m.sqlite3", max_turns=12, ttl_hours=0)
        mem.append("A", "user", "注意看群消息")
        assert mem.has_message("A", "user", "注意看群消息") is True
        assert mem.has_message("A", "assistant", "注意看群消息") is False
        mem.append("A", "assistant", "好的", ts=1000.0)
        row = mem._conn.execute(
            "SELECT ts FROM messages WHERE conv_id='A' AND role='assistant'"
        ).fetchone()
        assert float(row["ts"]) == 1000.0, "补历史时要能写当时的时间"
        assert mem.drop_message("A", "assistant", "好的") == 1
        assert mem.drop_message("A", "assistant", "好的") == 0, "删过了就不该再有"
        assert mem.has_message("A", "assistant", "好的") is False
        # origin：机器人回的 / 班长本人手动回的，同步给会话时要能分开
        mem.append("A", "assistant", "机器人说的")
        mem.append("A", "assistant", "我自己说的", origin="human")
        origin = {r["content"]: r.get("origin") for r in mem.since(0)}
        assert origin["机器人说的"] == "bot" and origin["我自己说的"] == "human", origin
        mem.close()
    print("  OK 记忆接口：带时间戳补记 / 查重 / 删草稿")


def test_memory_upgrades_old_db():
    """老记忆库（没有 origin 列）打开时要能自动补上列，老数据算机器人回的。"""
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL);
            INSERT INTO messages(conv_id, role, content, ts)
                VALUES('A', 'assistant', '升级前说过的', 1.0);
            """
        )
        conn.commit()
        conn.close()
        mem = Memory(path)
        assert mem.since(0)[0]["origin"] == "bot", "老数据默认算机器人回的"
        mem.append("A", "assistant", "升级后我自己回的", origin="human")
        got = {r["content"]: r["origin"] for r in mem.since(0)}
        assert got["升级后我自己回的"] == "human", got
        mem.close()
    print("  OK 老记忆库自动补 origin 列")


def test_ack_detection():
    """「只是在应一声」的判定：该略过的略过，该回的一句都不能漏。"""
    from dsbot.policy import is_ack

    for text in ("行", "好的", "好嘞", "嗯", "嗯嗯", "恩", "哦哦", "知道了", "明白啦",
                 "收到", "可以", "对的", "没问题", "ok", "OK", "Okk", "THX", "👍", "。",
                 "行吧", "可以了", "好的好的", "嗯好的", "好的，谢谢", "谢谢班长",
                 "谢了", "辛苦了", "麻烦你了", "行 谢谢"):
        assert is_ack(text) is True, "这句本该略过：%s" % text

    for text in ("行不行啊", "这样可以吗", "好的我这就去办", "嗯？", "体测几点",
                 "谢谢班长，那报名时间呢", "可以的话帮我问下", "是这个吧？",
                 "行吗", "多长时间", "ok的话我就去", "嗯，还有个事", "多少号截止",
                 "报名截止到几号"):
        assert is_ack(text) is False, "这句必须回：%s" % text
    print("  OK 「应一声」判定：行/ok/好的/谢谢略过，带疑问或内容的照回")


def test_policy_ack_skip():
    """策略层：应一声 = 不回（但要记进记忆）；不在白名单的会话不受这条影响。"""
    from dsbot.policy import is_ack      # noqa: F401  （顺带确认导出可用）

    t = TriggerConfig(mode="whitelist", whitelist=["同学甲"], groups="at",
                      at_names=["我的昵称"], cooldown=0)
    p = Policy(t, self_wxid="wxid_me")
    d = p.decide(conv_id="wxid_a", conv_name="同学甲", text="行")
    assert d.allow is False and d.ack is True, d
    assert d.text == "行", d.text
    d = p.decide(conv_id="wxid_a", conv_name="同学甲", text="ok 谢谢")
    assert d.ack is True, d
    d = p.decide(conv_id="wxid_a", conv_name="同学甲", text="体测几点")
    assert d.allow is True, d
    # 群里 @ 我 + 应一声，也不用回
    d = p.decide(conv_id="123@chatroom", conv_name="示例班级群", is_group=True,
                 sender_name="同学甲", text="@我的昵称 好的", at_usernames=["wxid_me"])
    assert d.ack is True, d
    # 不在白名单的人说「行」：原因得是白名单，不能记进记忆
    d = p.decide(conv_id="wxid_b", conv_name="超市老板", text="行")
    assert d.allow is False and d.ack is False, d
    assert "白名单" in d.reason, d.reason
    # 关掉这个功能，就照旧每条都回
    t2 = TriggerConfig(mode="whitelist", whitelist=["同学甲"], cooldown=0, skip_acks=False)
    assert Policy(t2).decide(conv_id="wxid_a", conv_name="同学甲", text="行").allow is True
    # 自己配的额外略过词
    t3 = TriggerConfig(mode="whitelist", whitelist=["同学甲"], cooldown=0,
                       ack_words=["妥妥的"])
    d = Policy(t3).decide(conv_id="wxid_a", conv_name="同学甲", text="妥妥的")
    assert d.ack is True, d
    print("  OK 策略：应一声不回（记进记忆）、非白名单不受影响、可关可加词")


def test_policy_watches():
    """哪些会话归它管（决定「你自己回的话」要不要记进它的记忆）。"""

    t = TriggerConfig(mode="whitelist", whitelist=["同学甲", "同学乙"],
                      blacklist=["广告群"], groups="at")
    p = Policy(t)
    assert p.watches(conv_id="wxid_a", conv_name="同学甲") is True
    assert p.watches(conv_id="wxid_b", conv_name="超市老板") is False
    assert p.watches(conv_id="wxid_c", conv_name="广告群") is False, "黑名单优先"
    assert p.watches(conv_id="123@chatroom", conv_name="示例班级群", is_group=True) is True
    assert Policy(TriggerConfig(mode="all")).watches(conv_id="wxid_x", conv_name="谁") is True
    assert Policy(TriggerConfig(mode="whitelist", whitelist=[])).watches(
        conv_id="wxid_x", conv_name="同学甲") is False, "空名单谁都不管"
    assert Policy(TriggerConfig(mode="whitelist", whitelist=["同学甲"],
                                groups="ignore")).watches(
        conv_id="123@chatroom", conv_name="示例班级群", is_group=True) is False
    print("  OK 管辖范围：白名单 / 黑名单 / 群 / 空名单")


def test_self_reply_remembered_and_bot_backs_off():
    """你自己在微信里回的话：记进它的记忆；它那条就不发了。"""
    import main as bot_mod

    names = {"wxid_guan": "同学甲", "wxid_wang": "同学乙", "wxid_stranger": "超市老板"}
    me = "wxid_me"

    def incoming(conv, text, sender="", created=None):
        return {"username": conv, "type": "文本", "content": text,
                "sender_username": sender, "sender_id": 2 if sender == me else 1,
                "local_id": 1, "create_time": created if created is not None else time.time()}

    with tempfile.TemporaryDirectory() as tmp:
        cfg = load_config(ROOT)
        cfg.memory.db_path = str(Path(tmp) / "m.sqlite3")
        cfg.knowledge.enabled = False
        cfg.sync.enabled = False
        cfg.reply.delay_min = cfg.reply.delay_max = 5.0    # 留着五秒，好观察「撤掉」
        cfg.reply.batch_window = 0
        cfg.trigger.mode = "whitelist"
        cfg.trigger.whitelist = ["同学甲", "同学乙"]
        cfg.trigger.groups = "at"
        cfg.trigger.cooldown = 0
        bot = bot_mod.WeChatBot(cfg, MockLLM(), no_send=False)
        bot.wxid = me
        bot.policy = Policy(cfg.trigger, self_names=["我的昵称"], self_wxid=me)
        bot.io.display_name = lambda username: names.get(username, username or "")
        sent: list = []
        bot.io.send_text = lambda who, text: sent.append((who, text)) or True

        # 同学问一句 → 排队；你自己回了 → 那条撤掉，一句话都不发
        bot.on_message(incoming("wxid_guan", "他在不"))
        assert "wxid_guan" in bot._pending
        bot.on_message(incoming("wxid_guan", "我帮你问了，他在宿舍", sender=me))
        assert "wxid_guan" not in bot._pending, "你自己回了，排队那条要撤"
        bot._fire("wxid_guan")
        assert not sent, sent
        hist = [(m["role"], m["content"]) for m in bot.memory.history("wxid_guan")]
        assert ("assistant", "我帮你问了，他在宿舍") in hist, hist
        origins = {r["content"]: r.get("origin") for r in bot.memory.since(0)}
        assert origins.get("我帮你问了，他在宿舍") == "human", origins

        # 机器人自己发出去的会被监听读到，那是回声，不能重复记
        bot.on_message(incoming("wxid_wang", "值日表发了吗"))
        bot._fire("wxid_wang")
        assert len(sent) == 1, sent
        before = len(bot.memory.history("wxid_wang"))
        bot.on_message(incoming("wxid_wang", sent[0][1], sender=me))
        assert len(bot.memory.history("wxid_wang")) == before, "回声记了两遍"
        assert bot._mirrored == 1, bot._mirrored

        # 生成慢的时候你自己回了：草稿不能发，也不能留在记忆里
        bot.on_message(incoming("wxid_guan", "体测几点集合"))
        real_reply = bot.engine.reply

        def slow_reply(conv_id, text, **kw):
            threading.Timer(0.2, lambda: bot.on_message(
                incoming(conv_id, "我问过老师了，东一体育场", sender=me))).start()
            time.sleep(0.6)
            return real_reply(conv_id, text, **kw)

        bot.engine.reply = slow_reply
        sent.clear()
        bot._fire("wxid_guan")
        assert not sent, sent
        texts = [m["content"] for m in bot.memory.history("wxid_guan")]
        assert "我问过老师了，东一体育场" in texts, texts
        assert not any(t.startswith("[offline]") for t in texts), texts
        bot.engine.reply = real_reply        # 慢生成只是这一幕，后面照常

        # 它不管的会话不记；十几小时前的老消息（重启可能重读）也不记
        bot.on_message(incoming("wxid_stranger", "菜钱转你了", sender=me))
        assert bot.memory.history("wxid_stranger") == []
        bot.on_message(incoming("wxid_guan", "很久以前我说的话", sender=me,
                                created=time.time() - 13 * 3600))
        assert "很久以前我说的话" not in (
            [m["content"] for m in bot.memory.history("wxid_guan")])

        # 人家应一声（「行」）：不排队回复，但要记进记忆
        sent.clear()
        bot.on_message(incoming("wxid_wang", "行"))
        assert "wxid_wang" not in bot._pending, "应一声不该排队"
        assert not sent
        assert "行" in [m["content"] for m in bot.memory.history("wxid_wang")]
        # 但真有事的消息照回
        bot.on_message(incoming("wxid_wang", "那体测几点集合？"))
        assert "wxid_wang" in bot._pending
        bot._fire("wxid_wang")
        assert len(sent) == 1 and "体测" in sent[0][1], sent
        bot.memory.close()
    print("  OK 你自己回的话：进记忆 / 它不抢 / 回声不重记 / 闲事不记")


def main() -> int:
    print("离线自测开始（不联网、不碰微信）\n")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print("  FAIL %s: %s" % (fn.__name__, exc))
        except Exception as exc:
            failed += 1
            print("  ERROR %s: %r" % (fn.__name__, exc))
    print("\n共 %d 项，失败 %d 项。" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
