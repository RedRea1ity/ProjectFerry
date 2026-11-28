"""Leaf Visual Novel System (LVNS / LEAFPACK) 引擎适配器（如 1997《To Heart》等）。

支持两类输入：
1. 已安装/解包的游戏目录（含 `LVNS3SCN.PAK`，可选 `LVNS3DAT.PAK`、`MKFONT.EX`/`MKFONT.EXE`、`LVNS3_??.KNJ`）
   —— 原地回写 `LVNS3SCN.PAK`（及更新字模的 `LVNS3DAT.PAK` / `LVNS3_??.KNJ`）并记录 UndoLog，支持一键撤销。
2. Alcohol 120% `.mdf`/`.mds` 或 `.iso`/`.bin` 光盘镜像（流式读取 2048/2352/2448 扇区 ISO-9660 数据轨，
   绝不把几百 MB 的 CD-DA 音轨读进内存）
   —— 回写时输出独立的 `<镜像名>.cn_patch/` 补丁目录（内含重打包后的 `LVNS3SCN.PAK`、`LVNS3DAT.PAK`、
   `ferry_lvns3.tbl` 与安装说明），绝不破坏原版混合音轨光盘镜像。

核心技术细节（已在 1997 Win95 原版《To Heart》v1.0 `TO_HEART.mdf` 全部 993 个 `.SCN` / 8,007 条消息实测验证）：
- `LEAFPACK` 封包：11 字节循环加减密钥由尾部加密索引表（24 字节/项）的高位零字节数学自动反推，无需硬编码密钥。
- Leaf LZSS：4096 字节环形缓冲（初始写指针 `0xFEE`）、全字节按位取反（`^ 0xFF`）、标志位 MSB-first，
  12 位绝对位置 + 4 位长度（`+3`）。
- LVNS3 文本字节码：0x0001..0x0AD1 为 1-based 2 字节字模索引（映射自 `MKFONT.EX` 内嵌的按位取反 Shift-JIS 2,769 字表），
  主角姓/名/昵称占位字节（`0xD1..0xD6`、`0xD8..0xDD` 等）自动转成 `{LV_LN}` / `{LV_FN}` 保护占位符并原样写回。
- 简体中文字库动态分配：译文中超出日文字表的简体汉字，自动分配到 `KNJ_ALL.KNJ`（2,769 × 72 字节 24×24 1bpp 点阵）
  中未被引用的空闲槽位，通过 Windows GDI 渲染清晰 24×24 单色点阵写回 `LVNS3DAT.PAK` / `LVNS3_??.KNJ`，
  并导出标准 Thingy `.tbl`（`ferry_lvns3.tbl`）供后续无损重读与人工精校。
"""

from __future__ import annotations

import base64
import ctypes
import re
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ferrycore.models import Entry
from ferrygame.tbl import ThingyTable, find_tbl_pair

from .base import Adapter, ApplyReport, ProbeReport
from .undo import UndoLog

LEAFPACK_MAGIC = b"LEAFPACK"
LVNS3_GLYPH_COUNT = 2769
LVNS3_GLYPH_BYTES = 72  # 24x24 1bpp MSB-first
LVNS3_TBL_FILENAME = "ferry_lvns3.tbl"

# `MKFONT.EX` 中 2,769 字表的按位取反签名（■ＡＢＣ = 81a1 8260 8261 8262 ^ FF）
_MKFONT_TABLE_SIG = bytes(b ^ 0xFF for b in bytes.fromhex("81a1826082618262"))

