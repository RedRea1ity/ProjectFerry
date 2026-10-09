# -*- coding: utf-8 -*-
"""
只做一件事：拿 Mojang 官方 1.20.1 mappings 核验摆渡 Mixin 的注入点签名。
不编译、不联网、不需要 ForgeGradle。
"""
import os
import re
import sys

# 允许 CI 传入自己的 mappings 路径；默认读 ForgeGradle 本地缓存。
MAPS = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "MC_MAPPINGS",
    r"D:\Temp\gradle-home\caches\forge_gradle\minecraft_repo\versions\1.20.1\client_mappings.txt",
)

# 要核验的源码类全名
COMPONENT = "net.minecraft.network.chat.Component"
CHAT = "net.minecraft.client.gui.components.ChatComponent"

def parse(path):
    """解析 ProGuard 格式 mappings -> {obf: {"name":类名, "methods": {(name,desc): True}}}"""
    classes = {}
    cur = None
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line:
                continue
            if not line.startswith("    "):          # 类行
                m = re.match(r"^(.*?) -> (.*?):$", line)
                if m:
                    cur = {"name": m.group(1), "methods": {}}
                    classes[m.group(2)] = cur
                else:
                    cur = None
            elif cur is not None:                     # 成员行
                body = line.strip()
                if " -> " not in body:
                    continue
                arrow = body.split(" -> ", 1)[0].strip()
                # 去掉行号前缀 "13:20:"
                if re.match(r"^\d+:\d+:", arrow):
                    arrow = arrow.split(":", 2)[2].strip()
                mm = re.match(r"^(\S+)\s+([\w$<>]+)\((.*)\)$", arrow)
                if not mm:
                    continue
                rtype, mname, args = mm.group(1), mm.group(2), mm.group(3)
                parts = [p.strip() for p in args.split(",")] if args.strip() else []

                def jvm(t):
                    if t.startswith("void"):   return "V"
                    if t in ("boolean", "byte", "char", "short", "int", "long", "float", "double"):
                        return {"boolean": "Z", "byte": "B", "char": "C",
                                "short": "S", "int": "I", "long": "J",
                                "float": "F", "double": "D"}[t]
                    return "L%s;" % t.replace(".", "/")

                desc = "(" + "".join(jvm(p) for p in parts) + ")" + jvm(rtype)
                cur["methods"][(mname, desc)] = cur["methods"].get((mname, desc), 0) + 1
    return classes


def main():
    if not os.path.exists(MAPS):
        print("[FATAL] mappings 不存在: %s" % MAPS)
        print("        本地：先跑一次 gradle 让 ForgeGradle 下载，或手动下载 client_mappings.txt")
        return 2
    print("解析 %s ..." % MAPS)
    classes = parse(MAPS)
    print("共解析 %d 个类\n" % len(classes))

    ok = True

    # 混淆名 -> 源码名
    by_src = {}
    for obf, info in classes.items():
        by_src.setdefault(info["name"], obf)

    # 1) Component：核验 getString 两个重载
    comp_obf = by_src.get(COMPONENT)
    if comp_obf is None:
        print("[FAIL] mappings 里找不到 %s" % COMPONENT)
        return 1
    comp = classes[comp_obf]
    print("Component 混淆名 = %s" % comp_obf)
    for desc in ["()Ljava/lang/String;", "(I)Ljava/lang/String;"]:
        hit = comp["methods"].get(("getString", desc), 0)
        print("   getString%-26s -> %s  (%d 处)" % (desc, "OK" if hit else "MISS", hit))
        if not hit:
            ok = False

    # 2) ChatComponent：核验 addMessage 三个重载
    chat_obf = by_src.get(CHAT)
    if chat_obf is None:
        print("\n[FATAL] 1.20.1 无 %s" % CHAT)
        return 1
    info = classes[chat_obf]
    print("\nChatComponent 混淆名 = %s" % chat_obf)
    for want in [
        "(Lnet/minecraft/network/chat/Component;)V",
        "(Lnet/minecraft/network/chat/Component;Lnet/minecraft/network/chat/MessageSignature;Lnet/minecraft/client/GuiMessageTag;)V",
        "(Lnet/minecraft/network/chat/Component;Lnet/minecraft/network/chat/MessageSignature;ILnet/minecraft/client/GuiMessageTag;Z)V",
    ]:
        hit = info["methods"].get(("addMessage", want), 0)
        print("   addMessage -> %-4s (%d 处)" % ("OK" if hit else "MISS", hit))
        if not hit:
            print("        期望: addMessage%s" % want)
            ok = False

    # 3) 附：Component 里所有 getString* 方法，人工对照
    print("\n--- Component 中所有 getString* 方法 ---")
    for (mn, de) in sorted(comp["methods"]):
        if mn.startswith("getString"):
            print("   %s%s" % (mn, de))

    # 4) 附：真正承载聊天栏渲染的类里有没有 addMessage
    print("\n--- 搜所有含 addMessage(Component) 的类 ---")
    for obf, info in classes.items():
        if ("addMessage", "(Lnet/minecraft/network/chat/Component;)V") in info["methods"]:
            print("   %s   (%s)" % (info["name"], obf))

    print("\n===== 结论: %s =====" % ("全部命中" if ok else "存在 MISS，需修正 mixin 签名"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
