#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成摆渡桥的硬编码文本映射表（第一版：boh 全量）。

流程：扫 jar 常量池拿到硬编码原文 → 匹配译文 → 写入
src/main/resources/ferrybridge/data/translations.json

key 存的是"剥掉 § 格式码后的英文原文"，因为 TranslationStore.lookup
会先精确匹配、失败再剥 § 码重试，所以 key 不带 § 更稳。
"""
import json
import re
import zipfile
from pathlib import Path

JAR = r"D:\Minecraft\versions\1.20.1-单独仙境吃屎版\mods\boh-0.0.10-forge-1.20.1.jar"
OUT = Path(__file__).resolve().parent.parent / "src/main/resources/ferrybridge/data/translations.json"
CODE = re.compile(r"§.")

# 译文表：左引号「」在 zh_core 里给出成对引号样式
# 这里按原文的花引号 “” 或直角引号" 成对写出。
ZH = {
    "“One shot. Make it count.”": "“一枪一个，要算得准。”",
    "“A strange power sleeps inside this apple.”": "“这颗苹果里睡着某种奇异的力量。”",
    "“YOU NEED TO LEARN… permanently.”": "“你得学会它… 永久地。”",
    "“It wants to move. You just hold on.”": "“它想动。你只要抓紧就行。”",
    "“It's 100% June”": "“百分之百是茱恩”",
    "“This… is my BOOMSTICK!”": "“这个… 就是我的爆爆棒！”",
    "“Some spirits fear the lens more than the blade.”": "“有些灵魂怕镜头胜过怕刀刃。”",
    "“Subtlety is overrated.”": "“低调是过奖了。”",
    '"Light in your hands. Heavy in their eyes."': '"手里是光。他们眼里，是重量。"',
    "“Eight pages. One obsession.”": "“八页纸。一份执念。”",
    "“Dignity sold separately.”": "“尊严另售。”",
    "“Fast enough to outrun your problems. Loud enough to announce them.”": "“快到能甩掉你的烦恼。响到能昭告它们。”",
    "“Sleep tight.”": "“好梦。”",
    "“A forbidden terminal designed to breach realities. The GATE Computer does not create life — it downloads it.”":
        "“一台为撕裂现实而生的禁忌终端。GATE 计算机不创造生命 —— 它下载生命。”",
    "“One cut is all it takes.”": "“一下就够了。”",
    '"Their hatred was so great it refused to die, now it cuts for whoever wields it"':
        '"他们的仇恨如此之大，以至于拒绝死亡；如今它为任何握持它的人切割"',
    "“The tide of blood answers his call.”": "“血潮回应他的召唤。”",
    "“The anger stayed.”": "“愤怒留了下来。”",
    "“They always scream the same when it hits.”": "“被击中时，他们总是叫得一模一样。”",
    "“A familiar face. A borrowed identity.”": "“熟悉的脸。借来的身份。”",
    "“A favorite of killers who don’t know when to stop.”": "“那些不知道停手的杀手的最爱。”",
    "“A crude copy of you. Good enough for emergencies.”": "“你那个粗糙的复制品。应急够用了。”",
    '"Every swing writes a new obituary.”': '"每一挥都写下一则新的讣告。"',
    "“It learned how to smile.”": "“它学会了怎么微笑。”",
    "“Its color is not paint.”": "“它的颜色不是颜料。”",
    "“Say cheese. Something will.”": "“说茄子。会有东西回应。”",
    "“Not of this world. Fortunately, neither are its victims.”": "“不属于这个世界。幸，它的受害者也一样。”",
    "“It never stops coming… and neither will you.”": "“它永不停下… 而你也一样。”",
    "“Without anchors, reality collapses inward.”": "“没有锚点，现实向内塌缩。”",
    "“You hear whispers when you hold it. They want you to swing harder.”": "“握住它时你听见低语。它们要你挥得更狠。”",
    "“Its pages are burned, but it still burns others.”": "“书页已被烧尽，但它仍在灼烧别人。”",
    "“Silent. Fragile. Hungry.”": "“沉默。脆弱。饥饿。”",
    "“It twitches. It remembers.”": "“它抽搐着。它记得。”",
    "“A hero never dies. But you might.”": "“英雄永不落幕。但你可能会。”",
    "“Silent. Quick. Uncomfortably practical.”": "“无声。迅捷。实用得让人不安。”",
    '"Heavy enough to break the ground… and your spine."': '"重到足以劈开地面… 和你的脊椎。"',
    "“A blade that doesn’t cut — it tears.”": "“一把不切割的刀 —— 它撕扯。”",
    "“Sirenhead’s least lethal weapon.”": "“塞壬头最不致命的武器。”",
    "“The tape inside moves on its own.”": "“里面的录像带会自己动。”",
    "“Reliable. Predictable. The opposite of everything else in here.”": "“可靠。可预测。与这里其他一切截然相反。”",
    '"Fixing the flaws of what they couldn\'t moderate right"': '"修补他们没能管好的东西的缺陷"',
    "“Fixing the flaws of what they couldn't moderate right”": "“修补他们没能管好的东西的缺陷”",
    "“Warm to the touch. Pulsing with something that is definitely not blood.”": "“触手温热。脉动着某种绝对不是血液的东西。”",
    "“They won’t listen… but they’ll feel it.”": "“他们不会听… 但他们会感受到。”",
    "“A killer’s promise carved into cold steel.”": "“刻在冷钢上的杀手的承诺。”",
    # 操作说明类（无引号包裹）
    "- Left-click: Weakness + Slowness (you can’t fight with it).": "- 左键：虚弱 + 缓慢（你没法用它战斗）",
    "- Right-click: Claps the ruler, granting": "- 右键：拍击教鞭，赋予",
    "- Right-click: Propels you forward, damaging everything in your path.": "- 右键：向前推进，摧毁路径上的一切。",
    "- Right-click: Freezes all nearby entities in place.": "- 右键：冻结附近所有实体。",
    "- Equippable (…why?).": "- 可装备（……为什么？）",
    "- Left-click: performs an AOE cleave.": "- 左键：发动范围横扫。",
    "- Right-click: Sends a blood wave forward.": "- 右键：向前释放一道血浪。",
    "- Right-click: Summons Phantom Animatronics that attack your target.": "- 右键：召唤幻影玩偶攻击你的目标。",
    "- Right-click: Dash forward, damaging everything in your path.": "- 右键：向前冲刺，摧毁路径上的一切。",
    "- Right-click: Gives Glowing to nearby mobs.": "- 右键：给予附近生物发光效果。",
    "- First left-click: Charge the strike.": "- 第一次左键：蓄力打击。",
    "- Second left-click: Slams the sign, dealing AOE damage.": "- 第二次左键：砸下标牌，造成范围伤害。",
    "- On hit: Low chance to grant Speed": "- 命中时：有小概率给予速度",
    "- Right-click: Gives short burst of Jump Boost": "- 右键：给予短时间跳跃提升",
}


def class_constants(data):
    """只扫常量池 tag=1（Utf8）项——那是字符串常量，不是方法名/类名。"""
    out = []
    if len(data) < 10:
        return out
    count = int.from_bytes(data[8:10], "big")
    i, n = 10, 0
    while i < len(data) and n < count:
        tag = data[i]
        if tag == 1:
            ln = int.from_bytes(data[i + 1:i + 3], "big")
            out.append(data[i + 3:i + 3 + ln])
            i += 3 + ln
        elif tag in (7, 8, 16, 19, 20):
            i += 3
        elif tag == 15:
            i += 4
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            i += 5
        elif tag in (5, 6):
            i += 9
            n += 1
        else:
            break
        n += 1
    return out


def scan_tooltip(jar_path):
    """扫 *TooltipProcedure 类里玩家可见的硬编码文本。"""
    found = {}
    with zipfile.ZipFile(jar_path) as z:
        for name in z.namelist():
            if not name.endswith(".class") or "Tooltip" not in name:
                continue
            for s in class_constants(z.read(name)):
                try:
                    t = s.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                # 玩家可见：带 § 码、有空格、不是命令/粒子
                if "§" in t and len(t) >= 10 and " " in t \
                        and not t.startswith(("/", "[particle")):
                    found[t] = name.split("/")[-1].replace(".class", "")
    return found


def main():
    raw = scan_tooltip(JAR)
    print(f"扫到硬编码 tooltip 文本 {len(raw)} 条（去重后）")

    mapping, unmatched = {}, []
    for text in raw:
        plain = CODE.sub("", text).strip()
        zh = ZH.get(plain)
        if zh:
            mapping[plain] = zh
        else:
            unmatched.append(plain)

    print(f"配译文 {len(mapping)}/{len(raw)} 条，未匹配 {len(unmatched)}")
    for t in unmatched:
        print(f"  未匹配: {t!r}")

    out = {
        "_说明": "硬编码文本映射表。key 是剥掉 § 格式码后的英文原文，value 是中文。",
        "_红线": "只做精确匹配。玩家自己写的内容永远不在表内，因此天然安全。",
        "_来源": f"boh-0.0.10 的 TooltipProcedure 类常量池，共 {len(raw)} 条。",
        "_查表逻辑": "TranslationStore.lookup 先精确匹配，失败则剥 § 码再试，故 key 不含 §。",
        "格式版本": 1,
        "map": dict(sorted(mapping.items())),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print(f"\n已写入 {OUT}：{len(mapping)} 条映射")
    return 0 if not unmatched else 1


if __name__ == "__main__":
    raise SystemExit(main())