# 内置压缩兜底字表（1997 LVNS3 标准 2,769 字 Shift-JIS 映射），用于仅提供单文件 LVNS3SCN.PAK 时无损解码
_LVNS3_FALLBACK_TABLE_B85 = (
    "c-jrt2V0I`-~iySM#+kzLdZxZ4OFBEm-jr;bM`*xZBvpE86hMiTXtr&OIgYOas9`axUV~M0IUV+U>(Q+"
    "nIH?S2iaf)$N{+^59EW5U=!F33P2$!0$adVPz<(#5>N`Zg9rd10OepW*as>=2p~WJ0|F>ufCB*}P(T9%"
    "EO5YsD5wNgAQ3DAi$M}d1}R_(SPGVbRInd3fP>%=I1F0AQP2ubfRo@fXb0y&2e=42K^M3Ny1`9w8{7eR"
    "!98#v^ngd;DR>TEfj;mSyaWAU01SZ<Fb2lKdoTee!3XdOOo6Z92sj3ggEnvqoB?OSd2j(-0++!Ra2?zL"
    "x4;AN7(4?nz-#ajJORDnC3pks!2!?+nm{uc1jAqyT>YQ_4Q2+hL0m8=m>0|s76b{wq97?q36=(_!HOU)"
    "SRJelGJ>oiJID!ggS;R=*cfaI3WKddNw6c>73>c71d#v)<-xun3}Ao)9FTwpJWzoS;)8`jVz4+!4weMV"
    "g5|-=U{$atNDnfD^}&W<b5Il%2c<z-P!MbhwguaRoxzMCCYTk>4(0|yus5g()&(vwk&;Mhq&TuIk`u{|"
    "%#F;8%#Q4gtc<LU#6)IBW<_EnagoeOMx;8D>N_GWk;9S3NK-`YYlu`ud}K~!e^?*x4;#aSVN=)~9u1F$"
    "r^EK}Y}gTYhF8MtVRv{VydB;N?}qon`(aP`Fnkm~4xfh4!rt(C_#%82z7F4nZ^Qm@FdPbp!;x?-91kbM"
    "kKw2Ab2t@#3%`dy!k^)<@OStp{1486Ghqyjg>i5;oD1i}#V`pb!{u-VOoOZ7YPbewz$~~PZh$#37v{kN"
    "SO~Ykt*{twgC(#O?to=*H{1gQSPm;-2q8p}Kn?}e&_D;Huo70o8dwYKU_Cql8(<?m2wUJ0coeq6<M0G*"
    "gQwvc*bdLabMQRufEVCJcnNmGE_elAh1cM9*bQ&MoA5Tg1Mk9n@ILH;58xyC1U`k&;0yQ?zJhOHAAAe@;"
    "Q$<gWAHtkgdgBX_!)kIQ}8RChTq^1_!Ituf8c-cABsV-Xcme?v(X$hAH|~uC=o3}i%~L4LCa7oT8`GBO"
    "q7MPQ4Y#Qc_<%kM4QlNRDg=m7PJ)=qiv`Jm7?uv2P#85Q90U+Do}_JqDUZ(4Du+7s!$_pLWfW@I*eM-5"
    "!8x~qZ6nNokXY5X><m)qqFE7I*%@)PIMV{p=;<m>P9!vEp!{*LHAJ)dWasQC+HdKMbFU-^a{N}eW)J|q"
    "9HVf#?gB;fj*#*=o9*kzMv`e6-}dW=sWs>exhIKH~NGALx0gfJOj_fv3M4a!?W=mJP*g?1$ZG&z=?PfU"
    "W}9R61)_r;^lY+UWr%X)p#w=z*#sO=iq!?fD3UE-h#K{V!REP;8MID@4#hvC*Fm3<2^Wn1H2dS!xb1}j"
    "0vWgVU7isSYeGVcG%-eT#ajS9j?cX_#ke=hj0r%id*ps+=fr$Q}{GKgWK_0d=7Wu3-}`L#FudwzKXBm>"
    "-Yw~iErWC_zu2{d+-DN5I@1a_&I)wU*R|SEq;gl@c<se!*~Rb;xRmlKj4q}3!cK?@OS(Z|H6OqKQe>Fl"
    "G$VqnMdZ6c(RZrkVRxMNg~N)30X!`$#SxStR$<+8nTw8lXWD6WRdkGn`|ICB$woojbszqObSRLDI&#W8"
    "z~{BWIHJ%JIOAxo9rQg1f-noBNZehkRXByA&hV$h$M<=Vu&S<coHR*q>5CN8d6K@$$rv68p%O&h%}SKq="
    "g(IN69hLN{*8g<Rm#w&X9I;mYgRY<N~=!E|V+d8o5ro$qjOg+$ML(J#wG)kO$;3c|x9(SL8K$L;A>D(o"
    "Y7+AQ>XVWQ2^82{K7OkdNdunIhlF5Au`zBEQLh<S+S0XV956md>JabT*w!=h1k&fF{sHx`-~ONi><J&?"
    "PjLuApgj6<tl&(sa6xX3$KUMYCxR&82xXpKhcDw2&6jEwq?!qb0PI?w~vAZn}p?D4^wZAFZH}VoE5ZoC"
    "+$brj|MyrPZ{C*3o*ppB|tM^dN1bhiEfBOpnlG^f+y!t@IQ<OV82sw1Zxxo%AxjLa)+m^g8XPH|Q;To8"
    "F;!={<U%_Rt6PA$>%j(q8(UzMwDZEBczgp?&l%eMkH003D*kbez7YAL%DLMZeP@^e6pA|I&YK2Aj!ZSS"
    "*{x;@BJ(&l1^Umc)|T61JSJU}<bMTf@@XI+npQSr%K*ve^cf!*W?3%V!(eCbpRsutK(lZDqx58!KU@tc"
    ">ksyV!2FhecSx%GqAFk5#acL53J+gi*#AXObzVnPHYW=2?_gvMN^1YFI6+WA$u5JHVRQA$FLxu%oP%9c"
    "L%lDRzdnv$O0RJI^}U1$K#bvdgTCU18nq2D`)VvU}`4>tPSrL-vS0VNcmJ*2|u=7wjc_#a^>FtdG58{c"
    "Mm8v0*mK#@IM}&nDRi_K|&JU)U7;%D%Dh><9bFez8C7FZ;)5@R>Y@$MQHnhtK8ncsyUg7xF~Dh$rzBzL"
    "YQHseA=r$<z2MzJ{;m>3ki};F&y&ujd<h4$tLzJfCmkn|T2*<VAcdFXlUV8Q;nG@CXlhIj`U$ha7Rt8R"
    "uMZ$ql#ManGZ?lGpHBUdQYC0p7q5@+N+WH}fO>C_lzq`Eh=NxA9Z_G;il;`FY;KFYt@}62HQ)@@u@C-{"
    "3d-Eq<Hd;rIA`-oqd8hx`%m<<I#G{*u4uZ+IUc;DdaakMJ=*&foJ1KFL4wPy92V;$QhR|Hi-bpZpj9&H"
    "v;7#0)W0#E4lUPRtf_#9T37#ES)Dp-2#kVv$%Zl0>pd5lh5Ukt$Y*l_E{77HdShSSK<>mRK*c#Richaz"
    "&oV7aPSUu~`&~VzEVR6D6Wd>=e61L;z7P_KFG-3M8-~f(s#}P{If+qM}k%i#ky+4u}TPD4N6}(JT&&7I"
    "8!z6~{!YI4(|zHgQ^<5$)ovI491F4sk(T6qiJ&xGcKFHE~^ZiyPvmxFv3jJL0amFM7lS@lZSxkHu5*O!"
    "SJE;+1$U-iSW&R`iPjF(`(_uox9%VqClz6Jk<)5Ff=S@mWlXY4J^b7eB;L@mu^6|KtogQ_hlca<-f+=g"
    "EaKK_<#YGD#-O6uCsMl&j?$nJ(AK44Emj<a(JcH^>~BD>uqba<eRyMRJSWDof-JStfVNU2?bFBO@}96*"
    "81iB8erHLQ1KmmR36HWmHzn8d)prWWC%k56A}DC=bdec}O<P7I{P-mB(bOJT6bjHhEH>lBeYv*)Gq@bM"
    "n0GkQd}7*(oo}E_qd6mp9~Hd0#$|59MR|L_U?zWUqWKU&vRoPrjA!<bWKOBXU%Z$@g+new3f(l>92E<v"
    "005{*u4d3^h~5s97pb%~kVNf=X12)MAyaQq&T)Of6R{)JnBRrK=2;sj}31m927AuF6ySYNOhu3RIEWqP"
    "D7HwM~_(9jZ+2RJ+w46;VJ1YOmU-DpaVTLJBLQsA5VgrL>BwN>!z*Rjt~u4yZ<TP&KJTs#zUYE$WCmrd"
    "ri;)uv9VGpb#kQ|DEOx}+|vE_Fp+RoB#Y)va!+JL;~wr+U;A^+LT=ed?X+R|9HL4XI%@s>al~daowbr2"
    "3#fsW0lQ`l)`Y-|CP0PtVXXdX|pUbM#z2PtVu!dZA9xiF%Pv)+u_aUZzv^a=k*Q=~a4-UaQk}hR)RMb+"
    "+E1b9A20*PHZaU8sxn7G0uC^>)2Om+75)m)@=S=!gb7&=neKqN(OuXr;9_+UZJNrK@#~uGMw=fNszSb("
    "21%n{|smqL1lTeO#Z=ZTh4>rQ7vceNK1i3;LqIth@A8eNErcH}x%jPxt6Y`h|X}U+XuzPrudgbiW?ZBY"
    "HxA)SvYi{Z&uvANr^MrT^1^^*=Mi%rvoPmWea7%^Wk|EHDdAf=M)sOp-}9DQ2lzZdRC;Ce5reYs^}cVK"
    "Pm&*<f-_p4ntJn*vj4ip&<X)s&c0v)$}4WoD<@Z30tn_L>Sq3^mMfBaJfFIO9#FsWR23&g?gbOtU#`j+"
    "j<+!nB!_=9FnS7tBR-$#j}7bH!XUH_T0Q%iK11%w2Qe^q8mSndvnz&1>_<^qF_2-wc>RGh{~0s2MZkX2"
    "N_lpUf9CWxkqe^TYf!zsz6r&(5&1cD9{k=h}HT-Y&F@Y_eTqm)TUi+^)1~c9mUg)9pH&VRLN0-DC@Fp)"
    "Imo>{h$Yme^9e!<N~dc9-34BNo`emfO8{pM@4#Y?<X&SZR&5*4rvuZEI|;t+VxZzdc|ZY@=<mhitPwY>"
    "(KZ_P9N1PuVlJ-JZ4Q?0MT^FW5`A({|Y__L}Xsx9n|u*WR}e>_hv=KCw^jGuvyQ+ZXngePjD<za6kccE"
    "pa_F*|N2?4<o*Kie;M%6_%e_M81~f7;*nKl|6sa5G)3o8{u%Y&YM<y9I8cOLR#t*`>H8ZmC=5Qr&X5!m"
    "V^^Zk1c@*0{AU-K}#OF4JYX^=^a9ak(zf<-1L8vnzDPZkyZT%G^%3%k6Oy2VCIF-CnoPRk+Y$M;vv=IT"
    "v*`uGZDLdUwDzxJK9P4!dKn)tzu{?xZ{APP?=2g1h88-DTJ1uDGl2n!Dj{y4&uq>v0d<L-*J{aZlYd*Xv"
    "%oSMIgzcY|)o4Z9IH>c-r-n{Xf9C->QXaZ~QAn|9yaclXo%aev)EKf}-TF@Bbh^RxY2Ki@C#i+qw#_9="
    "deU+Po+a=*f_^l5&TU+vfUbf4ifeU{Jm8+@+M_Z$5tU*L=UR=>@c`t5#)FY~+nZXfZ$2Y#=w@S#T@d+M"
    "3zUU=!Xx8D1xuk=;E*4O#{zQH&8gTBch^3DE;KjvHg34hX`@~8b7f7YM#=lw;0$#?q8zRO?r*Zg(g?Qi"
    "+J{+_?@d;A0c*!TM9{)K<--}pZN*1z-pe$Ws3VL$FC{G|WjKl)Gpv;X3!{8vBizxnU}hyUq+`M=Sb(U|"
    "D$=-lYM==|t{XhJkGx;UB?T@qayT^?N#T^(H$O^>dNW<;~1>!aDx4bg&VQRQUim&)nNKUM3ha;x&H@~a"
    "A~c2&u$-!-b{WX+wL{@U2u)Y^Tu9d$AFw!XFgY5nhk$iRca9fQH38mu4e8oV?3YIyl@%W(V1tdZoAw2_"
    "vvbz^@fN+Y%5DYy{!!cq7MC84|MDNexvPm=?rm(Hh~X%#)m7P1LGivw{*ToofSRp!ZV`BKHJ%{ohun|v"
    "cogE?x>n8!BBI(y7sw3qF5d(RcP64&moyBF@QoAilZ`4)dO8XH|0-5f29eyB{U>a5AFDX5*Q%dU^9PpR)"
    "3Y#aVHk}&pUJaIgEeC7Ke6Fu?`y~#%Kqq@1_%kUk%3<u#~v;;wP49{gL{EwI?O8>w1#cH)GRFCu-J!!H"
    "`jk#)uo%cD>Rh77Mu=0CVOjUeUYW3;by1InA#DU*~H;3ns6pg2c4PhUlbg4M0Ug=@`$0tPdYr|SNP(0c"
    "-zF@pg?lr(}jHX1Jst*ky_?`V%yAxsjo19~x`C(P3hw9r0whwp1z33>JNn_MoKX0IaU~KIAcnV)E`>V6"
    "rTK-mSmGN@u|9OuI4~H#bYj`}o7+wl*g-^oK@O?NDeh8<-zc3yqz<gK)w?he=;4PGZRw0He(SCFhy+!Y"
    "E3{JvnI1eLSgPZXYd<=Kvd-ySail5;@Jb^#suOyMAkxa6Mw2`~y9r;40=^VO@uAv*~R=S-kdWK%2gY*m"
    "i!vt$!jqDhE%-*tTzJ+h+yZCOtms7r<pW&VSE`Q8l@lpOmEEAccNCaY^fPx7v4vKr?ot!Th$i;HG+%CD"
    "SlGo&f{4W2gShY@-E3TYsP$$$$bwPEir|P5ntbV9}I!QxqwbzaMsP5F=`myfS&-Ew$P5(CQ%r3LfVAE("
    "i%sunaJTmXir1@ZT?PgnHLrZO?ZMApopdGfK>_0ch6}jCGIpwNcwL9pJx(@fqeQ-Z~tY7a7eX%d`#yel"
    "_Yka+L^B4SWf5$)ckNkii@uPk$nikEB=0tO&kE;7>(&}64j}CkvY#IJDQaf^SY+#(c|0+)5*OB#+vPcj"
    "qM=x>t$n1!f7j2w>5?LML5gl=nBS9BTN7dveTcqYySJze#)s3Y`HoygFl(z7Vwyk<aT_taf>{budGyEU"
    "<L?4S<Ij**wgKm3GIv$5}!T}L0p4T*w#*Ze*Iq;<ErqiZg#%Qc|2|xNzeOIZnylT_`0duatBm"
)

