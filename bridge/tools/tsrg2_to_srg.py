#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【2026-10-12 已停用】本脚本只为 Mixin 注解处理器生成 refmap 服务。
摆渡桥 L1 之后已零 Mixin（两条替换路径都改走 Forge 官方事件），
build.gradle 里的 AP / refmap / SRG 配置已整体删除，本脚本**不再参与构建**，
保留于此仅作踩坑记录。详见 docs/PLAN_SCALE_UP.md 第 2 节。

把 ForgeGradle 产出的 tsrg2 映射转换成 Mixin AP 的 MappingProviderSrg 能读的
旧式 SRG 格式（PK: / CL: / FD: / MD: 前缀行）。

为什么需要这一步（2026-10-10 花了一整晚才定位）：

  Mixin AP 由 SPI 实现 ObfuscationServiceMCP 提供映射服务，它根据
  -AdefaultObfuscationEnv 选择 MappingProvider：

    * notch  -> MappingProviderNotch（读 .tsrg / .srg 的 notch 映射）
    * searge -> MappingProviderSrg   <-- 我们这里选中的

  MappingProviderSrg 的 read() 用的是 MappingProviderSrg$1.processLine，
  反编译后语义如下（逐条对着字节码还原）：

    line 为空 或 以 '#' 开头           -> 跳过
    type = line.substring(0, 2)         # 前两个字符，如 "MD"
    tok  = line.substring(4).split(" ") # 跳过 "MD: "（4 个字符）再按空格切
    if type == "PK": packageMap.forcePut(tok[0], tok[1])
    if type == "CL": classMap.forcePut(tok[0], tok[1])
    if type == "FD": fieldMap.forcePut(MappingFieldSrg(tok[0]).copy(),
                                       MappingFieldSrg(tok[1]).copy())
    if type == "MD": methodMap.forcePut(MappingMethod(tok[0], tok[1]),
                                        MappingMethod(tok[2], tok[3]))
    else: raise MixinException("Invalid SRG file: " + input)

  其中：
    * substring(4) 说明前缀必须正好是 4 个字符 —— 即 "XX: "（XX + 冒号 + 空格）。
      写成纯 "XX "（3 个字符）会串位，写成 "XX  "（双空格）也不行。
    * MappingMethod(String, String) 会把第一个参数按最后一个 '/' 拆成 owner/name，
      所以 MD 行的 owner 和 name 要**用斜杠拼成一个 token**：
          MD: <owner>/<name> <desc> <owner2>/<name2> <desc2>
      这点跟传统 SRG（owner 和 name 分两个 token）不同，务必注意。

  方向（key / value）由 ObfuscationEnvironment.getObfMethod() 决定：
    methodMap.get(<可读方法>) -> <混淆方法>
  即 **key = 可读名，value = 混淆名**。
  我们的 tsrg2 是 left=official(可读) right=srg(混淆)，正好同向，直接搬。

输入：gradle/mappings/official_to_srg.tsrg（tsrg2 格式）
输出：gradle/mappings/official_to_srg.srg   （MappingProviderSrg 格式）
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)
SRC = os.path.join(BRIDGE, 'gradle', 'mappings', 'official_to_srg.tsrg')
DST = os.path.join(BRIDGE, 'gradle', 'mappings', 'official_to_srg.srg')


def main():
    if not os.path.isfile(SRC):
        sys.exit('缺少输入文件：%s' % SRC)

    # 注意：MappingProviderSrg 用 Charset.defaultCharset() 读文件。
    # 我们生成的内容全是 ASCII（类名/描述符/方法名都是 ASCII），
    # 所以 utf-8 与 GBK 读出来一致，不用纠结编码。
    with open(SRC, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()

    if not lines or not lines[0].startswith('tsrg2'):
        sys.exit('输入不是 tsrg2 格式（首行应为 "tsrg2 left right"）：%r'
                 % (lines[0] if lines else ''))

    out = []
    cur_left = None    # 当前类的可读名（official）
    cur_right = None   # 当前类的混淆名（srg）

    n_class = n_field = n_method = 0

    for raw in lines[1:]:
        if not raw.strip():
            continue
        if raw.startswith('\t\t'):
            # 二级缩进行是方法参数映射（0 p_xxx_ p_xxx_），SRG 格式不需要，跳过
            continue

        if raw.startswith('\t'):
            # 成员行有两种形态（tsrg2 规范）：
            #   字段：\t<leftName> <rightName>                    （2 token，无描述符）
            #   方法：\t<leftName> <leftDesc> <rightName>          （3 token）
            parts = raw[1:].split(' ')
            if len(parts) == 2:
                # 字段
                left_name, right_name = parts[0], parts[1]
                out.append('FD: %s/%s %s/%s' % (
                    cur_left, left_name, cur_right, right_name))
                n_field += 1
            elif len(parts) >= 3:
                # 方法
                left_name, left_desc, right_name = parts[0], parts[1], parts[2]
                # MD: <可读owner>/<可读名> <描述符> <混淆owner>/<混淆名> <描述符>
                # 描述符两侧相同（混淆不改原型），复用 left_desc 即可。
                # <init> / <clinit> 不参与混淆（right 名与 left 名相同），写进去无害。
                out.append('MD: %s/%s %s %s/%s %s' % (
                    cur_left, left_name, left_desc,
                    cur_right, right_name, left_desc))
                n_method += 1
        else:
            # 类行：<leftClass> <rightClass>（可能带第 3 个 token 表示继承，忽略）
            parts = raw.split(' ')
            if len(parts) < 2:
                continue
            cur_left, cur_right = parts[0], parts[1]
            out.append('CL: %s %s' % (cur_left, cur_right))
            n_class += 1

    with open(DST, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(out))
        f.write('\n')

    print('✅ 生成 %s' % DST)
    print('   类 %d / 字段 %d / 方法 %d' % (n_class, n_field, n_method))
    print('   大小 %.1f MB' % (os.path.getsize(DST) / 1024 / 1024))

    # 自检：确认 ChatComponent 的字段与三个 addMessage 重载都在
    with open(DST, 'r', encoding='utf-8') as f:
        body = f.read()
    probes = [
        # 字段：单参数消息列表
        'FD: net/minecraft/client/gui/components/ChatComponent/allMessages '
        'net/minecraft/client/gui/components/ChatComponent/f_93760_',
        # 方法重载 1
        'MD: net/minecraft/client/gui/components/ChatComponent/addMessage '
        '(Lnet/minecraft/network/chat/Component;)V '
        'net/minecraft/client/gui/components/ChatComponent/m_93785_ '
        '(Lnet/minecraft/network/chat/Component;)V',
        # 方法重载 3（带 MessageSignature 的）
        'MD: net/minecraft/client/gui/components/ChatComponent/addMessage '
        '(Lnet/minecraft/network/chat/Component;Lnet/minecraft/network/chat/MessageSignature;'
        'Lnet/minecraft/client/GuiMessageTag;)V '
        'net/minecraft/client/gui/components/ChatComponent/m_240964_ '
        '(Lnet/minecraft/network/chat/Component;Lnet/minecraft/network/chat/MessageSignature;'
        'Lnet/minecraft/client/GuiMessageTag;)V',
    ]
    for p in probes:
        print('   %s %s' % ('✓' if p in body else '✗', p[:100] + '...'))


if __name__ == '__main__':
    main()
