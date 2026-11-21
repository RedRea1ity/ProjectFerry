# 摆渡计划 · ProjectFerry

**给还没有中文的小众游戏做汉化的桌面工作台。** 摆渡能从游戏里提取对白和菜单，导出双语表让人翻译、校对，再把译文放回游戏。它最早为 Minecraft 模组而写；从 2.0 起，视觉小说、独立游戏和老平台游戏有了自己的工作区，不需要安装 Minecraft。

**[进入「渡口通信」发布页](https://redrea1ity.github.io/ProjectFerry/)** · [下载 Windows 版](https://redrea1ity.github.io/ProjectFerry/#downloads) · [GitHub 历史版本](https://github.com/RedRea1ity/ProjectFerry/releases) · [反馈问题](https://github.com/RedRea1ity/ProjectFerry/issues)

> 人无语言则茫然无依，故为摆渡。这里的机器译文是临时替补；有译稿就用译稿，愿意自己翻也不需要 API Key。

## 从哪里开始

下载并运行 `ProjectFerry.exe`，在左侧选择你手里的项目：

| 你想做什么 | 选哪个入口 | 最后拿到什么 |
| --- | --- | --- |
| 翻一款没有中文的游戏，或把已经校好的台词放回去 | **游戏汉化**：选游戏目录、镜像或程序，先点「检测游戏」 | 双语表，以及该格式允许生成的翻译副本、补丁或写回结果。[通用游戏手册](docs/GAME-LOCALIZATION.md) |
| 给 Minecraft 整合包里的模组补中文 | **模组汉化**：选实例，扫描模组自带译文和已有资源包 | 摆渡自己的中文资源包；部分不走语言文件的文字另用桥接。[Minecraft 手册](docs/MINECRAFT.md) |

**选版本：** 要用双轨工作台，请从发布页选择 **2.0 系列**。其中 [v2.0.0-alpha.5](https://github.com/RedRea1ity/ProjectFerry/releases/tag/v2.0.0-alpha.5) 是早期实验版，新增的塞班和黑莓写回尚缺实机验证，稳定性低于 alpha.4。只做 MC 资源包也可选较早的正式版。发行附件里的桥接 JAR 不是桌面程序。

## 通用游戏：先试译一条，再决定是否翻完整作

比如你想翻一部吉里吉里视觉小说：先复制游戏目录，在副本上检测。若报告显示可读、可写，点「提取文本(自翻)」，在 `ferry_tl.tsv` 的 `dst` 列改一行主菜单。保存后点「导入外部译文…」并重打包，进游戏看这行字有没有显示、缺字或挤出按钮。**这趟流程不用调用翻译服务。** 确认效果后，可以继续手译，也可以用翻译引擎出初稿，再逐条校对。

适配范围包括吉里吉里、Ren’Py、RPG Maker、Godot、Unity 等游戏引擎，也包括 PC-98 磁盘、PSP/PS Vita/NDS 镜像和部分手机游戏包。这里说的是 **Windows 工作台能处理的游戏文件**，不是说工作台能装到这些设备上。认出格式、读到文字、写回成功和真机能跑是四件事；只读到文本时，先停在导出这一步。[按引擎或平台查范围](docs/GAME-LOCALIZATION.md#哪些格式值得先试)

## Minecraft：只补缺口，不动原模组

先扫描实例。默认情况下，只要模组已有可识别的人工中文，摆渡就会跳过整个模组，即使它只翻了一半。没有人工翻译的模组可以生成独立资源包；汉化组后来补上时，再扫描会撤回摆渡生成的对应条目。**这套自动让位规则属于 MC 资源包**，不代表通用游戏会自动替换已有补丁。

资源包能处理语言文件，不能改变所有屏幕文字。Forge 1.20.1 的物品提示可走桥接；聊天栏仍待实机验证。FTB 任务书、只补缺词和桥接安装分别见 [Minecraft 手册](docs/MINECRAFT.md)。

## 动手之前

- **先留原档。** 不同格式会生成副本、补丁或原地写回；保留干净游戏、存档和双语表。撤销记录不能代替备份。
- **把机器翻译当初稿。** 检测、提取和手工回录不消耗翻译额度。选择在线引擎时，待翻文本会发给所选服务；私有或未公开的剧本可手译，或配置本地模型。不要在 Issue 里贴密钥或上传整部游戏。
- **报告比扩展名重要。** 图片里的字、运行时才拼出来的句子和缺少中文字形的字体，都不是翻完语言表就能解决的。反馈时附上工具版本、游戏格式、脱敏后的检测报告，以及一条能复现问题的文本。

两条路线共用「翻译引擎」设置：可以选择 MyMemory、DeepL、自定义兼容接口或本地模型。先用几句对白检查术语和格式，再决定是否增加并发或启用模型池。请勿分享自己的 `ferry_config.json`；仓库里提供的是不含个人密钥的示例。

## 从源码运行

安装带 Tk 的 Python 3.11 或更高版本，在仓库根目录运行 `py -3 ProjectFerry.pyw`。查看 [工作台截图](site/public/workbench.png)、[翻译引擎配置示例](ferry_config.example.json)、[代码结构](docs/ARCHITECTURE.md) 或 [持续集成的测试清单](.github/workflows/tests.yml)。

## 更新日志

[在发布页按时间翻阅版本链](https://redrea1ity.github.io/ProjectFerry/#archive)。下方保留各版的开发记录；历史条目说明的是当时的功能和测试状态。

<details>
<summary>展开版本记录</summary>

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

**红现实**，项目发起与维护。项目使用 [MIT 许可证](LICENSE)。感谢提供语言资源与工具的 Mojang、PandaDevOfficial / Minecraft-All-Lang、CFPA 社区及 [LiteralBridge](https://github.com/Losketch/LiteralBridge)，也感谢愿意把一句生硬的机翻改对的人。社区汉化保留各自的许可和署名。

[到揭示板留言](https://redrea1ity.github.io/ProjectFerry/#forum) · [GitHub Issues](https://github.com/RedRea1ity/ProjectFerry/issues) · [邮件联系红现实](mailto:zetadarkdragon@gmail.com)