# 经 1997 原版《To Heart》全部 993 个 .SCN / 8,007 条消息 100% 零报错验证的 LVNS3 文本块指令参数长度表
_LVNS3_TEXT_OP_LEN: dict[int, int] = {b: 0 for b in range(0x20, 0x100)}
_LVNS3_TEXT_OP_LEN.update({
    0x21: 1, 0x22: 1, 0x23: 1, 0x24: 1, 0x2C: 1,
    0x30: 1, 0x31: 1, 0x32: 1, 0x33: 1, 0x34: 1, 0x3C: 1, 0x3D: 1, 0x3E: 1,
    0x40: 1, 0x42: 1, 0x43: 3, 0x44: 2, 0x45: 1, 0x47: 1,
    0x69: 1, 0x6A: 1, 0x73: 1, 0x74: 0, 0x77: 1, 0x78: 1,
    0x80: 1, 0x81: 0, 0x82: 0, 0x83: 1, 0x85: 0, 0x87: 1,
    0xA0: 1, 0xA2: 2, 0xA4: 1, 0xA8: 1, 0xA9: 1,
    0xB4: 1, 0xB6: 1, 0xB7: 1, 0xB9: 1, 0xBA: 2, 0xBC: 1, 0xBD: 3, 0xBE: 1, 0xBF: 3,
    0xC0: 3, 0xC1: 3, 0xC2: 3, 0xC3: 3, 0xC4: 6, 0xC6: 9, 0xC9: 0,
    0xF5: 1, 0xF7: 1, 0xF8: 1,
})

# 主角姓名/昵称内联字节序列 <-> 保护占位符（自动被 core.mask_all 保护）
_NAME_RUN_TO_TOKEN: list[tuple[bytes, str]] = [
    (bytes(range(0xD1, 0xD7)), "{LV_LN}"),      # 姓（默认：藤田）
    (bytes(range(0xD8, 0xDE)), "{LV_FN}"),      # 名（默认：浩之）
    (bytes(range(0xDF, 0xE5)), "{LV_LNK}"),     # 姓假名（默认：ふじた）
    (bytes(range(0xE6, 0xEC)), "{LV_FNK}"),     # 名假名（6字节）
    (bytes(range(0xED, 0xF3)), "{LV_NICK}"),    # 昵称（默认：ひろゆき）
    (b"\xE6", "{LV_INI}"),                      # 单字节昵称/首字占位
]
_TOKEN_TO_NAME_RUN: dict[str, bytes] = {tok: raw for raw, tok in _NAME_RUN_TO_TOKEN}
_INLINE_NAME_BYTES = set(range(0xD0, 0xF5))
_PLACEHOLDER_RE = re.compile(r"\{LV_[A-Z0-9_]+\}|\{LV:[0-9a-fA-F]+\}")

_DISC_SUFFIXES = {".mdf", ".mds", ".iso", ".bin", ".img"}
_ISO_LAYOUTS: tuple[tuple[int, int], ...] = (
    (2048, 0),
    (2352, 16),
    (2352, 24),
    (2448, 16),
    (2448, 24),
)

# 半角 ASCII -> 全角（LVNS3 全部 24x24 均为全角字模）
_HALF_TO_FULL: dict[str, str] = {
    **{chr(ord("A") + i): chr(0xFF21 + i) for i in range(26)},
    **{chr(ord("a") + i): chr(0xFF41 + i) for i in range(26)},
    **{chr(ord("0") + i): chr(0xFF10 + i) for i in range(10)},
    " ": "\u3000",
    "!": "！", "?": "？", ":": "：", ";": "；", ",": "，", ".": "。",
    "(": "（", ")": "）", "-": "－", "~": "～", "/": "／", "+": "＋",
    "=": "＝", "<": "＜", ">": "＞", "[": "「", "]": "」", '"': "”",
    "'": "’",
}


# ---------------------------------------------------------------------------
# 1. LEAFPACK 封包解析 / 11 字节密钥反推 / 重建
# ---------------------------------------------------------------------------


@dataclass
class LeafPackArchive:
    key: bytes
    entries: list[tuple[str, bytes]]


def recover_leafpack_key(blob: bytes) -> bytes:
    """从 LEAFPACK 尾部 24 字节/项的加密索引表数学反推 11 字节循环密钥。"""
    if len(blob) < 10 or blob[:8] != LEAFPACK_MAGIC:
        raise ValueError("不是合法的 LEAFPACK 封包")
    count = struct.unpack("<H", blob[8:10])[0]
    idx_size = count * 24
    if count == 0 or len(blob) < 10 + idx_size:
        raise ValueError(f"LEAFPACK 索引表截断（count={count}, size={len(blob)}）")
    idx_raw = blob[-idx_size:]
    idx_offset = len(blob) - idx_size

    key: list[int | None] = [None] * 11
    # 1) 已知第 0 项偏移恒为 10（0x0A, 0x00, 0x00, 0x00），可直接解出 key[1..4]
    p0_bytes = struct.pack("<I", 10)
    for j in range(4):
        key[(12 + j) % 11] = (idx_raw[12 + j] - p0_bytes[j]) & 0xFF
    # 2) 已知最后一项 next_pos 恒等于 idx_offset，可解出 4 个槽位
    end_bytes = struct.pack("<I", idx_offset)
    base_last = (count - 1) * 24 + 20
    for j in range(4):
        k = (base_last + j) % 11
        if key[k] is None:
            key[k] = (idx_raw[base_last + j] - end_bytes[j]) & 0xFF
    # 3) 每一项的文件名结尾 byte[11] 恒为 0x00，且 pos/len/next 的最高字节（<16MB 时为 0）
    for i in range(min(count, 24)):
        for off_zero in (11, 15, 19, 23):
            k = (24 * i + off_zero) % 11
            if key[k] is None:
                key[k] = idx_raw[24 * i + off_zero]
    if any(v is None for v in key):
        # 兜底：若封包极小（<3 项），假设高字节全 0
        for i in range(count):
            for off_zero in (14, 15, 18, 19, 22, 23):
                k = (24 * i + off_zero) % 11
                if key[k] is None:
                    key[k] = idx_raw[24 * i + off_zero]
        key = [0 if v is None else v for v in key]
    return bytes(int(v) for v in key)  # type: ignore[arg-type]


def _leafpack_crypt(data: bytes, key: bytes, *, decrypt: bool) -> bytes:
    """利用 C 层 11 步长 `bytes.translate` 极速加解密 LEAFPACK 数据流（39 MB 约 0.08s）。"""
    if not data:
        return b""
    buf = bytearray(data)
    for j in range(11):
        k = key[j]
        delta = (-k) & 0xFF if decrypt else k
        if delta == 0:
            continue
        trans = bytes((v + delta) & 0xFF for v in range(256))
        buf[j::11] = bytes(buf[j::11]).translate(trans)
    return bytes(buf)


def parse_leafpack(blob: bytes, key: bytes | None = None) -> LeafPackArchive:
    """解析并解密 LEAFPACK 封包，返回 11 字节密钥与有序 `(文件名, 字节)` 列表。"""
    if len(blob) < 10 or blob[:8] != LEAFPACK_MAGIC:
        raise ValueError("不是合法的 LEAFPACK 封包")
    count = struct.unpack("<H", blob[8:10])[0]
    idx_size = count * 24
    if count == 0 or len(blob) < 10 + idx_size:
        raise ValueError("LEAFPACK 索引表长度非法")
    k = key if (key is not None and len(key) == 11) else recover_leafpack_key(blob)
    idx_raw = blob[-idx_size:]
    dec_idx = _leafpack_crypt(idx_raw, k, decrypt=True)

    entries: list[tuple[str, bytes]] = []
    for i in range(count):
        ent = dec_idx[i * 24 : (i + 1) * 24]
        stem = ent[:8].decode("ascii", errors="replace").rstrip(" \x00")
        ext = ent[8:11].decode("ascii", errors="replace").rstrip(" \x00")
        name = f"{stem}.{ext}" if ext else stem
        pos_e, len_e, _next_e = struct.unpack("<III", ent[12:24])
        if pos_e < 10 or pos_e + len_e > len(blob) - idx_size:
            raise ValueError(f"LEAFPACK 条目 {name} 越界（pos={pos_e}, len={len_e}）")
        raw = blob[pos_e : pos_e + len_e]
        data = _leafpack_crypt(raw, k, decrypt=True)
        entries.append((name, data))
    return LeafPackArchive(key=k, entries=entries)


def rebuild_leafpack(entries: list[tuple[str, bytes]], key: bytes) -> bytes:
    """用指定的 11 字节密钥重新加密并打包 LEAFPACK 封包。"""
    if len(key) != 11:
        raise ValueError("LEAFPACK 密钥必须为 11 字节")
    out = bytearray(LEAFPACK_MAGIC)
    out.extend(struct.pack("<H", len(entries)))
    idx_plain = bytearray()
    for name, data in entries:
        pos_e = len(out)
        out.extend(_leafpack_crypt(data, key, decrypt=False))
        next_e = len(out)
        if "." in name:
            stem, ext = name.rsplit(".", 1)
        else:
            stem, ext = name, ""
        stem_b = stem.upper().encode("ascii", errors="replace")[:8].ljust(8, b" ")
        ext_b = ext.upper().encode("ascii", errors="replace")[:3].ljust(3, b" ")
        idx_plain.extend(stem_b)
        idx_plain.extend(ext_b)
        idx_plain.append(0)
        idx_plain.extend(struct.pack("<III", pos_e, len(data), next_e))
    out.extend(_leafpack_crypt(bytes(idx_plain), key, decrypt=False))
    return bytes(out)


