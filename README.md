# 摆渡计划 · ProjectFerry

> 人无语言则茫然无依，故为摆渡。

**[「渡口通信」发布页](https://redrea1ity.github.io/ProjectFerry/)** · 
## 作者真正的人话省流×碎碎念√

本质是个非科班人员做的ai项目。
最开始是做为一个mc模组汉化工具，然后还是自用啥的。后面突然想起来电脑上有一万个奇异搞笑老游戏（真的很奇异搞笑！没人碰的那种。），没中文也没人做。不求完全汉化，至少得看得明白剧情吧，怎么办，我就想着用ai吧。但上下文总是会炸膛的，不如把老游戏翻译给集合到这个mc模组汉化工具里，专职汉化。然后我就把我能记住的所有老东西小众东西还有大众东西全部一股脑的塞进去了。但人在学校没有那么多时间去测试。我的老电波神人游戏库存也都在家里，所以直到放假都会是alpha超级早期测试版（至少pc98和大部分rpg独立游戏是可以翻译和导出的）。有什么问题都可以反馈，我网瘾大。有可能哪天又想起来一堆平台然后往里塞。

顺带一提，发布页是上课老师让水的。风格原本打算走4ch的超—————级混乱风格，但因为生出了多余的感情所以改成了千禧年win9x风格。

**摆渡现在是个 Windows 游戏汉化工作台。** 左边的「游戏汉化」管视觉小说、独立游戏、老电脑和掌机游戏的文本；「模组汉化」继续给 MC 整合包补中文。两边共用翻译引擎。手里有译稿就用自己的，没买 API 也能做通用游戏的提取和回录。

至于为什么还留着机翻，就一句老话：**别人熬夜翻的字，我一个都不敢碰；没人翻的那部分，我拿 AI 顶两天班，等正主回来了我自己滚。**

## 第一次开船：先让一句话变成中文

从[下载区](https://redrea1ity.github.io/ProjectFerry/#downloads)拿 2.0 系列的 `ProjectFerry.exe`，打开后按你手里的东西选入口。旁边那个桥接 JAR 是给匹配的 MC 实例装的，不是桌面程序。

### 手里是一部游戏，跟方块没关系

假设你想翻一部没中文的视觉小说。先不管那几万句告白，拿标题画面的「はじめから」开刀。失败了重来，总比读到真结局才发现女主从头到尾都叫「□□」划算。

1. **复制游戏目录和存档**，在「游戏汉化」里选副本，点「检测游戏」。看报告说能提取什么、能不能写回；只认出引擎，没读到字，就先停。
2. 点「① 提取文本(自翻)」，留好 `ferry_tl.json`。打开旁边的 `ferry_tl.tsv`，在 `dst` 列把那一行填成「新游戏」。占位符和控制码别顺手翻了，它们不是台词。
3. 点「导入外部译文…」，选刚改的 TSV，确认重打包。按报告找到产物，开游戏回到标题画面：按钮能点、中文不缺字、没挤出框，再去翻那几万句告白。**这三步不用翻译接口。**

吉里吉里、Ren’Py、RPG Maker、Godot、Unity 都有对应的适配器；PC-98 磁盘、PSP 镜像、塞班和黑莓游戏包也有各自的读写条件。这说的是**电脑上的工作台能处理哪些文件**，不是我把这玩意儿移植去黑莓了。[按引擎查能做什么 → 通用游戏手册](docs/GAME-LOCALIZATION.md#哪些格式值得先试)

### 手里是 MC 整合包，回老家了

选「模组汉化」，指到含 `mods` 的实例目录，扫描，挑一个确实缺中文的模组试翻。进游戏启用摆渡生成的资源包，先找一件认识的物品看看。原模组 JAR 不动。

默认只要认出一个模组已经有人工中文，哪怕只翻了一半，摆渡也先跳过；要它补剩下的词，得你自己开选项。后来汉化组补上了，再扫一次，摆渡撤掉**自己写的**对应条目。自我削除谢谢喵。这个承诺只管 MC 资源包，不是说通用游戏的第三方补丁也会自己长腿跑路。

资源包启用了，物品悬浮说明还是英文？那句可能写死在代码里。自研桥的 **Forge 1.20.1 物品提示**已实机验证，聊天栏这条还没验。别为了翻个物品名先给整合包做开颅手术。[资源包、桥接和 FTB 任务书 → Minecraft 手册](docs/MINECRAFT.md)

## 几句真别跳过的

- **选版本。** [v2.0.0-alpha.5](https://github.com/RedRea1ity/ProjectFerry/releases/tag/v2.0.0-alpha.5) 是早期实验版，稳定性低于 alpha.4，新增的塞班、黑莓写回还缺实机验证。能打出个包，和那台老机器肯开机认它，是两件事。只做 MC 资源包也可以选较早的正式版。
- **留原档。** 有的格式出补丁，有的出副本，有的原地写回。干净的游戏、存档、译文表各留一份。「撤销汉化」不能替你把没备份的青春读档。
- **谁会看到剧本？** 手翻不用交给在线服务；选在线引擎，待翻文本就会发给你选的那家。未公开的剧本可以手翻或用本地模型。别把密钥、`ferry_config.json` 或整部游戏扔进公开 Issue。
- **怎么报错？** 告诉我工具版本、游戏名或引擎、你按了哪个按钮，以及一条本来该长什么样、实际又长什么样的文本。有脱敏的检测报告更好。图片上画的字、运行时现拼的句子、字库里压根没有的汉字，处理办法都不一样。

## 想拆开看的人走这边

源码运行需要带 Tk 的 Python 3.11 或更高版本。在仓库根目录执行 `py -3 ProjectFerry.pyw`。两条线的「翻译引擎」设置共用，可选 MyMemory、DeepL、自定义兼容接口或本地模型；先试几句，再决定要不要开并发。

[工作台截图](site/public/workbench.png) · [引擎配置示例](ferry_config.example.json) · [代码结构](docs/ARCHITECTURE.md) · [测试清单](.github/workflows/tests.yml)

## 更新日志

[在发布页按时间翻阅版本链](https://redrea1ity.github.io/ProjectFerry/#archive)。下方保留各版的开发记录；历史条目说明的是当时的功能和测试状态。

<details>
<summary>展开版本记录</summary>

**v2.0.0-alpha.6.1（补丁更新 · 早期实验测试版）**

- **新增 Leaf LVNS3 (`LEAFPACK`) 适配器（1997 原版《To Heart》等）**：支持直接流式读取 Alcohol 120% `.mdf/.mds` / `.iso` 光盘镜像（跳过 CD-DA 音轨）及本地安装目录，自动反推 11 字节密钥与 Leaf LZSS 压缩重建，保护 `{LV_FN}` / `{LV_LN}` 主角名占位符，并自动将简体中文新字通过 Windows GDI 渲染成 24×24 点阵写回 `KNJ_ALL.KNJ` 字库。
- **修复大体积光盘镜像卡死/爆内存隐患**：拦截单文件 >64 MB 或光盘镜像后缀误入 `rawbin` 二进制盲扫，并将 7 个适配器 `probe()` 的整文件切片读改为只读文件头字节。

**v2.0.0-alpha.6（早期实验测试版）**

- **新增 Adventure Game Studio (AGS)、世嘉土星 (Saturn)、N64、PS1 / PS2 适配器**：支持 AGS `.tra` 译文包无损重建（含 UTF-8 扩展块、外置中文字体覆盖与 `ags36` 运行时按需下载），土星/PS1/PS2 光盘 `.chd`（按需 `chdman`）与 MODE1/MODE2 EDC/ECC 扇区回写，以及 N64 `.z64/.v64/.n64/.zip` 的 CIC 硬件 CRC 重算。
- **新增通用 Thingy `.tbl` 自定义码表与相对搜索**：覆盖原始二进制与土星/N64/PS1/PS2/PSV/PSP/NDS 等多款复古主机，方便应对非标准字库排序的日文老游戏。
- **工作台与翻译管线升级**：提取文本改走后台线程并联动进度条、按 ROM 隔离 `ferry_tl` 译文表，新增世界书预分析、纯净补丁包导出、中文字体回退、翻译记忆与 Batch API / Token 压缩优化。

**v2.0.0-alpha.5（早期实验测试版 · 比 alpha.4 更不稳定）**

- **⚠️ 稳定性警示（比 `v2.0.0-alpha.4` 更不稳定）**：本版为全线覆盖塞班全世代与黑莓全世代的**早期实验测试版**，新增 `.sisx` TLV/zlib 重压缩、Qt `.qm` 重建与黑莓 `.cod/.jad/.bar` 协议回写未经大规模实机验证，稳定性低于 `v2.0.0-alpha.4`，仅供尝鲜测试与提交反馈；处理原游戏文件前请务必备份，求稳请优先使用正式版或 `v2.0.0-alpha.4`。
- **塞班全世代（S60v1 → 安娜 Anna / 贝拉 Belle）彻底打通**：
  - 内置 Symbian 9+ `.sisx` (`0x10201A7A`) 的 **TLV `SISCompressed` (`0x0018`) `zlib` 数据流自动解包与重压缩**，无需外部 `SISContents` 或 `unsis` 即可直接深入 `.sisx` 压缩块内部抽取并回写 `.cn.sisx`；
  - 新增塞班^3 / 安娜 / 贝拉（以及 PC Qt 游戏）通用的 **Qt `.qm` 编译语言包（魔数 `3C B8 64 18...`）解析器与 `UTF-16BE` 任意长度重建器**，以及 **Qt Quick `.qml` (`qsTr(...)`)** 抽取与回写。
- **新增黑莓 BlackBerry 全世代适配器 (`blackberry`)（冲至 29 类适配器）**：
  - **经典 BBOS 4.x ~ 7.1 (`.cod` + `.jad`)**：同时支持单模块裸二进制 `.cod`（魔数 `DE C0 FF FF`）与超过 64KB 的多模块 ZIP 捆绑 `.cod`；产出 `.cn.cod` 时自动同步更新 `.cn.jad` 的 `RIM-COD-Size` 并**重算 20 字节 `RIM-COD-SHA1` 校验值**；
  - **后期 BlackBerry 10 & PlayBook (`.bar`)**：支持 QNX `.bar` 容器内的 Qt Cascades `.qm / .qml`、`app/native/` 多语言词表与二进制剧本抽取回写。
- 全仓自动化回归测试扩充至 **307 项全绿**。

**v2.0.0-alpha.4（早期测试版）**

- **古董功能机、塞班、Flash 与 Win3.1/95 邪典引擎全接入（冲至 28 类适配器）**：
  - **Java ME / J2ME ("jvav" `.jar / .jad`)**：自动识别 `MIDlet-` 清单，纯标准库解析并**任意长度重建 `.class` (`0xCAFEBABE`) 的 `CONSTANT_Utf8` 常量池**，产出 `.cn.jar` 并自动同步修正 `.cn.jad` 的 `MIDlet-Jar-Size`。
  - **塞班 Symbian OS (`symbian`)**：支持 S60v1/v2/v3/v5 / UIQ / N-Gage 的 `.sis / .sisx`、`.rsc / .r01..r99` 编译资源文件以及 EPOC `E32Image` (`.app`) 可执行程序。
  - **Adobe / Macromedia Flash (`flash`)**：支持 `FWS`、`CWS` (zlib) `.swf` 以及尾部带 `0xFA123456` 封尾的 Flash Projector `.exe`，直接重建 AS1/AS2 `ActionConstantPool` (`0x88`) 与 AS3 AVM2 `DoABC` 字符串常量池，支持任意长度 UTF-8 中文。
  - **Win3.1 / Win95 / Win98 早期多媒体与老 AVG (`retro_win`)**：原生支持 **Macromedia Director (`RIFX`/`XFIR` `.dir/.dxr/.cst` `STXT` 演员文本块 —— Win3.1《东脑》《中天》同款引擎)**、**Windows 3.1 16 位 `NE` 程序**与 **AliceSoft System 1/2/3/3.5 (`*.ald` 扇区归档)**。
- **新增「① 提取文本（自翻导出）→ ② 自翻回录重打包」闭环（0 Token）**：
  - 界面与 CLI (`ferrygame extract` / `ferrygame repack`) 新增一键导出带 UTF-8 BOM 的 **`ferry_tl.tsv`**（Windows Excel 双击不乱码）与 **`ferry_tl.json`**；
  - 支持在 Excel / 记事本 / ParaTranz / 网页大模型自翻后，一键从 `.tsv / .csv / .json` 回录译文并自动重打包回 `.cn.jar / .cn.apk / .cn.ipa / .cn.iso / .cn.cso / .cn.nds / .cn.swf / .cn.hdi / .dir` 等原格式！
- 全仓自动化回归测试扩充至 **305 项全绿**。

**v2.0.0-alpha.3（早期测试版）**

- **欧美与全球主流/独立引擎补齐（冲至 24 类适配器）**：
  - **Unreal Engine 4 / 5 (`unreal`)**：纯标准库解析并重建 `.locres`（Legacy v0 与 Optimized v1/v2/v3），以 `FString` UTF-16LE 宽字符无损重建任意长度中文字符串池。
  - **欧美独立叙事引擎集群 (`narrative`)**：原生支持 **Inkle's Ink (`.ink.json`)**、**Yarn Spinner (`.yarn`)**、**Twine 互动小说 (`.html`)**、**Godot Dialogic (`.dtl`)** 与 **Adventure Game Studio (`.trs`)**。
  - **LÖVE / Love2D (`love2d`)**：支持 `.love` 归档、融合 `zip-in-exe` 与散装 `main.lua` 的 Lua 字面量智能抽取与回写。
  - **全球多编码二进制兜底 (`rawbin`)**：打破「只能扫日文 Shift-JIS」的限制，新增欧美 **UTF-8 / ASCII 自然英文句与菜单**及 **Windows UTF-16LE 宽字符**的双轨二进制扫描与定长对齐覆写。
- **深度二进制与掌机/VN 死角清扫**：
  - **Unity IL2CPP `global-metadata.dat`**：直接解析 `0xFAB11BAF` 元数据头并重建 `stringLiteral` 与 `stringLiteralData` 表，支持任意长度 UTF-8 中文静态写回（同时覆盖 PC、APK 与 IPA 内嵌的 IL2CPP 游戏）。
  - **Android `resources.arsc`**：支持直接解析与原位覆写编译后 `ResStringPool` (`0x0001`) 字符串池。
  - **PSP `.cso` (`CISO`) 与 Nintendo DS `.nds` (NitroROM)**：PSP 适配器新增 `.cso` 分扇区 deflate 透明解压与重打包；新增 `NdsAdapter` 解析 NDS 卡带 FNT/FAT 文件表并输出 `.cn.nds`。
  - **ONScripter (`nscript.dat` `0x84` XOR)、WOLF RPG Editor (`Data/*.dat/.mps`)、Artemis Engine (`.pfs / .ast`)** 全部纳入标准库适配。
- 全仓自动化回归测试扩充至 **300 项全绿**。

**v2.0.0-alpha.2（早期测试版）**

- **主流引擎全面扩编**：
  - **Kirikiri 2 / Z（吉里吉里）**：纯标准库解析并重建 `.xp3` 归档，剧本统一以 `UTF-16LE with BOM` 写出 `patch.xp3`，免转区、免 DLL 代理直出简体中文。
  - **Electron `app.asar` 容器**：实现 Chromium Pickle 头解析与 SHA-256 完整性重算，现代打包进 `resources/app.asar` 的 TyranoBuilder 与 Web 独立游戏开箱即用。
  - **RPG Maker 2000 / 2003**：实现 LCF BER 变长块隔离，只抽 `.ldb` 玩家可见块与 `.lmu` 事件块（81），彻底隔离地图图块矩阵与内部变量名。
  - **Godot Engine 3.x / 4.x**：实现 `GDPC` v1 / v2 `.pck` 解包与重建（含 MD5 重算与相对 `file_base`），支持 `.tscn` 场景文本属性抽取与回写。
  - **Unity Engine**：直接解析 `.NET` `Assembly-CSharp.dll` 的 `BSJB` 元数据根与 `#US`（User Strings）UTF-16LE 流及 `StreamingAssets` 词表，原地更新并自动生成 **XUnity.AutoTranslator** `_PreTranslated.txt` 预翻译包。
  - **通用 i18n 词表**：新增 GNU Gettext `.po`（含多行拼接）、`locales/*.json`、`i18n/*.csv` 通用适配器。
- **移动端与掌机平台极限适配**：
  - **Android `.apk`**：支持 `res/values*/strings.xml`、`assets/` 内嵌游戏剧本与 **`classes.dex` 字符串池定长原位覆写**（写回自动重算 DEX 头部 SHA-1 签名与 Adler32 校验和），重打包 `.cn.apk` 自动保持 `resources.arsc` 为 `ZIP_STORED` 并剥除失效旧签名块。
  - **iOS `.ipa` / `Payload/*.app`**：同时支持明文与 **`bplist00` 二进制 `.strings`** 无损读写，重打包 `.cn.ipa` 严格保留 Mach-O `0755` 可执行权限位。
  - **PS Vita `.vpk` / `sce_sys`**：解析 Sony `param.sfo`（`\x00PSF`）提取 `TITLE` 与 `TITLE_ID`，支持明文与裸二进制混合抽取，输出 `.cn.vpk` 或实机/Vita3K **`rePatch/<TITLE_ID>/`** 挂载补丁目录。
  - **PSP `.iso` / `PSP_GAME`**：纯标准库实现 **ISO-9660 光盘文件系统解析**，定位 `/PSP_GAME/PARAM.SFO` 与 `USRDIR` 脚本 LBA 扇区并在 `.cn.iso` 内完成扇区原位覆写。
- 全仓自动化回归测试从 105 项扩充至 **295 项全绿**。

**v2.0.0-alpha.1（早期测试版）**

- **双轨工作台重构**：正式拆分 `ferrycore/`（MC 模组线）与 `ferrygame/`（通用游戏线），重构 `ProjectFerry.pyw` 界面为双轨工作台。
- **NEC PC-98 磁盘镜像（`.hdi / .fdi / .nhd`）全自动汉化闭环**：纯标准库解析 FAT12/16 镜像，内置 524,350 字节 `anex86.bmp` 字库底板，实现 JIS 第二水准字模欺骗与 16 位汇编 `LODSW` 双字节偶对齐，并在实机模拟器跑通《上九一色村物语》。
- **未知引擎裸二进制适配器（`rawbin`）与字节侦查台（`recon`）**：支持 Shift-JIS 长度前缀感知扫描、定长补齐原位覆写、香农熵分析与 PE 导入表替身 DLL 侦查。
- **双语对照工程表 `ferry_tl.json`**：支持控制符铁律掩码、剧本滚动上下文、字节预算超长自动重试、以及手工编辑 `ferry_tl.json` 后 **0 Token 重刷写回**。

**v1.9.0-beta（预发布）**

- **桥去 Mixin（L1）**：聊天栏从 `ChatComponentMixin` 改为 Forge 官方事件 `ClientChatReceivedEvent`，并拆掉整套 Mixin / refmap / SRG 混淆映射构建链。桥现**零 Mixin**，`bridge/build.gradle` 从 290 行降至 90 行，jar 内不再有 `mixins.json` / refmap。⚠ 该改动尚未实机验证，故为预发布。唯一缺口：别的模组在客户端进程直接向聊天栏塞字（不经网络包）的消息收不到，不为它回退 Mixin。
- 自研摆渡桥（旧 Mixin 版）实机验证：聊天栏 + 物品 tooltip 两条路径都能替换（实测「已替换 4600+ 次」）。
- 大批量翻译打通，主流程改为「进游戏之前全在摆渡里做完」：扒 jar class 常量池扫出硬编码 / tooltip / 聊天栏文本 → AI 翻译 → 译完自动把映射写进游戏目录。游戏内采集降级为「开发者补漏」。
- 查表新增**格式噪声归一化**（剥 `§` 颜色码、把排版省略号 `…` 折成 `...`），治「扫出来翻了、游戏里却还是英文」。
- 修两个真 bug：①「导出桥接映射」与「导入游戏采集」整表覆盖互抹 → 改为合并；② 硬编码启发式子串匹配日志关键词，长句含 `already` 就被整句误杀 → 长句不再参与关键词过滤。
- LiteralBridge 获取方式：从「没有就打开项目页」改为**联网直接从 GitHub Release 下载**匹配 jar 装进 `mods`；网络不通或无匹配版本才回落打开项目页。下载后拆包读 `mods.toml` 复核，不匹配不落盘。
- 新增**自动升级 LiteralBridge**（默认开）：三条硬保证 —— 已是最新只查不下载；换前把旧版改名成 `xxx.jar.bak-<时间>`（绝不出现两份同 modId 的 jar）；改名失败即放弃升级。
- GitHub Release 附件国内网络约一半失败（固定卡 21 秒），改为轻量重试一次，失败直接打开项目页；不加第三方代理镜像。Release 列表缓存 5 分钟。
- 状态列措辞分清功过（见 [人工汉化优先](docs/MINECRAFT.md#人工汉化优先)）；硬编码只有还有没翻完的才挂 ⚠，翻完回归完成态。
- **API Key 加本地保护**：配置落盘用 Windows 自带 DPAPI 加密（`dpapi:` 前缀），读回自动解密；只用标准库（ctypes 调 Crypt32）。老明文配置下次启动自动加密写回。
- **仓库不再提交 `ProjectFerry.exe`**：唯一来源 `.github/workflows/release.yml`（打 `v*` tag → CI 构建 → 挂 Release）。
- **代码模块化**：`mc_ai_translator.py` 从 3809 行拆到 **58 行薄壳**，实现下沉到 `ferrycore/` 的 19 个模块。对外 API 不变（`core.xxx` 照旧）。见 `docs/ARCHITECTURE.md`。
- 顺手修两个真缺陷：① `app_dir()` 平移后目录跑偏；② `_version_key` 同文件定义两次、后者静默覆盖前者。现合并为 `ferrycore.versions.version_key` 唯一口径。
- 新增「**一键诊断**」（摆渡底部按钮 + 游戏内 F9），文件不含 API Key 的值。
- 测试从 95 项加到 105 项；修掉两个 CLI 测试的 `mock.patch` 跨模块失效（会真读本机配置、真打网络）。

**v1.8.x**

- v1.8.6：摆渡让位可被看见 —— 汉化组补译文时状态栏播报撤回了 N 条；退场记录写入包内 `AI_TRANSLATION_NOTICE.txt`（带时间戳，留最近 20 条）；撤空则删包。
- v1.8.5：桥接规则改用直填 `translation`（A/B 实测 1.2.0 仅此路可用）；规则三层加固。
- v1.8.4：导出双格式，LiteralBridge 规则加 tooltip 兜底映射。
- v1.8.3：修复「翻译选中模组」被放大成翻译全部。
- v1.8.2：修复启动即崩（引用了不存在的实例属性）。
- v1.8.1：翻译目标动态纳入硬编码候选；刷新实例保持选择。
- v1.8.0：桥接改投 LiteralBridge 格式；硬编码检测返回全量候选；自研桥冻结为兜底；导出过滤过短规则。

**v1.7.x**

- v1.7.2：「翻译硬编码文本」开关挪到常驻设置行。
- v1.7.1：硬编码文本进翻译管线（候选以 `nbt:<hash>` 为 key，译文存 `.hardcoded.json`）。
- v1.7.0：摆渡桥模组 M1（1.20.1 Forge，tooltip 显示层精确替换）加「导出桥接映射」。

**v1.6.x**

- v1.6.2：修复翻译线程被社区整合包下载卡死。
- v1.6.1：右键新增「加入不翻译名单」。
- v1.6.0：新增 GitHub 检查更新；新增「查询在线汉化」；修复社区汉化基线静默失效（改拉官方整合包，14 天缓存）。

**v1.5.0**

- 界面全面改版（渡口水色配色、卡片式布局、三步上手引导等）。
- 修复「下次不再显示」从不生效。
- 兼容修复：1.11–1.12.2（pack_format 3）写小写 `zh_cn.lang`；26.4 兜底格式更新为 99.0。
- 版本表支持联网更新。
- 新增 CI：打 `v*` tag 自动构建 exe 并发布 Release。

</details>

## 作者与许可

我是**红现实**。这玩意儿是我一个不懂编程的人逼着 AI 一砖一瓦垒出来的，所以你看到它长得磕碜，那不是风格，那就是本来面目。

代码按 [MIT 许可证](LICENSE) 开放。感谢 Mojang、PandaDevOfficial / Minecraft-All-Lang、CFPA 社区和 [LiteralBridge](https://github.com/Losketch/LiteralBridge)。社区译文的署名和许可归各自作者；也谢谢愿意进游戏对着一行字改三遍的人。

说句实在的：这工具最好的结局，是被你删掉。等官方汉化补上、等汉化组把坑填了，它就没人理了，那说明它赢了。一个永远被需要的临时替补，才叫失败。

[到渡口揭示板找我](https://redrea1ity.github.io/ProjectFerry/#forum) · [GitHub Issues](https://github.com/RedRea1ity/ProjectFerry/issues) · [给红现实发邮件](mailto:zetadarkdragon@gmail.com)