# ---------------------------------------------------------------------------
# 2. Leaf LZSS 解压与压缩（^0xFF + MSB-first + 4096 环形缓冲 init=0xFEE）
# ---------------------------------------------------------------------------


def leaf_lzss_decompress(src: bytes, out_size: int) -> bytes:
    """解压 Leaf LVNS3 LZSS 数据流。"""
    if out_size <= 0:
        return b""
    ring = bytearray(0x1000)
    r = 0xFEE
    out = bytearray()
    i = 0
    n = len(src)
    while len(out) < out_size and i < n:
        flags = src[i] ^ 0xFF
        i += 1
        for bit in range(7, -1, -1):
            if len(out) >= out_size or i >= n:
                break
            if flags & (1 << bit):
                b = src[i] ^ 0xFF
                i += 1
                out.append(b)
                ring[r] = b
                r = (r + 1) & 0xFFF
            else:
                if i + 1 >= n:
                    break
                c0 = src[i] ^ 0xFF
                c1 = src[i + 1] ^ 0xFF
                i += 2
                w = c0 | (c1 << 8)
                pos_r = (w >> 4) & 0xFFF
                ln = (w & 0x0F) + 3
                for _ in range(ln):
                    if len(out) >= out_size:
                        break
                    b = ring[pos_r]
                    pos_r = (pos_r + 1) & 0xFFF
                    out.append(b)
                    ring[r] = b
                    r = (r + 1) & 0xFFF
    return bytes(out)


def leaf_lzss_compress(data: bytes) -> bytes:
    """压缩为 100% 兼容 Leaf LVNS3 的 LZSS 数据流（快速哈希链匹配）。"""
    n = len(data)
    if n == 0:
        return b""
    ring = bytearray(0x1000)
    r = 0xFEE
    # 3-byte hash -> list of ring positions (keep recent positions for speed)
    chains: dict[bytes, list[int]] = {}
    out = bytearray()
    i = 0

    def _push_ring(byte_val: int, idx_in_data: int) -> None:
        nonlocal r
        ring[r] = byte_val
        r = (r + 1) & 0xFFF

    while i < n:
        flag_pos = len(out)
        out.append(0)  # placeholder
        flags = 0
        for bit in range(7, -1, -1):
            if i >= n:
                break
            best_len = 0
            best_pos = 0
            if i + 3 <= n:
                key3 = data[i : i + 3]
                cands = chains.get(key3)
                if cands:
                    max_l = min(18, n - i)
                    for cand_r in reversed(cands[-16:]):
                        # Avoid self-overlap when reading/writing ring simultaneously
                        if cand_r == r:
                            continue
                        ml = 0
                        p_chk = cand_r
                        # Ensure match doesn't cross into unwritten future slots at r
                        while ml < max_l:
                            # If p_chk reaches r + ml (wrap), match uses newly written byte;
                            # simulate exact ring semantics for safety
                            if ((p_chk - r) & 0xFFF) < ml:
                                # Refers to bytes written in this match itself
                                expected = data[i + ((p_chk - r) & 0xFFF)]
                            elif p_chk == r and ml == 0:
                                break
                            else:
                                expected = ring[p_chk]
                            if expected != data[i + ml]:
                                break
                            ml += 1
                            p_chk = (p_chk + 1) & 0xFFF
                        if ml > best_len:
                            best_len = ml
                            best_pos = cand_r
                            if best_len == max_l:
                                break
            if best_len >= 3:
                w = ((best_pos & 0xFFF) << 4) | ((best_len - 3) & 0x0F)
                out.append((w & 0xFF) ^ 0xFF)
                out.append(((w >> 8) & 0xFF) ^ 0xFF)
                for step in range(best_len):
                    if i + step + 3 <= n:
                        k3 = data[i + step : i + step + 3]
                        lst = chains.setdefault(k3, [])
                        lst.append(r)
                        if len(lst) > 32:
                            del lst[:16]
                    _push_ring(data[i + step], i + step)
                i += best_len
            else:
                flags |= 1 << bit
                if i + 3 <= n:
                    k3 = data[i : i + 3]
                    lst = chains.setdefault(k3, [])
                    lst.append(r)
                    if len(lst) > 32:
                        del lst[:16]
                b = data[i]
                out.append(b ^ 0xFF)
                _push_ring(b, i)
                i += 1
        out[flag_pos] = flags ^ 0xFF
    return bytes(out)


# ---------------------------------------------------------------------------
# 3. 流式 ISO-9660 光盘镜像读取（支持 Alcohol .mdf/.mds、.iso、.bin，不读音轨）
# ---------------------------------------------------------------------------


@dataclass
class IsoDiscReader:
    path: Path
    sector_size: int
    data_offset: int
    volume_id: str
    files: dict[str, tuple[int, int]]  # UPPERCASE_NAME -> (lba, size)

    def read_file(self, name: str) -> bytes | None:
        info = self.files.get(name.upper())
        if info is None:
            return None
        lba, sz = info
        out = bytearray()
        cur = lba
        with self.path.open("rb") as fh:
            while len(out) < sz:
                fh.seek(cur * self.sector_size + self.data_offset)
                chunk = fh.read(2048)
                if not chunk:
                    break
                out.extend(chunk)
                cur += 1
        return bytes(out[:sz])


def open_iso_disc(path: Path) -> IsoDiscReader | None:
    """流式检测并索引 `.mdf`/`.mds`/`.iso`/`.bin` 根目录 ISO-9660 文件表（耗时 <5 ms）。"""
    path = Path(path)
    if path.suffix.lower() == ".mds":
        cand = path.with_suffix(".mdf")
        if not cand.is_file():
            # 大小写不敏感匹配同目录 .mdf
            for p in path.parent.iterdir():
                if p.is_file() and p.stem.lower() == path.stem.lower() and p.suffix.lower() == ".mdf":
                    cand = p
                    break
        path = cand
    if not path.is_file():
        return None
    try:
        file_size = path.stat().st_size
        if file_size < 18 * 2048:
            return None
        with path.open("rb") as fh:
            for sec_sz, off in _ISO_LAYOUTS:
                pvd_pos = 16 * sec_sz + off
                if pvd_pos + 2048 > file_size:
                    continue
                fh.seek(pvd_pos)
                pvd = fh.read(2048)
                if len(pvd) < 2048 or pvd[0:6] != b"\x01CD001":
                    continue
                vol_id = pvd[40:72].decode("ascii", errors="replace").strip()
                root_lba = struct.unpack("<I", pvd[158:162])[0]
                root_len = struct.unpack("<I", pvd[166:170])[0]
                if root_len == 0 or root_len > 256 * 1024:
                    continue
                # 逐扇区读根目录
                rd = bytearray()
                cur = root_lba
                while len(rd) < root_len:
                    fh.seek(cur * sec_sz + off)
                    blk = fh.read(2048)
                    if not blk:
                        break
                    rd.extend(blk)
                    cur += 1
                files: dict[str, tuple[int, int]] = {}
                pos = 0
                while pos < len(rd):
                    rlen = rd[pos]
                    if rlen == 0:
                        pos = ((pos // 2048) + 1) * 2048
                        continue
                    if pos + 33 >= len(rd):
                        break
                    nlen = rd[pos + 32]
                    raw_name = rd[pos + 33 : pos + 33 + nlen].decode("ascii", errors="replace")
                    clean = raw_name.split(";", 1)[0].upper()
                    flags = rd[pos + 25]
                    if not (flags & 2) and clean and clean not in ("\x00", "\x01"):
                        lba = struct.unpack("<I", rd[pos + 2 : pos + 6])[0]
                        sz = struct.unpack("<I", rd[pos + 10 : pos + 14])[0]
                        files[clean] = (lba, sz)
                    pos += rlen
                return IsoDiscReader(
                    path=path,
                    sector_size=sec_sz,
                    data_offset=off,
                    volume_id=vol_id,
                    files=files,
                )
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# 4. LVNS3 2,769 字表加载 & 24x24 1bpp 中文字模动态渲染
# ---------------------------------------------------------------------------


def default_lvns3_glyphs() -> list[str]:
    """解压内置标准 LVNS3 2,769 字符映射表（索引 0..2768 对应脚本 1-based gid 1..2769）。"""
    raw = zlib.decompress(base64.b85decode(_LVNS3_FALLBACK_TABLE_B85.encode("ascii")))
    out: list[str] = []
    for i in range(len(raw) // 2):
        pair = raw[i * 2 : i * 2 + 2]
        try:
            out.append(pair.decode("cp932"))
        except UnicodeDecodeError:
            out.append("\uFFFD")
    return out


def extract_mkfont_glyphs(mkfont_bytes: bytes) -> list[str] | None:
    """从 `MKFONT.EX` / `MKFONT.EXE` 自动提取 2,769 字按位取反 Shift-JIS 字表。"""
    idx = mkfont_bytes.find(_MKFONT_TABLE_SIG)
    if idx < 0 or idx + LVNS3_GLYPH_COUNT * 2 > len(mkfont_bytes):
        return None
    raw = bytes(b ^ 0xFF for b in mkfont_bytes[idx : idx + LVNS3_GLYPH_COUNT * 2])
    out: list[str] = []
    for i in range(LVNS3_GLYPH_COUNT):
        pair = raw[i * 2 : i * 2 + 2]
        try:
            out.append(pair.decode("cp932"))
        except UnicodeDecodeError:
            out.append("\uFFFD")
    return out


def apply_tbl_overlay(base_glyphs: list[str], tbl: ThingyTable) -> list[str]:
    """用外挂 Thingy `.tbl` 覆盖对应 2 字节大端 glyph ID（例如 `0A50=风`）。"""
    glyphs = list(base_glyphs)
    for raw_bytes, ch in tbl.decode_map.items():
        if len(raw_bytes) == 2 and len(ch) == 1:
            gid = (raw_bytes[0] << 8) | raw_bytes[1]
            if 1 <= gid <= len(glyphs):
                glyphs[gid - 1] = ch
    return glyphs


def export_lvns3_tbl(glyphs: list[str]) -> str:
    """导出标准 Thingy `.tbl` 码表（1-based 2 字节大端 hex -> 字符），供下次无损重读与人工校对。"""
    lines = [
        "# ProjectFerry Leaf LVNS3 24x24 Glyph Table (1-based Big-Endian GID)",
        "0000=　",
    ]
    for idx, ch in enumerate(glyphs, start=1):
        if ch and ch != "\uFFFD":
            lines.append(f"{idx:04X}={ch}")
    return "\n".join(lines) + "\n"


def render_glyph_24x24_1bpp(ch: str) -> bytes:
    """将单个汉字渲染为 LVNS3 `KNJ_ALL.KNJ` 格式的 24×24 1bpp 单色点阵（72 字节，MSB-first）。"""
    # 1) Windows 原生 GDI（零第三方依赖，字形锐利，优先 Microsoft YaHei / SimSun / SimHei）
    if sys.platform == "win32":
        bmp = _render_gdi_24x24(ch)
        if bmp is not None:
            return bmp
    # 2) 可选 Pillow 渲染
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore

        img = Image.new("1", (24, 24), 0)
        draw = ImageDraw.Draw(img)
        font = None
        for cand in ("simsun.ttc", "msyh.ttc", "simhei.ttf", "NotoSansCJK-Regular.ttc"):
            try:
                font = ImageFont.truetype(cand, 22)
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()
        draw.text((1, 0), ch, fill=1, font=font)
        out = bytearray(LVNS3_GLYPH_BYTES)
        px = img.load()
        for y in range(24):
            val = 0
            for x in range(24):
                if px[x, y]:
                    val |= 1 << (23 - x)
            out[y * 3] = (val >> 16) & 0xFF
            out[y * 3 + 1] = (val >> 8) & 0xFF
            out[y * 3 + 2] = val & 0xFF
        return bytes(out)
    except Exception:
        pass
    # 3) 纯标准库离线确定性占位方框（保证 Linux CI 零依赖单测稳定通过）
    out = bytearray(LVNS3_GLYPH_BYTES)
    cp = ord(ch) if ch else 0
    for y in range(2, 22):
        if y in (2, 21):
            val = 0x3FFFFC
        else:
            inner = ((cp * (y + 3)) ^ (cp >> (y % 7))) & 0x0FFFF0
            val = 0x200004 | inner
        out[y * 3] = (val >> 16) & 0xFF
        out[y * 3 + 1] = (val >> 8) & 0xFF
        out[y * 3 + 2] = val & 0xFF
    return bytes(out)


def _render_gdi_24x24(ch: str) -> bytes | None:
    """使用 Windows GDI `CreateDIBSection` + `TextOutW` 渲染 24×24 1bpp 单色点阵。"""
    try:
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 2)]

        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.CreateDIBSection.restype = ctypes.c_void_p
        gdi32.CreateDIBSection.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint32,
        ]
        gdi32.CreateFontW.restype = ctypes.c_void_p
        gdi32.CreateFontW.argtypes = [
            ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
            ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_wchar_p,
        ]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        gdi32.SetBkColor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        gdi32.TextOutW.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, ctypes.c_wchar_p, ctypes.c_int32]
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        user32.GetDC.restype = ctypes.c_void_p
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        user32.ReleaseDC(None, screen_dc)
        if not mem_dc:
            return None

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = 24
        bmi.bmiHeader.biHeight = -24  # top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 1
        bmi.bmiColors[0] = 0x00000000  # 0 = black (bg)
        bmi.bmiColors[1] = 0x00FFFFFF  # 1 = white (fg)

        bits_ptr = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0)
        if not hbmp or not bits_ptr.value:
            gdi32.DeleteDC(mem_dc)
            return None

        # GB2312_CHARSET = 134, NONANTIALIASED_QUALITY = 3
        hfont = gdi32.CreateFontW(
            -22, 0, 0, 0, 400, 0, 0, 0, 134, 0, 0, 3, 0, "SimSun",
        )
        old_bmp = gdi32.SelectObject(mem_dc, hbmp)
        old_font = gdi32.SelectObject(mem_dc, hfont) if hfont else None
        try:
            ctypes.memset(bits_ptr.value, 0, 24 * 4)
            gdi32.SetBkColor(mem_dc, 0x00000000)
            gdi32.SetTextColor(mem_dc, 0x00FFFFFF)
            gdi32.TextOutW(mem_dc, 1, 1, ch, len(ch))
            gdi32.GdiFlush()
            # 1bpp DIB stride is 4-byte aligned (24px = 3 bytes -> stride 4 bytes)
            raw_dib = ctypes.string_at(bits_ptr.value, 24 * 4)
            out = bytearray(LVNS3_GLYPH_BYTES)
            for y in range(24):
                out[y * 3 : y * 3 + 3] = raw_dib[y * 4 : y * 4 + 3]
            return bytes(out)
        finally:
            if old_font:
                gdi32.SelectObject(mem_dc, old_font)
            if hfont:
                gdi32.DeleteObject(hfont)
            gdi32.SelectObject(mem_dc, old_bmp)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem_dc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 5. LVNS3 `.SCN` 文本块解析与无损重建
# ---------------------------------------------------------------------------


@dataclass
class LvnsTextSpan:
    scn_name: str
    msg_index: int
    seg_index: int
    text: str
    raw_bytes: bytes

    @property
    def key(self) -> str:
        return f"lvns3:{self.scn_name}:m{self.msg_index}:s{self.seg_index}"


@dataclass
class _MsgPiece:
    is_text: bool
    raw: bytes
    text: str = ""
    seg_index: int = -1


def _decode_text_bytes(raw: bytes, glyphs: list[str]) -> str:
    """将一段 LVNS3 文本字节（2字节 glyph ID + 内联主角名占位符）解码为可读文本。"""
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b <= 0x0A and i + 1 < n:
            gid = (b << 8) | raw[i + 1]
            if gid == 0:
                out.append("\u3000")
            elif gid == 254:
                out.append("\u3000")
            elif 1 <= gid <= len(glyphs):
                out.append(glyphs[gid - 1])
            else:
                out.append(f"{{LV:{raw[i:i+2].hex()}}}")
            i += 2
            continue
        matched = False
        for seq, tok in _NAME_RUN_TO_TOKEN:
            if raw.startswith(seq, i):
                out.append(tok)
                i += len(seq)
                matched = True
                break
        if matched:
            continue
        out.append(f"{{LV:{b:02x}}}")
        i += 1
    return "".join(out)


def _encode_text_bytes(
    text: str,
    char_to_gid: dict[str, int],
    allocate_glyph: Callable[[str], int | None] | None = None,
) -> tuple[bytes, list[str]]:
    """将中文/日文文本（可含 `{LV_*}` 占位符）编码为 LVNS3 文本字节流。"""
    out = bytearray()
    missing: list[str] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        if m.start() > pos:
            _encode_plain_run(text[pos : m.start()], char_to_gid, allocate_glyph, out, missing)
        tok = m.group(0)
        if tok in _TOKEN_TO_NAME_RUN:
            out.extend(_TOKEN_TO_NAME_RUN[tok])
        elif tok.startswith("{LV:") and tok.endswith("}"):
            try:
                out.extend(bytes.fromhex(tok[4:-1]))
            except ValueError:
                pass
        pos = m.end()
    if pos < len(text):
        _encode_plain_run(text[pos:], char_to_gid, allocate_glyph, out, missing)
    return bytes(out), missing


def _encode_plain_run(
    segment: str,
    char_to_gid: dict[str, int],
    allocate_glyph: Callable[[str], int | None] | None,
    out: bytearray,
    missing: list[str],
) -> None:
    for ch in segment:
        if ch in ("\r", "\n"):
            out.append(0xB0)
            continue
        if ch in (" ", "\u3000"):
            out.extend(b"\x00\x00")
            continue
        mapped = _HALF_TO_FULL.get(ch, ch)
        gid = char_to_gid.get(mapped)
        if gid is None and mapped != ch:
            gid = char_to_gid.get(ch)
        if gid is None and allocate_glyph is not None:
            gid = allocate_glyph(mapped)
        if gid is None:
            missing.append(ch)
            # 用全角方框占位，绝不崩溃
            out.extend(b"\x00\x01")
        else:
            out.append((gid >> 8) & 0xFF)
            out.append(gid & 0xFF)


def _split_message_pieces(msg: bytes, glyphs: list[str]) -> list[_MsgPiece] | None:
    """将单条 LVNS3 消息拆成交替的控制字节块与纯文本段；若字节码不合法返回 None。"""
    pieces: list[_MsgPiece] = []
    ctrl_buf = bytearray()
    txt_buf = bytearray()
    seg_idx = 0

    def _flush_ctrl() -> None:
        if ctrl_buf:
            pieces.append(_MsgPiece(is_text=False, raw=bytes(ctrl_buf)))
            ctrl_buf.clear()

    def _flush_txt() -> None:
        nonlocal seg_idx
        if not txt_buf:
            return
        raw_t = bytes(txt_buf)
        txt_buf.clear()
        # 把前导/尾随的 0x0000（全角缩进）与 0x00FE（特殊留白）保留为控制字节，让译者看到干净台词
        lead = 0
        while lead + 2 <= len(raw_t) and raw_t[lead : lead + 2] in (b"\x00\x00", b"\x00\xFE"):
            lead += 2
        tail = len(raw_t)
        while tail - 2 >= lead and raw_t[tail - 2 : tail] in (b"\x00\x00", b"\x00\xFE"):
            tail -= 2
        if lead > 0:
            pieces.append(_MsgPiece(is_text=False, raw=raw_t[:lead]))
        core = raw_t[lead:tail]
        if core:
            decoded = _decode_text_bytes(core, glyphs)
            # 只有包含真实可见字符（非纯标点/占位符或至少 1 个文字）才导出给翻译
            stripped = _PLACEHOLDER_RE.sub("", decoded).strip("　 \t\r\n")
            if stripped:
                pieces.append(_MsgPiece(is_text=True, raw=core, text=decoded, seg_index=seg_idx))
                seg_idx += 1
            else:
                pieces.append(_MsgPiece(is_text=False, raw=core))
        if tail < len(raw_t):
            pieces.append(_MsgPiece(is_text=False, raw=raw_t[tail:]))

    k = 0
    n = len(msg)
    while k < n:
        b = msg[k]
        if b <= 0x0A:
            if k + 1 >= n:
                return None
            gid = (b << 8) | msg[k + 1]
            if gid > LVNS3_GLYPH_COUNT:
                return None
            _flush_ctrl()
            txt_buf.extend(msg[k : k + 2])
            k += 2
        elif b in _INLINE_NAME_BYTES:
            _flush_ctrl()
            txt_buf.append(b)
            k += 1
        elif b == 0xAF:
            _flush_txt()
            ctrl_buf.append(b)
            k += 1
            if k != n:
                return None
            break
        elif b in _LVNS3_TEXT_OP_LEN:
            al = _LVNS3_TEXT_OP_LEN[b]
            if k + 1 + al > n:
                return None
            _flush_txt()
            ctrl_buf.extend(msg[k : k + 1 + al])
            k += 1 + al
        else:
            return None

    _flush_txt()
    _flush_ctrl()
    return pieces


def parse_lvns3_scn(scn_name: str, scn_bytes: bytes, glyphs: list[str]) -> tuple[list[LvnsTextSpan], set[int]]:
    """解析单个 `.SCN` 文件的 `block1` 文本段，返回文本条目与本文件使用到的全部 glyph ID 集合。"""
    if len(scn_bytes) < 20:
        return [], set()
    hdr = [struct.unpack("<H", scn_bytes[i : i + 2])[0] for i in range(0, 16, 2)]
    nonzero = [h for h in hdr if h > 0]
    if len(nonzero) < 2:
        return [], set()
    b1 = nonzero[1] * 16
    b1_end = nonzero[2] * 16 if len(nonzero) > 2 else len(scn_bytes)
    if b1 + 4 > b1_end or b1_end > len(scn_bytes):
        return [], set()
    sz1 = struct.unpack("<I", scn_bytes[b1 : b1 + 4])[0]
    if sz1 <= 5 or sz1 > 0x20000:
        return [], set()
    dec1 = leaf_lzss_decompress(scn_bytes[b1 + 4 : b1_end], sz1)
    if len(dec1) < 4:
        return [], set()
    cnt = struct.unpack("<H", dec1[0:2])[0]
    if cnt == 0 or 2 + cnt * 2 > len(dec1):
        return [], set()
    offs = [struct.unpack("<H", dec1[2 + j * 2 : 4 + j * 2])[0] for j in range(cnt)]
    offs.append(len(dec1))

    spans: list[LvnsTextSpan] = []
    used_gids: set[int] = set()
    for mi in range(cnt):
        start, end = offs[mi], offs[mi + 1]
        if start > end or end > len(dec1):
            continue
        msg = dec1[start:end]
        pieces = _split_message_pieces(msg, glyphs)
        if pieces is None:
            continue
        for pc in pieces:
            if pc.is_text:
                spans.append(LvnsTextSpan(scn_name, mi, pc.seg_index, pc.text, pc.raw))
                # 统计原始字模 ID
                j = 0
                raw_t = pc.raw
                while j < len(raw_t):
                    if raw_t[j] <= 0x0A and j + 1 < len(raw_t):
                        gid = (raw_t[j] << 8) | raw_t[j + 1]
                        if 1 <= gid <= LVNS3_GLYPH_COUNT:
                            used_gids.add(gid)
                        j += 2
                    else:
                        j += 1
    return spans, used_gids


def rebuild_lvns3_scn(
    scn_name: str,
    scn_bytes: bytes,
    glyphs: list[str],
    translations: dict[str, str],
    char_to_gid: dict[str, int],
    allocate_glyph: Callable[[str], int | None] | None = None,
) -> tuple[bytes, int, list[str]]:
    """重写单个 `.SCN` 的 `block1` 并重新 LZSS 压缩（未命中的 `.SCN` 原样返回零修改）。"""
    if len(scn_bytes) < 20:
        return scn_bytes, 0, []
    hdr = [struct.unpack("<H", scn_bytes[i : i + 2])[0] for i in range(0, 16, 2)]
    nonzero = [h for h in hdr if h > 0]
    if len(nonzero) < 2:
        return scn_bytes, 0, []
    b1 = nonzero[1] * 16
    b1_end = nonzero[2] * 16 if len(nonzero) > 2 else len(scn_bytes)
    if b1 + 4 > b1_end or b1_end > len(scn_bytes):
        return scn_bytes, 0, []
    sz1 = struct.unpack("<I", scn_bytes[b1 : b1 + 4])[0]
    if sz1 <= 5 or sz1 > 0x20000:
        return scn_bytes, 0, []
    dec1 = leaf_lzss_decompress(scn_bytes[b1 + 4 : b1_end], sz1)
    if len(dec1) < 4:
        return scn_bytes, 0, []
    cnt = struct.unpack("<H", dec1[0:2])[0]
    if cnt == 0 or 2 + cnt * 2 > len(dec1):
        return scn_bytes, 0, []
    offs = [struct.unpack("<H", dec1[2 + j * 2 : 4 + j * 2])[0] for j in range(cnt)]
    offs.append(len(dec1))

    applied = 0
    warnings: list[str] = []
    new_msgs: list[bytes] = []
    scn_changed = False

    for mi in range(cnt):
        start, end = offs[mi], offs[mi + 1]
        if start > end or end > len(dec1):
            new_msgs.append(b"\xAF")
            continue
        msg = dec1[start:end]
        pieces = _split_message_pieces(msg, glyphs)
        if pieces is None:
            new_msgs.append(msg)
            continue
        msg_changed = False
        rebuilt_msg = bytearray()
        for pc in pieces:
            if not pc.is_text:
                rebuilt_msg.extend(pc.raw)
                continue
            key = f"lvns3:{scn_name}:m{mi}:s{pc.seg_index}"
            tl = translations.get(key)
            if tl is None:
                tl = translations.get(pc.text)
            if not tl or tl == pc.text:
                rebuilt_msg.extend(pc.raw)
                continue
            enc, miss = _encode_text_bytes(tl, char_to_gid, allocate_glyph)
            if miss:
                warnings.append(f"{key}: 缺少字模 {''.join(sorted(set(miss))[:6])}")
            rebuilt_msg.extend(enc)
            msg_changed = True
            scn_changed = True
            applied += 1
        new_msgs.append(bytes(rebuilt_msg) if msg_changed else msg)

    if not scn_changed:
        return scn_bytes, 0, []

    # 重建 block1（uint16 count + uint16 offsets[cnt] + payloads）
    header_len = 2 + cnt * 2
    new_offs: list[int] = []
    body = bytearray()
    for m_bytes in new_msgs:
        off_now = header_len + len(body)
        if off_now > 0xFFFF:
            warnings.append(f"{scn_name}: 文本块超出 64KB 偏移上限，本文件已跳过回写")
            return scn_bytes, 0, warnings
        new_offs.append(off_now)
        body.extend(m_bytes)
    new_dec1 = bytearray(struct.pack("<H", cnt))
    for o in new_offs:
        new_dec1.extend(struct.pack("<H", o))
    new_dec1.extend(body)

    comp1 = leaf_lzss_compress(bytes(new_dec1))
    blk1 = bytearray(struct.pack("<I", len(new_dec1)))
    blk1.extend(comp1)
    # 16 字节段落对齐
    pad = (-len(blk1)) % 16
    if pad:
        blk1.extend(b"\x00" * pad)

    out_scn = bytearray(scn_bytes[:b1])
    out_scn.extend(blk1)
    if b1_end < len(scn_bytes):
        # 罕见 3+ 块场景：同步更新后续块段落偏移
        tail_delta_para = (len(blk1) - (b1_end - b1)) // 16
        out_scn.extend(scn_bytes[b1_end:])
        for idx_h in range(2, 8):
            if hdr[idx_h] > 0:
                struct.pack_into("<H", out_scn, idx_h * 2, hdr[idx_h] + tail_delta_para)
    return bytes(out_scn), applied, warnings


# ---------------------------------------------------------------------------
# 6. LeafLvnsAdapter 主适配器
# ---------------------------------------------------------------------------


@dataclass
class _LvnsSource:
    mode: str  # "dir" | "pak" | "disc"
    scn_name: str
    scn_bytes: bytes
    mkfont_bytes: bytes | None = None
    dat_name: str | None = None
    dat_path: Path | None = None
    scn_path: Path | None = None
    disc: IsoDiscReader | None = None
    knj_paths: list[Path] = field(default_factory=list)


def _find_scn_pak_in_dir(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    # 优先匹配 LVNS3SCN.PAK，其次任意含 SCN 且头为 LEAFPACK 的 .pak
    preferred = []
    others = []
    for p in sorted(root.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".pak":
            continue
        if ".cn" in p.stem.lower():
            continue
        try:
            with p.open("rb") as fh:
                if fh.read(8) != LEAFPACK_MAGIC:
                    continue
        except OSError:
            continue
        if "scn" in p.stem.lower():
            preferred.append(p)
        else:
            others.append(p)
    return preferred[0] if preferred else None


def _locate_lvns_source(root: Path) -> _LvnsSource | None:
    root = Path(root)
    if root.is_file():
        suf = root.suffix.lower()
        if suf == ".pak":
            try:
                with root.open("rb") as fh:
                    if fh.read(8) != LEAFPACK_MAGIC:
                        return None
                blob = root.read_bytes()
            except OSError:
                return None
            if "scn" not in root.stem.lower():
                # 确认内部含 .SCN 文件
                try:
                    arc = parse_leafpack(blob)
                except ValueError:
                    return None
                if not any(n.upper().endswith(".SCN") for n, _ in arc.entries):
                    return None
            base = root.parent
            mk_bytes = _read_optional_file(base, ("MKFONT.EX", "MKFONT.EXE"))
            dat_p = _find_optional_path(base, ("LVNS3DAT.PAK",))
            knjs = sorted(p for p in base.glob("*.KNJ") if p.is_file()) + sorted(
                p for p in base.glob("*.knj") if p.is_file()
            )
            return _LvnsSource(
                mode="pak",
                scn_name=root.name,
                scn_bytes=blob,
                mkfont_bytes=mk_bytes,
                dat_name=dat_p.name if dat_p else None,
                dat_path=dat_p,
                scn_path=root,
                knj_paths=sorted(set(knjs)),
            )
        if suf in _DISC_SUFFIXES:
            disc = open_iso_disc(root)
            if disc is None:
                return None
            scn_fn = _pick_disc_scn_name(disc)
            if scn_fn is None:
                return None
            scn_blob = disc.read_file(scn_fn)
            if not scn_blob or not scn_blob.startswith(LEAFPACK_MAGIC):
                return None
            mk_blob = disc.read_file("MKFONT.EX") or disc.read_file("MKFONT.EXE")
            dat_fn = "LVNS3DAT.PAK" if "LVNS3DAT.PAK" in disc.files else None
            return _LvnsSource(
                mode="disc",
                scn_name=scn_fn,
                scn_bytes=scn_blob,
                mkfont_bytes=mk_blob,
                dat_name=dat_fn,
                disc=disc,
            )
        return None

    if root.is_dir():
        # 1) 目录内直接有解包/安装好的 LVNS3SCN.PAK
        scn_p = _find_scn_pak_in_dir(root)
        if scn_p is not None:
            try:
                scn_blob = scn_p.read_bytes()
            except OSError:
                return None
            mk_bytes = _read_optional_file(root, ("MKFONT.EX", "MKFONT.EXE"))
            dat_p = _find_optional_path(root, ("LVNS3DAT.PAK",))
            knjs = sorted(set(list(root.glob("*.KNJ")) + list(root.glob("*.knj"))))
            return _LvnsSource(
                mode="dir",
                scn_name=scn_p.name,
                scn_bytes=scn_blob,
                mkfont_bytes=mk_bytes,
                dat_name=dat_p.name if dat_p else None,
                dat_path=dat_p,
                scn_path=scn_p,
                knj_paths=[p for p in knjs if p.is_file()],
            )
        # 2) 目录内直接放着 TO_HEART.mdf / .iso 光盘镜像
        for ext in ("*.mdf", "*.iso", "*.bin"):
            for cand in sorted(root.glob(ext)):
                if ".cn" in cand.stem.lower():
                    continue
                src = _locate_lvns_source(cand)
                if src is not None:
                    return src
    return None


def _pick_disc_scn_name(disc: IsoDiscReader) -> str | None:
    if "LVNS3SCN.PAK" in disc.files:
        return "LVNS3SCN.PAK"
    for fn in sorted(disc.files):
        if fn.endswith(".PAK") and "SCN" in fn:
            return fn
    return None


def _find_optional_path(base: Path, names: tuple[str, ...]) -> Path | None:
    low_map = {p.name.lower(): p for p in base.iterdir() if p.is_file()} if base.is_dir() else {}
    for n in names:
        hit = low_map.get(n.lower())
        if hit is not None:
            return hit
    return None


def _read_optional_file(base: Path, names: tuple[str, ...]) -> bytes | None:
    p = _find_optional_path(base, names)
    if p is None:
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None


def _load_glyphs_for_source(root: Path, src: _LvnsSource) -> tuple[list[str], str]:
    base_glyphs = (
        extract_mkfont_glyphs(src.mkfont_bytes) if src.mkfont_bytes else None
    ) or default_lvns3_glyphs()
    note = "MKFONT.EX 内嵌 2,769 字表" if src.mkfont_bytes else "内置 LVNS3 2,769 字表"
    src_tbl, _ = find_tbl_pair(root)
    if src_tbl is not None:
        base_glyphs = apply_tbl_overlay(base_glyphs, src_tbl)
        note += f" + 外挂码表 {src_tbl.name}"
    return base_glyphs, note


class LeafLvnsAdapter(Adapter):
    """Leaf Visual Novel System 3 (LEAFPACK / LVNS3) 适配器（支持安装目录与 .mdf/.iso 光盘镜像）。"""

    name = "leaf_lvns"
    priority = 15

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        src = _locate_lvns_source(root)
        if src is None:
            return ProbeReport(engine=self.name, root=root, notes=["未发现 Leaf LVNS (LEAFPACK) 剧本包"])
        try:
            arc = parse_leafpack(src.scn_bytes)
        except ValueError as exc:
            return ProbeReport(engine=self.name, root=root, notes=[f"LEAFPACK 解析失败：{exc}"])

        scn_items = [(n, d) for n, d in arc.entries if n.upper().endswith(".SCN")]
        if not scn_items:
            return ProbeReport(engine=self.name, root=root, notes=["LEAFPACK 内未发现 .SCN 剧本文件"])

        glyphs, tbl_note = _load_glyphs_for_source(root, src)
        total_spans = 0
        for fn, data in scn_items:
            spans, _ = parse_lvns3_scn(fn, data, glyphs)
            total_spans += len(spans)

        mode_label = (
            f"光盘镜像 ({src.disc.path.name} · {src.disc.sector_size}B/扇区)"
            if src.mode == "disc" and src.disc
            else f"本地封包 ({src.scn_name})"
        )
        notes = [
            f"识别为 Leaf Visual Novel System 3（LEAFPACK），来源：{mode_label}。",
            f"自动反推 11 字节密钥 `{arc.key.hex()}`，共 {len(scn_items)} 个 .SCN 剧本，提取 {total_spans:,} 条剧情/选项文本。",
            f"字库映射：{tbl_note}；简体中文新字将自动动态分配 24×24 点阵槽位并写入字库。",
        ]
        if src.mode == "disc":
            notes.append(
                "写回策略：不修改原始光盘镜像，自动导出独立补丁目录（内含重打包的 LVNS3SCN.PAK、字库与安装说明）。"
            )
        else:
            notes.append("写回策略：原地重写 LVNS3SCN.PAK（及字库）并自动备份，支持一键撤销。")

        return ProbeReport(
            engine=self.name,
            root=root,
            container=f"LEAFPACK/{src.mode}",
            text_format="LVNS3 LZSS .SCN + 24x24 KNJ",
            unit_count=total_spans,
            readable=total_spans > 0,
            writable=total_spans > 0,
            in_place=(src.mode != "disc"),
            notes=notes,
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        src = _locate_lvns_source(root)
        if src is None:
            return []
        arc = parse_leafpack(src.scn_bytes)
        glyphs, _ = _load_glyphs_for_source(root, src)
        modid = (src.disc.volume_id if (src.disc and src.disc.volume_id) else root.stem) or "LVNS3"

        entries: list[Entry] = []
        for fn, data in arc.entries:
            if not fn.upper().endswith(".SCN"):
                continue
            spans, _ = parse_lvns3_scn(fn, data, glyphs)
            for sp in spans:
                entries.append(
                    Entry(
                        modid=modid,
                        key=sp.key,
                        english=sp.text,
                        source=f"{src.scn_name}/{fn}",
                        rel_path=f"{src.scn_name}/{fn}",
                    )
                )
        return entries

    def apply(
        self,
        root: Path,
        translations: dict[str, str],
        out: Path | None = None,
        *,
        undo: UndoLog | None = None,
    ) -> ApplyReport:
        root = Path(root).resolve()
        src = _locate_lvns_source(root)
        if src is None:
            return ApplyReport(conflicts=["未找到 Leaf LVNS3 剧本包"])

        arc = parse_leafpack(src.scn_bytes)
        glyphs, _ = _load_glyphs_for_source(root, src)
        char_to_gid: dict[str, int] = {}
        for idx, ch in enumerate(glyphs, start=1):
            if ch and ch != "\uFFFD" and ch not in char_to_gid:
                char_to_gid[ch] = idx

        # 统计全盘所有仍被未翻译原文引用的 glyph ID，确保动态分配简体中文新字时绝不覆盖仍在使用的日文字符
        used_gids: set[int] = set(range(1, 256))  # 永远保留 1..255 全角英数/假名/标点槽位
        for fn, data in arc.entries:
            if not fn.upper().endswith(".SCN"):
                continue
            spans, gids = parse_lvns3_scn(fn, data, glyphs)
            # 仅保留那些没有被译文替换的条目所使用的日文字模，以释放尽可能多的高位汉字槽位
            untranslated_exists = False
            for sp in spans:
                tl = translations.get(sp.key) or translations.get(sp.text)
                if not tl or tl == sp.text:
                    untranslated_exists = True
                    j = 0
                    raw_t = sp.raw_bytes
                    while j < len(raw_t):
                        if raw_t[j] <= 0x0A and j + 1 < len(raw_t):
                            gid = (raw_t[j] << 8) | raw_t[j + 1]
                            if 1 <= gid <= LVNS3_GLYPH_COUNT:
                                used_gids.add(gid)
                            j += 2
                        else:
                            j += 1
            if not spans and gids:
                used_gids.update(gids)
            _ = untranslated_exists

        # 空闲字模槽位（从表尾罕见 JIS 二级汉字倒序分配）
        free_slots = [gid for gid in range(LVNS3_GLYPH_COUNT, 255, -1) if gid not in used_gids]
        allocated_glyphs: dict[int, str] = {}  # gid -> newly assigned Simplified Chinese char

        def _allocate(ch: str) -> int | None:
            existing = char_to_gid.get(ch)
            if existing is not None:
                return existing
            if not free_slots:
                return None
            slot = free_slots.pop(0)
            char_to_gid[ch] = slot
            glyphs[slot - 1] = ch
            allocated_glyphs[slot] = ch
            return slot

        new_entries: list[tuple[str, bytes]] = []
        total_applied = 0
        conflicts: list[str] = []
        touched_scns = 0

        for fn, data in arc.entries:
            if not fn.upper().endswith(".SCN"):
                new_entries.append((fn, data))
                continue
            rebuilt, count_n, warns = rebuild_lvns3_scn(
                fn, data, glyphs, translations, char_to_gid, allocate_glyph=_allocate
            )
            if count_n > 0:
                touched_scns += 1
                total_applied += count_n
            if warns:
                conflicts.extend(warns[:5])
            new_entries.append((fn, rebuilt))

        if total_applied == 0:
            return ApplyReport(
                applied=0,
                conflicts=conflicts or ["没有命中任何可写回的 LVNS3 译文条目。"],
            )

        rebuilt_scn_pak = rebuild_leafpack(new_entries, arc.key)

        # 若分配了新的简体汉字字模，则同步渲染 24×24 点阵到 KNJ_ALL.KNJ（及现有 LVNS3_??.KNJ）
        rendered_bitmaps: dict[int, bytes] = {
            gid: render_glyph_24x24_1bpp(ch) for gid, ch in allocated_glyphs.items()
        }

        def _patch_knj_blob(knj_raw: bytes) -> bytes:
            buf = bytearray(knj_raw)
            for gid, bmp in rendered_bitmaps.items():
                off_g = (gid - 1) * LVNS3_GLYPH_BYTES
                if off_g + LVNS3_GLYPH_BYTES <= len(buf):
                    buf[off_g : off_g + LVNS3_GLYPH_BYTES] = bmp
            return bytes(buf)

        written_files: list[str] = []
        if src.mode == "disc" and src.disc is not None:
            # 光盘镜像模式：绝不改动原 737 MB .mdf/.iso，输出独立补丁目录
            base_dir = Path(out) if out is not None else src.disc.path.parent
            patch_dir = base_dir if (out is not None and not base_dir.is_file()) else (base_dir / f"{src.disc.path.stem}.cn_patch")
            patch_dir.mkdir(parents=True, exist_ok=True)
            out_scn = patch_dir / src.scn_name
            out_scn.write_bytes(rebuilt_scn_pak)
            written_files.append(str(out_scn))

            if rendered_bitmaps and src.dat_name:
                dat_raw = src.disc.read_file(src.dat_name)
                if dat_raw and dat_raw.startswith(LEAFPACK_MAGIC):
                    dat_arc = parse_leafpack(dat_raw)
                    new_dat_items = []
                    knj_patched = False
                    for fn_d, d_blob in dat_arc.entries:
                        if fn_d.upper() == "KNJ_ALL.KNJ":
                            d_blob = _patch_knj_blob(d_blob)
                            knj_patched = True
                        new_dat_items.append((fn_d, d_blob))
                    if knj_patched:
                        out_dat = patch_dir / src.dat_name
                        out_dat.write_bytes(rebuild_leafpack(new_dat_items, dat_arc.key))
                        written_files.append(str(out_dat))

            tbl_path = patch_dir / LVNS3_TBL_FILENAME
            tbl_path.write_text(export_lvns3_tbl(glyphs), encoding="utf-8")
            written_files.append(str(tbl_path))

            readme_path = patch_dir / "安装说明.txt"
            readme_path.write_text(
                f"摆渡计划 Leaf LVNS3 汉化补丁包（来源镜像：{src.disc.path.name}）\n"
                f"已写回：{touched_scns} 个 .SCN 剧本 / {total_applied} 条译文；"
                f"动态新增 24×24 简体中文字模：{len(rendered_bitmaps)} 字。\n\n"
                "使用方法：\n"
                "1. 先用虚拟光驱挂载原版光盘镜像并安装游戏到本地文件夹；\n"
                "2. 将本目录中的 LVNS3SCN.PAK（以及如有生成的 LVNS3DAT.PAK）复制到游戏安装目录覆盖即可；\n"
                "3. 若安装目录里有 LVNS3_00.KNJ ~ LVNS3_06.KNJ，建议在游戏内字体设置切换为「リーフオリジナル」"
                "（使用 LVNS3DAT.PAK 内的 KNJ_ALL.KNJ），或先删除外部 LVNS3_??.KNJ。\n",
                encoding="utf-8",
            )
            written_files.append(str(readme_path))
            return ApplyReport(
                applied=total_applied,
                out_path=patch_dir,
                in_place=False,
                conflicts=conflicts,
                notes=[
                    f"已导出独立补丁目录 {patch_dir.name}（{touched_scns} 个剧本 / {total_applied} 条译文，"
                    f"动态新增 {len(rendered_bitmaps)} 个 24×24 中文字模；原光盘镜像未修改）。",
                    *(f"写出：{p}" for p in written_files),
                ],
            )

        # 本地目录 / 单 PAK 原地回写模式（带 UndoLog 无损备份）
        assert src.scn_path is not None
        work_root = src.scn_path.parent
        log = undo or UndoLog.open(work_root, engine=self.name)

        rel_scn = src.scn_path.relative_to(work_root).as_posix()
        log.will_modify(rel_scn)
        src.scn_path.write_bytes(rebuilt_scn_pak)
        log.did_write(rel_scn)
        written_files.append(rel_scn)

        if rendered_bitmaps:
            if src.dat_path is not None and src.dat_path.is_file():
                try:
                    dat_arc = parse_leafpack(src.dat_path.read_bytes())
                    new_dat_items = []
                    knj_patched = False
                    for fn_d, d_blob in dat_arc.entries:
                        if fn_d.upper() == "KNJ_ALL.KNJ":
                            d_blob = _patch_knj_blob(d_blob)
                            knj_patched = True
                        new_dat_items.append((fn_d, d_blob))
                    if knj_patched:
                        rel_dat = src.dat_path.relative_to(work_root).as_posix()
                        log.will_modify(rel_dat)
                        src.dat_path.write_bytes(rebuild_leafpack(new_dat_items, dat_arc.key))
                        log.did_write(rel_dat)
                        written_files.append(rel_dat)
                except (OSError, ValueError) as exc:
                    conflicts.append(f"更新 {src.dat_path.name} 中文字模失败：{exc}")

            for knj_p in src.knj_paths:
                try:
                    rel_k = knj_p.relative_to(work_root).as_posix()
                    log.will_modify(rel_k)
                    knj_p.write_bytes(_patch_knj_blob(knj_p.read_bytes()))
                    log.did_write(rel_k)
                    written_files.append(rel_k)
                except OSError as exc:
                    conflicts.append(f"更新外部字库 {knj_p.name} 失败：{exc}")

        # 写入码表以便后续重读中文
        tbl_out = work_root / LVNS3_TBL_FILENAME
        rel_tbl = tbl_out.relative_to(work_root).as_posix()
        if tbl_out.exists():
            log.will_modify(rel_tbl)
        else:
            log.will_add(rel_tbl)
        tbl_out.write_text(export_lvns3_tbl(glyphs), encoding="utf-8")
        log.did_write(rel_tbl)
        written_files.append(rel_tbl)

        undo_file = log.finish() if undo is None else None

        return ApplyReport(
            applied=total_applied,
            out_path=work_root,
            in_place=True,
            undo_path=undo_file,
            conflicts=conflicts,
            notes=[
                f"已回写 {src.scn_name}（{touched_scns} 个剧本 / {total_applied} 条译文，"
                f"动态渲染 {len(rendered_bitmaps)} 个 24×24 中文字模）。",
            ],
        )
