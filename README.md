# 摆渡计划 / ProjectFerry

> 人无语言则茫然无依，故为摆渡。
> Where words fail, we ferry.

**开发中 · v2.0.0-alpha.4（早期测试版）**

摆渡计划已经从最初单纯的「Minecraft 模组临时汉化器」，正式演进为**双轨制跨平台全语种游戏本地化工作台**（`ferrycore` 模组主线 + `ferrygame` 通用游戏线）。首次打开时从左侧导航栏或「项目工作台」选择要处理的轨道；两条线共用底层 AI 翻译引擎与密钥配置，但探测、提示词、写回与无损还原各自独立、互不污染：

- **通用游戏汉化（28 类全球主流 / 独立 / 复古 / 移动 / 掌机 / 功能机 / Flash 引擎适配）**：
  - **欧美与全球主流/独立引擎**：**Unreal Engine 4/5 (`.locres` v0~v3)**、**Unity (`StreamingAssets` + Mono `#US` + IL2CPP `global-metadata.dat` → XUnity)**、**Godot 3/4 (`.pck` + `.tscn`)**、**GameMaker (`data.win` IFF `STRG`)**、**LÖVE / Love2D (`.love` / 融合 EXE / `.lua`)**、**全球叙事引擎集群（Inkle's Ink `.ink.json`、Yarn Spinner `.yarn`、Twine `.html`、Godot Dialogic `.dtl`、Adventure Game Studio `.trs`）**、**Adobe / Macromedia Flash (`.swf` `FWS`/`CWS` + Projector `.exe` AS2/AS3 常量池变长重建)**、**通用 Gettext `.po / .json / .csv`**；
  - **日系同人与视觉小说引擎**：**Kirikiri 2/Z 吉里吉里 (`.xp3` + UTF-16LE `patch.xp3`)**、**Ren'Py (`.rpa / .rpy`)**、**RPG Maker 全家桶（2000/2003 `.ldb/.lmu`、XP/VX/Ace `.rgss3a/.rvdata2`、MV/MZ `.json`）**、**TyranoScript / Electron `app.asar`**、**ONScripter (`nscript.dat` `0x84` XOR / `0.txt`)**、**WOLF RPG Editor (`Data/*.dat/.mps`)**、**Artemis Engine (`.pfs` + `.ast`)**、**HSP (`DPMX` + 替身 DLL)**；
  - **复古电脑（PC-98 / Win3.1 / Win95 / Win98）**：**NEC PC-98 (`.hdi/.fdi/.nhd` FAT12/16 + 内置 `anex86.bmp` 字库底板)**、**Macromedia Director（`RIFX`/`XFIR` `.dir/.dxr/.cst` 及 Projector EXE —— 覆盖 Win3.1《东脑》《中天》等同代多媒体邪典）**、**Windows 3.1 16 位 `NE` 程序**、**Win95/98 AliceSoft System 1/2/3/3.5 (`*.ald` 扇区归档)**；
  - **移动端、掌机与功能机老古董**：**Java ME / J2ME ("jvav" `.jar` `.class` `0xCAFEBABE` 常量池变长重建 + `.jad` 字节大小自动同步)**、**塞班 Symbian OS（S60 / N-Gage / UIQ `.sis / .sisx / .rsc / .r01 / E32Image .app`）**、**Android `.apk`（`strings.xml` + `resources.arsc` + `classes.dex` 重算校验 + `global-metadata.dat`）**、**iOS `.ipa`（明文与 `bplist00` 二进制 `.strings` + `0755` 权限保持）**、**PS Vita (`.vpk` + `rePatch/<TITLE_ID>/`)**、**PSP (`.iso` & `.cso` CISO 压缩镜像 LBA 扇区写回)**、**Nintendo DS (`.nds` NitroROM FNT/FAT 写回)**；
  - **测试版专属功能 —— 0 Token「文本提取 → 自翻译 → 回录重打包」**：一键将任意受支持游戏/镜像的文本提取为 Excel 直读的 UTF-8-BOM **`ferry_tl.tsv`** 与 **`ferry_tl.json`**，在 Excel / 记事本 / ParaTranz / 网页大模型自翻后，一键回录并自动重打包回原格式镜像或安装包！
- **Minecraft 模组汉化**：选择 Minecraft 实例，扫描整合包的中文缺口。默认避让人工译文；社区汉化、质量检查、术语表和桥接工具在「资源与桥接」。
- **翻译引擎**：管理接口、密钥和预设，测试连接，配置并发与模型池。

说白了就是：别人熬夜翻的字，我一个都不敢碰；没人翻的那部分，我拿 AI 顶两天班，等正主回来了我自己滚。这玩意儿是我一个不懂编程的人逼着 AI 一砖一瓦垒出来的，所以你看到它长得磕碜——那不是风格，那就是本来面目。

模组汉化当前状态：

- 资源包汉化可用（扫描 / 只补缺口 / 与人工汉化共存）。
- 自研桥的物品 tooltip 路径（Forge 官方事件）**已实机验证**（实测「已替换 4600+ 次」）。
- 聊天栏路径刚从 Mixin 改为 Forge 官方事件 `ClientChatReceivedEvent`，**尚未实机验证** —— 本版标 `beta` 即因此。
- 「**进游戏之前全在摆渡里做完**」已成立：桌面扫描（硬编码 / tooltip / 聊天栏文本）→ 翻译 → 自动导出 `ferrybridge-mappings.json` → 进游戏即中文。游戏内采集已降级为**开发者补漏**。
- 现在缺的是**覆盖率**（映射表还不够全），不是机制。
- 汉化桥模组生成器 / 通用桥模板生成器尚未开工。

## 作者真正的人话省流

- 一个妄图学forbric统一三界的（对，自然是想要支持NeoForge，Fabric，Forge等多种加载器。），**使用多种人工智能大模型编写并且人工测试以及人工路线以及方向策划的**，我的世界模组临时ai汉化软件。总之就是一个临时ai汉化，在没有官方/人工汉化的时候临时替补。
- 自带检测到人工汉化自动退位（自我削除谢谢喵）。孩子们这就是我们全世界最尊重汉化组的临时ai汉化工具的嘴脸。（目前只有资源包汉化）
- 内置cfpa人工汉化联网查询（当然你也可以自己添加可信查询源），查询成功则安装进mc实例，ai不做翻译。如汉化部分缺失会进行填补。不会对汉化组原文进行任何改动。（查询失败就只能ai汉化喵）
- 同时如果需要可以通过喂ai汉化语料来进行深入精细汉化（该功能可关闭喵不要打我喵）
- **注意：为了保证术语翻译精准会喂翻译ai部分术语语料进行术语表生成**
- 同时为了省钱和加速支持自定义api和多并发和多模型共同翻译，内置两个免费源，一个mymemory一个deepl免费版。也就是说理论上你可以同时抽打114514个大模型给你翻译，对。
- 理论上可以无限向上兼容，因为会联网更新版本表，同时最低支持1.6.1资源包机制。
- （对不起并未做到一统三界，目前可以通过LiteralBridge来达成forge1.20.1的硬编码汉化。但依旧无法完整汉化ftb任务书，但目前技术路线是内置桥模组生成模板，只需要在用户电脑上生成对应加载器的桥接模组，理论上也算个一统三界（但我不是秦始皇，我真没打算统一加载器），这样就不用我过多维护（我要上学。并且整个项目只有我和我的大模型编码agent清蒸大闸蟹干饭臭肥鱼这样那样的维护。）也不用让我死的这么早，我还没活够。所以，迟早会更新的。相信红现实喵，相信哈基红谢谢喵。）
 - 2026.10.12：已通过LiteralBridge达成forge1.20.1的硬编码汉化喵，下一步就是全线替代LiteralBridge改用摆渡桥。不过下次更新不知道多久喵。
 - 2026.10.20：FTB 任务书现在有正经主路了——装 **FTB Quest Localizer**，进游戏跑 `/ftblang export`，摆渡只读它导出的 `kubejs/assets/ftbquestlocalizer/lang/en_us.json`，把带颜色/悬停的富文本数组也翻掉，`zh_cn.json` 只写进摆渡自己的资源包。**摆渡不解析、不写回任何 SNBT**，`config/ftbquests/**` 和 `kubejs/` 里的原文件一律不动。没装 Localizer 时摆渡只提示，不代装。
 - 2026.11.21（v2.0.0-alpha）：被拷打之后终于不再只是 MC 模组汉化器了（悲）。独立拆出了 `ferrygame/` 通用游戏线（彻底隔离 MC 的苦力怕提示词），一口气塞进了 **17 个引擎与平台适配器**——小到上古 **PC-98 软盘/硬盘镜像（`.hdi/.fdi/.nhd`）+ 内置 `anex86.bmp` 汉化字库底板**、无引擎裸二进制（`.exe/.dat/.mes` 定长原位覆写），主流到 **吉里吉里 `.xp3`、Ren'Py、RPG Maker 全家桶（2000/2003/XP/VX/Ace/MV/MZ）、Tyrano / Electron `app.asar`、Godot `.pck`、Unity、GameMaker、HSP**，甚至极端到 **Android `.apk`（含 `classes.dex` 重算校验）、iOS `.ipa`（含二进制 `bplist00`）、PS Vita（`.vpk` + `rePatch` 补丁层）、PSP（`.iso` 光盘 LBA 扇区原位写回）**，全程依然是 **Python 标准库零第三方依赖**。

## 它能做什么

- **资源包汉化**：扫描模组语言文件（老的 `.lang` 也认），找出没中文的 key，用翻译引擎生成低优先级资源包。默认引擎是免费的 MyMemory。
- **人工汉化优先**：只要某模组有人工汉化，哪怕只翻了一半，默认就不给它生成 AI 翻译。详见 [人工汉化优先](#人工汉化优先)。
- **硬编码文本**（写死在代码里、不走语言文件的文字）：见 [桥接](#桥接硬编码文本的显示层替换)。
- **联网查询现成汉化**、**检查更新**、**术语表**等，见 [联网功能](#联网功能) 与后续章节。

### 做不到的

- **烤在图片和纹理里的字**：无法处理。除非把美术也 AI 了，那我还得先学会画画。
- **运行时动态拼接的文本**：桌面扫描抓不到，只能靠游戏内采集补漏（见桥接章节）。这条我认，是真没辙，不是懒。
- 语言 key 全翻完 ≠ 游戏里全中文。`.class` 里的代码常量不在语言文件里，需走桥接。我也没有读心术。

**再列一遍，免得你抱期待**：翻不了图里的字、翻不了游戏现拼的句子、翻完 key 也不保证游戏全中文。这工具能干的活儿就那几样，别指望它是救世主——它就是个替班的，救世主这活儿本来就该由人来干。

说句实在的：这工具最好的结局，是被你删掉。等官方汉化补上、等汉化组把坑填了，它就没人理了——那说明它赢了。一个永远被需要的临时替补，才叫失败。

## 怎么打开

- **免安装**：去 [Releases](https://github.com/RedRea1ity/ProjectFerry/releases) 下载 `ProjectFerry.exe`。该 exe 由 CI 打 tag 时自动构建（仓库**不再提交**这个 13 MB 二进制，避免与源码不同步）。
- **有 Python**：双击 `ProjectFerry.bat`，或双击 `ProjectFerry.pyw`（需系统已关联 Python）。

打开后会自动查找本机 MC 实例填入下拉框。选一个，下方列表即缺中文的模组。找不到实例可点「浏览」手动指定。

## 桥接：硬编码文本的显示层替换

有些模组把物品备注、任务文本直接写死在代码里，不走语言文件，资源包盖不住。这类文字只能在**显示层**替换。

摆渡的做法：把「英文原文 → 中文」的规则交给显示层替换模组 [LiteralBridge](https://github.com/Losketch/LiteralBridge)，并供数据给它。

1. 扫描 + 翻译完，点底部「**导出桥接映射**」，会写两样东西：
   - `<实例>/config/literalbridge/ferry.json`：英文原文 → 翻译键的规则文件（LiteralBridge 标准外部格式，改完自动热重载，**不用重启游戏**）。
   - 资源包内补 `assets/literalbridge/lang/zh_cn.json`：翻译键 → 中文。
2. 确认实例 `mods` 里有 LiteralBridge。**没有的话导出时会自动从 GitHub 下载**匹配你加载器与游戏版本的那个 jar 丢进 `mods`（模组需重启游戏加载）；只有网络不通、或没有匹配「加载器 + 版本」的本体时，才回落成打开项目页。已装实例勾选「自动升级 LiteralBridge」时还会顺带查新版，有就换掉（旧版改名成 `.jar.bak-…` 留在原地备份）。
3. 进游戏，硬编码文本在显示层变中文。删掉模组和规则文件即还原。

**硬编码文本的翻译**：设置摘要行勾上「**翻译硬编码文本**」后，检测器扫出的疑似硬编码英文会随翻译批一并交给 AI，译文存资源包旁的 `.hardcoded.json`，导出时一并并入规则。检测是启发式的，可能混入奇怪的句子；但游戏里按精确匹配，没那句就永不显示，顶多浪费一点 token。（命令串如 `/execute`、`@e[…]` 会被自动剔除。）

> **LiteralBridge 版本限制**：官方 Release 只有 **Forge 版**，MC 版本范围 **`[1.20.1, 1.21)`**（见其 `mods.toml`）。NeoForge / 1.21+ 上装不上，硬编码汉化需靠自研桥兜底。摆渡不会向这类实例强塞不匹配的 Forge jar，会如实提示「没有匹配的本体」。

### 自研桥当前能力

| 路径 | 状态 |
|---|---|
| 物品 tooltip / Shift 技能说明 | ✅ 能汉化（Forge 官方事件 `ItemTooltipEvent`，已实机跑通） |
| 聊天栏文本 | ⚠ 已从 Mixin 改为 Forge 官方事件 `ClientChatReceivedEvent`，**待实机验证** |

自研桥模组在 `bridge/` 目录，作为 LiteralBridge 未覆盖的版本与加载器的兜底（**已解冻并在做**）。规模化路线见 [docs/PLAN_SCALE_UP.md](docs/PLAN_SCALE_UP.md)。

> 2026-10-12 起整个桥**零 Mixin**：聊天栏与 tooltip 都走 Forge 官方事件，不再需要 mixin 配置 / refmap / 混淆映射。唯一覆盖缺口：别的模组在客户端进程里**直接向聊天栏塞字**（不经网络包）的消息，事件收不到 —— 不为它回退 Mixin。

### 大批量翻译（桌面一条龙，不用进游戏）

桥自身不翻译，映射表需有人填。**推荐全程在摆渡里完成**：

**主流程 · 桌面静态扫描**（不用进游戏）：

1. 勾上设置摘要行的「**翻译硬编码文本**」，选中模组，点「开始翻译」。摆渡会扒 jar 的 `.class` 常量池，把写死在代码里的英文（物品 tooltip、Shift 详细说明、聊天台词……）抓出来交给 AI，译文存进资源包旁的 `.hardcoded.json`。
2. 翻译收尾时会**自动**把当前所有映射（语言文件译文 + 硬编码译文）写进游戏目录的 `ferrybridge-mappings.json`（手动也行，点「**导出桥接映射**」）。
3. 进游戏即中文。之后改完重新翻译 / 导出即可，**游戏不用重启**。

**开发者补漏 · 游戏内采集**（兜底静态扫描抓不到的）：

1. 进游戏正常玩一圈（悬停物品、看聊天栏）。桥会把**出现过、但映射表里没有译文**的英文，按「聊天栏 / 物品 tooltip」分桶记到游戏目录的 `ferrybridge-collected.txt`。按 **F9** 可实时看「已采集 N 条」。
2. 回摆渡，点「**导入游戏采集（开发者补漏）**」，读清单、批量翻译，写进同一份映射表。
3. 游戏**不用重启**，按 F9 可见「已替换」开始涨。**按 F9 同时会在游戏目录写一份 `ferrybridge-diagnose.txt`**。

两个入口写的是**同一个文件**，摆渡会**合并**（不再整表覆盖）。

命令行等价物：`python mc_ai_translator.py bridge-collect --instance <实例目录>`。

采集器**只记录、不翻译、不自动替换** —— 玩家自己敲进聊天框的话会被记下，但不在表里就一直是原文（由「整条精确匹配」保证）。

### 出问题了怎么反馈

- **摆渡侧**：底部「**一键诊断**」按钮 → 生成诊断包（版本 / 运行环境 / 配置概览 / 游戏侧状态 / 脱敏日志）并告诉你位置。
- **游戏侧**：按 F9 生成 `ferrybridge-diagnose.txt`。

两份都不用自己翻日志、列模组清单，**整份发出来就行**。

**自检模式**（保证游戏能启动、不做任何替换）：

- 临时：启动参数加 `-Dferrybridge.safe=true`
- 长期：在游戏目录建 `ferrybridge.json`，内容 `{"enabled": false}`

无论开关如何，桥都会在游戏目录生成 `ferrybridge-diagnostic.txt`，用人话写清「映射多少条 / 几条替换路径活着 / 坏了该修什么」。

## 默认会过滤掉的模组

纯前置、库、性能优化、底层修复类的模组默认不翻，例如 Architectury、Cloth Config、GeckoLib、KuroLib、内存泄漏修复、星光、FerriteCore、Entity Culling、ModernFix 等。翻这些玩家看不见，纯烧钱。

地图、JEI、Jade、背包整理、枪械、任务、怪物等面向玩家的**不过滤**。被过滤的模组仍在游戏里，只是不进「待汉化」列表。若认为某个被过滤的该翻，GUI 点「用户白名单」加 modid，保存后重扫；白名单存于 `ferry_whitelist.json`。

## 人工汉化优先

这条是底线，也是全部卖点。

扫描时把中文来源分类：模组自带与社区包里的 `zh_cn` 算**人工汉化**；本工具上次生成的 AI 包靠包内 `AI_TRANSLATION_NOTICE.txt` 认出，不算。

**规则只有一条**：只要某模组有人工汉化，哪怕只翻了一半，默认就不给它生成 AI 翻译，保持原样；没人翻的才进「待 AI 翻译」。

**它会自己撤场**：若汉化组在你用完之后补上了译文，下次扫描摆渡会把之前自己生成的那几条从资源包删掉；删到整包为空则一并删掉包文件。不是「不覆盖」，是「擦掉自己的」。每次撤场会在状态栏播报，并在包内 `AI_TRANSLATION_NOTICE.txt` 留带时间戳的记录（保留最近 20 条）：

```
摆渡退场记录（这些都是摆渡主动撤回的，不是翻译失败）：
  · [2026-10-08 16:52] 撤回自己此前生成的 4 条译文（tacz）
  · [2026-10-08 17:03] 撤回自己此前生成的 2 条译文（jei）
```

### 可选玩法

- `--fill-community`：只补「有人工汉化但不完整」的模组缺的那几个 key，不改已有译文。
- GUI 勾「参考人工汉化语料补全缺失 key」或 `--refine-community`：把同模组现有 `en_us` 和人工 `zh_cn` 配对当术语与文风参考，只让 AI 翻人工和现有 AI 包都没覆盖的 key。人工译文永不会被当待翻项、也不会写进 AI 包。需 OpenAI 兼容 / Anthropic / Gemini / 本地模型，并发池不能混 MyMemory 和 DeepL。**默认关闭**。
- **状态列措辞**：英文 key 全被覆盖、硬编码也全翻完时 —— AI 从零翻完的显示「✓ 已 AI 汉化完成」，人工打底、AI 只补缺口的显示「✓ 已 AI 补完」，全是人工汉化、摆渡没插手的显示「◈ 人工·完整（已让位）」。悬停可看人工与 AI 各占多少（同一 key 优先算人工，不重复计）。
- `en_us` 里没有文字的空 key 不算待翻译，也不计进比例。
- 若 `.class` 里有疑似写死的界面文字，状态列标「⚠ 含硬编码·待翻N」，N 是**还没翻完的条数**。翻完后（译文记在包旁 `<资源包名>.hardcoded.json`）不再挂 ⚠，正常回到完成态。

## 右键菜单

列表内右键选中项：

- **删除本工具 AI 汉化**：把该模组的 AI 译文从摆渡包卸掉，回退到模组自带的人工 / 官方汉化（没有则显示英文）。卸掉的译文存到 `<资源包名>.uninstalled.json`，列表标「AI 汉化已卸载」，不会被「继续翻译」偷偷加回。
- **重载已卸载的 AI 汉化**：从记录恢复，不调模型。人工译文与「强制还原英文」的 key 不会被盖。
- **强制还原英文**：生成 / 更新覆盖包，把该模组 `en_us` 所有键用英文写进 `zh_cn`，连人工与官方汉化一起盖掉。包内用 `FERRY_REVERTED.json` 记录，重扫显示「已还原英文（不翻译）」，「翻译全部待AI」也不会再翻回去。
- **重翻整个模组 / 联网安装人工汉化包 / 设置不翻译 key**。
- **加入不翻译名单**：该模组完全跳过 AI 翻译，状态列显示「∅ 已排除（不翻译）」，「翻译全部待AI」与双击都自动跳过。右键「移出不翻译名单」恢复。

删完自动重扫。撤销「强制还原英文」：删掉对应资源包；恢复卸载的 AI 译文：右键「重载」。

## 联网功能

- **查询在线汉化**：表格选中模组后点底部按钮，查 CFPA 社区整合包（内置来源）与自定义查询源，看哪些模组已有现成汉化，结果可直接开链接。查询源在「设置 → 编辑查询源…」添加，每行一个网址，用 `{modid}` 代替模组 ID。
- **检查更新**：启动时每周自动查一次 GitHub 最新版（弹窗可关），「关于」页也能手动查。发现新版可自动更新（下载替换 exe，重启生效）或跳转发布页手动下载。
- **社区汉化基线**：拉 CFPA 整合包（约 6.8 MB），14 天缓存一次，本地抽取。
- **翻译硬编码文本**：设置摘要行勾选后，疑似硬编码英文随翻译批交给 AI，译文存包旁 `.hardcoded.json`，走桥接在游戏里替换。

## 版本兼容

1.6.1 之前没有资源包机制，不支持（1.6.1 是 2013 年的版本）。所有汉化靠资源包覆盖，不碰模组文件。

**1.6.1–1.12.2 旧版**：`pack_format` 1–2（1.6.1–1.10.2）写 `assets/<modid>/lang/zh_CN.lang`；`pack_format` 3（1.11–1.12.2）起资源包内文件名强制全小写、改写 `zh_cn.lang`；1.13+ 写 `zh_cn.json`。旧实例若已有本工具生成的 JSON 包，重扫与翻译时会自动迁成 `.lang`，原 ZIP 先备份成同目录的 `.pre-lang-backup`。

**新版向上兼容**：优先读实例客户端 jar 的 `version.json`，直接取 `pack_version.resource_major/resource_minor`（如 1.21.10→69.0，26.3→97.1），故以后出新版基本不用改代码。读不到才退回内置版本表（1.21.8→64，1.21.9→69.0，1.21.11→75.0，26.1→84.0，26.2→88.0，26.3→97.1，26.4→99.0；为当前快照值，正式版以 `version.json` 为准）。游戏现用年份版本号（`26.2`、`26.3`），若填成 `1.26.3` 会自动当 `26.3` 处理。1.21.9（25w31a）起 `pack.mcmeta` 用 `min_format` + `max_format`（整数或 `[major, minor]`），更早仍用 `pack_format`，读取时两种都认。

**版本表联网更新**：本地版本表未跟上游戏更新时，会去仓库根目录 `pack_formats.json` 拉最新表（raw.githubusercontent 主源 + jsDelivr 备源），结果缓存 24 小时到 `ferry_cache/`；拉不到则继续用内置表，再不行才弹手动选择。故游戏更新后只需在 GitHub 给该 JSON 补一行，旧安装无需重下程序。不想联网可在 `ferry_config.json` 把 `online_pack_format` 设为 `false`。

未知版本时 GUI 让你手动选或填（支持 `97.1` 这类小数），命令行用 `--pack-format N`，或 `--yes` 硬按 15 走（包内标「版本未确认」）。HMCL / PCL 的私有配置尚未实机确认，`options.txt` 自动启用一直关闭。

> 资源包能被游戏加载 ≠ 模组文字真的变了。真实验证仍需进游戏对比启用/关闭前后的物品名。

## 翻译进度 / 断点续传

- 列表显示每个模组的「AI 已翻 / 缺中文 key / 状态」，中途中断重扫也能看到进度。
- 检测到上次没翻完的，顶部提示剩余条数，「翻译全部待AI」自动变成「继续翻译」。
- 失败的 key 记在 `ferry_progress.json`，下次扫描标红「上次失败 N 条」，翻成功后自动移除。

## 术语表

翻译时把 Mojang 自动词库当上下文术语提示（如 `Redstone Dust` → 红石粉），不再把 `Lead`、`Map` 这类多义短词脱离句子强行替换。你在 `ferry_config.json` 的 `glossary` 里填的词会严格固定，模组名与 Forge/NeoForge 等品牌词也会保护。

联网更新官方术语表（从 Mojang 官方语言文件提取实体、附魔、效果、物品名）：

```powershell
py mc_ai_translator.py glossary --update   # 联网下载并提取
py mc_ai_translator.py glossary            # 看概况
py mc_ai_translator.py glossary --show     # 列全部条目
```

结果存于 `user_glossary.json`（可手动改）；`ferry_config.json` 的 `glossary` 和 `keep_untranslated` 也可加自定义术语与不翻译词。

## 引擎

| 引擎 | 说明 |
| --- | --- |
| `mymemory` | MyMemory，免费，默认（匿名约 5000 字符/天，填 `mymemory_email` 可提到 5 万） |
| `deepl` | DeepL（默认免费版端点） |
| `openai` | OpenAI 兼容，可改 `openai_base_url` 接 DeepSeek、Kimi、Qwen 等 |
| `anthropic` | Anthropic（Claude）兼容 |
| `gemini` | Google Gemini 兼容 |
| `local` | 本地模型（Ollama / LM Studio，OpenAI 兼容，默认 `http://127.0.0.1:11434/v1`） |

引擎与 key 都存在 `ferry_config.json`。GUI 分三层选：先选「免费 / 自定义接口」，自定义再选「兼容接口」，还能「保存预设」把多套 API 存成名字。

云接口必须用 HTTPS 远程地址，填 `localhost` 或 `127.0.0.1` 会在开翻前直接拦下。本地跑走 `local` 引擎（免费，需自备显卡）。

## 并发与多模型加速

默认串行，`concurrency` 控制同时翻几个批次：

```powershell
py mc_ai_translator.py translate --instance "..." --concurrency 3
```

多模型池在 `ferry_config.json` 配 `model_pool`，按批次轮换给不同引擎，配合 `concurrency` 并行提速：

```json
{
  "model_pool": [
    { "engine": "mymemory", "weight": 3 },
    { "engine": "openai", "api_key": "sk-...", "model": "gpt-4o-mini", "weight": 1 },
    { "engine": "local", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5", "weight": 2 }
  ]
}
```

批次按「字符量 ÷ weight」贪心分配：贵的、量大的少分，免费便宜的给高 `weight` 多分，尽量让各模型花费接近。翻完报告每个模型的条数 / 批数 / 字数。GUI「设置」可图形化加模型，从预设一键拉进池。

> MyMemory 免费额度有限，并发建议 2~4；付费与本地引擎可调高。

## 命令行

```powershell
py mc_ai_translator.py detect
py mc_ai_translator.py scan --interactive
py mc_ai_translator.py scan --instance "D:\Minecraft\instances\测试整合包"
py mc_ai_translator.py translate --instance "D:\Minecraft\instances\测试整合包"
py mc_ai_translator.py translate --instance "..." --engine openai --only-modid tacz
py mc_ai_translator.py translate --instance "..." --only-modid tacz,jei
```

默认读 `<实例目录>\mods` 与 `<实例目录>\resourcepacks`，也可用 `--mods`、`--resourcepacks` 单独指定。

## 通用游戏汉化（PC 主流引擎 / 复古 PC-98 / 移动端 APK & IPA / 掌机 PSV & PSP）

从 `v2.0.0-alpha` 起，通用游戏汉化拥有独立包 `ferrygame/` 与工作台专属页面，**跟 MC 那套不共用目录、不混用提示词**（不再出现把日系 AVG 或 RPG 台词翻出「苦力怕 / 红石」的串味惨案），唯一共用的是底层翻译接口、密钥加密与并发池。

### 核心工作流

1. **先体检 / 字节侦查（0 Token）**：选择游戏目录或镜像文件（`.apk / .ipa / .vpk / .iso / .hdi / .xp3 / .pck / .exe`），先看认不认得引擎、能不能取到文本、**能不能写回去**，并自动在目录下生成双语对照表 `ferry_tl.json`。
2. **未知引擎字节侦查台（`recon`）**：遇到从未见过的猎奇单 EXE 或怪后缀数据包，点「字节侦查」直接计算各文件香农熵（0~8.0 判断明文 / 编译二进制 / 强加密）、PE 32/64 位架构、导入的 GDI 画字与菜单 API（`TextOutA` / `GetGlyphOutlineA` / `ModifyMenuA`）以及可用的替身 DLL（`version.dll` / `msimg32.dll`）。
3. **带滚动上下文与字节预算的 AI 翻译**：
   - 自动将控制符（`\C[2]`、`[r]`、`{w=0.5}`、`%1$s`、`^m`、`$c1` 等）掩码为 `__T0__` 铁律占位符，漏码整句拒绝；
   - 自动按剧本顺序注入前文滚动上下文，保证长对话人称与语气连贯；
   - 对有严格字节上限的定长二进制（如裸 EXE、`RPG_RT.ldb`、PC-98、PSP/PSV 脚本、`classes.dex`），超出 `max_bytes` 时自动追加「缩字重试」指令，绝不写爆指针偏移。
4. **手工精修 `ferry_tl.json` → 0 Token 重刷**：翻译完成后，可随时点击「编辑译文表」修改 `ferry_tl.json` 中的 `dst`，再点「写回译文表」瞬间重刷进游戏，不花一分钱 Token。
5. **一键无损撤销（`.ferry-undo.json`）**：所有原地修改（先备份 MD5 再落盘）和副本生成都记录在 `.ferry-undo.json`，随时点「撤销还原」100% 恢复原状。

### 已支持的 28 类全球引擎与平台适配器总表

| 优先级 | 适配器标识 | 覆盖平台 / 引擎 / 容器 | 核心机制与回写策略 |
| :---: | :--- | :--- | :--- |
| `11` | `apk` | **Android `.apk`** / 解包目录 | 抽取 `res/values*/strings.xml`、**`resources.arsc` (`ResStringPool` `0x0001`)**、`assets/` 内嵌游戏剧本、**`classes.dex` 字符串池**及 Unity IL2CPP `global-metadata.dat`；写回 `.cn.apk` 时自动重算 DEX SHA-1 与 Adler32、保持 `resources.arsc` 为 `ZIP_STORED` 无压缩并剥除旧 `META-INF` 签名块 |
| `12` | `ipa` | **iOS `.ipa`** / `Payload/*.app` | 原生支持 Apple `*.lproj/*.strings`（**明文 UTF-8/UTF-16 与编译后 `bplist00` 二进制格式**）及包内游戏剧本与 `global-metadata.dat`；重建 `.cn.ipa` 严格保留 Mach-O `0755` 可执行权限位 |
| `13` | `j2me` | **Java ME / J2ME ("jvav" `.jar / .jad`)** | 识别 `META-INF/MANIFEST.MF` 的 `MIDlet-` 清单，纯标准库解析并**任意长度重建 `.class` (`0xCAFEBABE`) 的 `CONSTANT_Utf8` 常量池**（不破坏字节码下标），产出 `.cn.jar` 并自动同步修正 `.cn.jad` 的 `MIDlet-Jar-Size` |
| `13` | `symbian` | **塞班 Symbian OS (`.sis / .sisx / .rsc / .r01 / .app`)** | 覆盖 S60v1/v2 (`0x10000419`)、S60v3/v5 (`0x10201A7A`)、UIQ 与 N-Gage，支持 `.rsc / .r01..r99` 资源表与 EPOC `E32Image` (`.app`) `_LIT16 / _LIT8` 台词抽取与定长回写 |
| `13` | `psv` | **PS Vita `.vpk`** / 解包目录 | 解析 `sce_sys/param.sfo`（`\x00PSF` 标题与 `TITLE_ID`），鉴别 `SCE\x00` 加密与明文 ELF，抽取明文词表与多编码二进制剧本；输出 `.cn.vpk` 或实机/Vita3K 标准 **`rePatch/<TITLE_ID>/`** 补丁目录 |
| `14` | `psp` | **PSP `.iso` & `.cso`** / `PSP_GAME` | 纯标准库解析 **ISO-9660 光盘文件系统**与 **`CISO` (`.cso`) 分扇区 deflate 压缩流**，读取 `/PSP_GAME/PARAM.SFO` 与 `USRDIR` 脚本，在 `.cn.iso / .cn.cso` 对应 LBA 扇区区间内原位写回，PPSSPP 与实机直读 |
| `14` | `nds` | **Nintendo DS `.nds`** (NitroROM) | 纯标准库解析 NDS 卡带头部的 **FNT（文件名表 `0x40`）与 FAT（文件分配表 `0x48`）**，抽取内部脚本并在 FAT 区间原位定长覆写产出 `.cn.nds` |
| `15` | `pc98` | **NEC PC-98** (`.hdi/.fdi/.nhd`) | 纯标准库解析 FAT12 / FAT16 磁盘镜像，内置 524KB **`anex86.bmp` 汉化字库底板**，把 CP932 缺字动态映射至 JIS 第二水准冷僻汉字格并自动修正 16 位汇编 `LODSW` 双字节偶对齐，输出镜像副本 + 定制 `anex86.bmp` |
| `16` | `flash` | **Adobe / Macromedia Flash (`.swf` & Projector `.exe`)** | 支持 `FWS`、`CWS` (zlib) 与尾部带 `0xFA123456` 封尾的独立 Flash Projector `.exe`；纯标准库重建 **AS1/AS2 `ActionConstantPool` (`0x88`)** 与 **AS3 AVM2 `DoABC` `string_info` 常量池**，支持任意长度 UTF-8 中文 |
| `16` | `retro_win` | **Win3.1 / 95 / 98 早期多媒体与老 AVG** | 覆盖 **Macromedia Director (`RIFX`/`XFIR` `.dir/.dxr/.cst` `STXT` 演员文本块 —— Win3.1《东脑》《中天》同款引擎)**、**Windows 3.1 16 位 `NE` 程序**、以及 **AliceSoft System 1/2/3/3.5 (`*.ald` 扇区归档)** |
| `17` | `onscripter` | **ONScripter / NScripter** | 支持 **`nscript.dat`（单字节 `0x84` XOR 自动加解密）** 与 `0.txt / 00.txt`，抽取 `caption`、`select/csel` 选项与正文台词 |
| `18` | `kirikiri` | **Kirikiri 2 / Z（吉里吉里）** | 纯标准库解包与打包 `.xp3` 归档（支持 zlib 压缩索引与加密滤镜鉴别），回写 `.ks` 统一采用 **UTF-16LE with BOM (`0xFF 0xFE`)** 生成 `patch.xp3`，无需转区或注入 DLL 直接原生渲染简体中文 |
| `19` | `wolfrpg` | **WOLF RPG Editor** | 自动鉴别 `.wolf` (DXArchive) 与解包 `Data/BasicData/*.dat`、`Data/MapData/*.mps`，抽取并定长写回数据库与事件台词 |
| `19` | `artemis` | **Artemis Engine** (`.pfs / .ast`) | 纯标准库解析 `pf8 / pf6` (`.pfs`) 归档索引，抽取 `.ast` Lua 表结构剧本台词与选项，写出高优先级覆盖脚本 |
| `20` | `tyrano` | **TyranoScript / TyranoBuilder** | 覆盖散装目录、`zip-in-exe` 尾部追加包、以及现代 **Electron `resources/app.asar`**（Chromium Pickle 头解析与 SHA-256 完整性重算） |
| `20` | `renpy` | **Ren'Py** (`.rpy` / `.rpa` v2 & v3) | 解包 `RPA-2.0 / RPA-3.0` 归档，生成非侵入式 `game/tl/chinese/` 官方多语言挂载脚本 |
| `21` | `rpgmaker_2k` | **RPG Maker 2000 / 2003** | 解析 LCF BER 变长块结构，只抽取 `RPG_RT.ldb` 玩家可见块（`11..15, 17, 18, 21, 25`）与 `Map*.lmu` 事件块（`81`），硬跳过图块矩阵与开关变量名，定长原地写回 |
| `22` | `rpgmaker_ace` | **RPG Maker XP / VX / VX Ace** | 纯标准库解包 `RGSSADv1 / v3`（`.rgssad / .rgss2a / .rgss3a`），原生反序列化与重打包 Ruby Marshal 4.8（`.rxdata / .rvdata / .rvdata2`） |
| `24` | `love2d` | **LÖVE (Love2D / Lua)** | 覆盖 `.love` 归档、融合 `zip-in-exe` 与散装 `main.lua`，智能抽取 `.lua` 表值与 UI/剧情字面量（跳过 `require`、资源路径与表键） |
| `25` | `rpgmaker` | **RPG Maker MV / MZ** | 按文件 + JSON Pointer + 事件指令码（`401/405/102/320/324/325`）三重精确定位 `data/*.json` 与 `www/data/*.json`，不碰 `plugins.js` 与代码行 |
| `26` | `unreal` | **Unreal Engine 4 / 5** (`.locres`) | 纯标准库解析并重建 UE4/UE5 `.locres`（支持 Legacy v0 与 Optimized v1/v2/v3 `LOCRES_MAGIC`），以负数长度 **`FString` UTF-16LE 宽字符**无损重建任意长度中文字符串池，保持前置 CRC32/CityHash64 键索引完整 |
| `27` | `godot` | **Godot Engine 3.x / 4.x** | 纯标准库解析并重建 `GDPC` v1 / v2 `.pck` 资源包（含 Godot 4 相对 `file_base` 与 MD5 重算），抽取并回写 `.tscn` 场景节点文本属性 |
| `28` | `unity` | **Unity Engine (Mono & IL2CPP)** | 同时支持 **`StreamingAssets` 词表**、Mono **`Assembly-CSharp.dll` (`BSJB` `#US` UTF-16LE 流)**、以及 **IL2CPP `global-metadata.dat` (`0xFAB11BAF` `stringLiteral` 任意长度重建)**，并同步生成 **XUnity.AutoTranslator** `_PreTranslated.txt` 预翻译包 |
| `29` | `narrative` | **欧美独立叙事引擎集群** | 一站式支持 **Inkle's Ink (`.ink.json` `^` 节点)**、**Yarn Spinner (`.yarn` 台词与 `->` 选项)**、**Twine (`.html` `<tw-passagedata>`)**、**Godot Dialogic (`.dtl`)** 与 **Adventure Game Studio (`.trs`)** |
| `30` | `gamemaker` | **GameMaker Studio** | 解析并重建 IFF `FORM` 容器（`data.win / game.unx / game.ios`）的 `STRG` 字符串池与外部 CSV/JSON 词表 |
| `35` | `i18n_table` | **通用 i18n 词表** | 支持跨引擎标准 GNU Gettext `.po`（含多行拼接）、`locales/*.json`、`i18n/*.csv` 抽取与原地写回 |
| `40` | `hsp` | **Hot Soup Processor (HSP)** | 解包 `DPMX` / `#HSP3` 字节码字符串池，配合按需编译的 GDI 替身 DLL 在渲染层实时拦截替换 |
| `90` | `rawbin` | **全球多编码裸二进制兜底** | 在 `.exe / .dat / .bin / .mes / .scn` 中同时扫描 **Shift-JIS（→GBK）**、**欧美 UTF-8/ASCII 自然英文句与菜单（→UTF-8）**、**Windows UTF-16LE 宽字符（→UTF-16LE）**，按原字节长度补齐定长覆写，不改任意指针偏移 |

### 通用游戏线命令行（`ferrygame` & `mc_ai_translator.py`）

```powershell
# 1. 独立通用游戏线 CLI（推荐）
py -3 -m ferrygame extract "D:\Games\某游戏或镜像.jar"   # 【自翻第一步】0 Token 提取文本，导出 ferry_tl.tsv (Excel 直开) + ferry_tl.json
py -3 -m ferrygame repack "D:\Games\某游戏或镜像.jar"    # 【自翻第二步】0 Token 回录改好的 ferry_tl.tsv / .csv / .json 并重打包游戏
py -3 -m ferrygame recon "D:\Games\未知猎奇游戏"         # 字节侦查（香农熵 / PE 导入表 / 台词密度）
py -3 -m ferrygame scan "D:\Games\某游戏"                # 0 Token 体检 + 生成译文表
py -3 -m ferrygame translate "D:\Games\某游戏"           # AI 翻译（更新 ferry_tl.json 与 ferry_tl.tsv）
py -3 -m ferrygame apply "D:\Games\某游戏"               # 0 Token 把译文表写回游戏
py -3 -m ferrygame run "D:\Games\某游戏"                 # 扫描 → AI 翻译 → 写回一条龙
py -3 -m ferrygame revert "D:\Games\某游戏"              # 按 .ferry-undo.json 一键无损还原

# 2. 原 CLI 入口同样支持
py -3 mc_ai_translator.py localize "D:\Games\某游戏" --dry-run
py -3 mc_ai_translator.py localize --list-adapters
py -3 mc_ai_translator.py fontcheck "D:\Games\某游戏"
```

## 配置与密钥

参数从 `ferry_config.json` 读（key、base URL、模型、引擎、pack_format、各种开关）。无此文件则用内置默认值。

```powershell
py mc_ai_translator.py config          # 看当前配置（Key 已打码）
py mc_ai_translator.py config --init   # 生成默认配置
```

**API Key 加密落盘**：Windows 上用系统自带 DPAPI，存成 `dpapi:<密文>`，用当前**用户账户**凭据 —— 换机器、换账户都解不开，文件被拿走或被网盘同步也无妨。读取时自动解密。内存里仍是明文（否则无法调接口），要防的是**文件泄漏**，不是同机内存抓取。非 Windows 无 DPAPI，退化为明文存（写日志提醒）。

**老配置自动迁移**：从早期版本（Key 明文）升级的，**下次启动**读到明文 Key 就会自动加密写回。迁移前先做「加密→解密→逐字比对」自检，确认密文解得回来才落盘。

## 保护规则

- AI 只翻人工汉化未覆盖的 `en_us` key；主动开语料补缺时，人工译文只作参考，绝不覆盖。
- 汉化组后续补上的译文，摆渡会自动撤回自己生成的那几条并播报（是让位，不是失败）。
- 不内置第三方汉化词典。
- 资源包不改原模组 JAR，删了就还原。
- 占位符（`%s` `%d` `%1$s` `{0}` `§a` `$(br)` 等）校验失败则保留英文。
- 翻译随时能停，产出是只含已翻部分但可直接用的不完整包；单批失败自动重试并跳过，已完成批次进缓存，下次续上。

## 界面速览

- 翻译设置默认折叠成一行摘要（引擎、模型、并发），点「▸ 翻译设置」展开；「并发 / 模型池…」与「翻译硬编码文本」开关常驻在旁。
- 翻译按钮与进度条贴在模组列表下方：看表 → 选模组 → 点翻译。
- 表格支持搜索（输入即筛）、筛选（全部 / 只看有缺口 / 只看失败 / 只看含人工汉化 / 只看疑似硬编码 / 只看已完成）、点列头排序（数值按数字排，带 ▲/▼）。
- 「完成度」显示百分比，悬停看构成；「状态」用符号加短文字（✓ 完成 ◐ 部分 ○ 待翻 ◈ 人工 ⚠ 疑似硬编码 ∅ 已排除 ⊘ 已还原 ⊗ 已卸载 ✗ 失败），悬停看完整标记。
- 只有一个实例时自动隐藏「实例」列。
- API Key 默认打码，点「显示」才明文；双击模组翻译前弹确认框告知条数。
- 列宽可拖，排序不重置宽度，窗口大小位置会记住。

## 更新日志

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
- 状态列措辞分清功过（见 [人工汉化优先](#人工汉化优先)）；硬编码只有还有没翻完的才挂 ⚠，翻完回归完成态。
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

## 开发 / 测试

- Python 3.11+，只用标准库（`tomllib`、`plistlib`、`zlib`、`struct`、tkinter 等），**无第三方依赖**。
- 跑全量测试（当前 **305 项全绿**）：

```powershell
py -3 -m unittest test_refine test_spec_p0 test_gui_p0 test_ftb_localizer test_localize test_fontcheck test_deps test_gamemaker test_hsp test_rpa_renpy test_rpgmaker_ace test_localized_resume test_ferrygame
```

- CI 在 `.github/workflows/tests.yml`，push / PR 时于 Python 3.11、3.12 上跑上述测试。
- 桥接模组在 `bridge/`，1.20.1 Forge，`.github/workflows/bridge.yml` 构建它。
- 本地构建桥：`cd bridge && ./build.sh`（离线优先，缓存缺件会自动联网补一次；新机器先跑 `bash tools/prefetch-forge-deps.sh` 生成 `.forge-prefetch/m2`）。产物在 `bridge/build/libs/ferrybridge-1.0.0.jar`。
- **代码结构**见 `docs/ARCHITECTURE.md`：`mc_ai_translator.py` 是 58 行薄壳，实现在 `ferrycore/` 的 19 个模块，依赖单向分层。动代码前先读该文档，尤其「`mock.patch.object(core, "X")` 跨模块会静默失效」一节。
- 设计文档在 `docs/`：`ARCHITECTURE.md`（代码结构与拆分路线）、`PLAN_SCALE_UP.md`（规模化路线）、`TEST_DESKTOP_ONESTEP.md`（桌面一步到位测试清单）、`TEST_COLLECT_ROUND1.md`（游戏内采集测试清单）。
- 自行打包 exe（平时用不着，打 tag 时 CI 自动构建）：

```powershell
py -m PyInstaller --onefile --windowed --name ProjectFerry --icon ferry_icon.ico --add-data "ferry_icon.png;." ProjectFerry.pyw
```

## 致谢

- Minecraft 官方语言文件（Mojang）——术语表来源
- PandaDevOfficial / Minecraft-All-Lang——语言文件镜像
- CFPAOrg / Minecraft-Mod-Language-Package——社区汉化基线（遵守其许可证与署名）
- Losketch / [LiteralBridge](https://github.com/Losketch/LiteralBridge)——硬编码文本的通用显示层桥接。Apache-2.0。
- Minecraft Wiki · Pack_format——资源包格式与版本号参考
- Python 标准库——本项目零第三方依赖。以及所有被我复制粘贴过代码的 Stack Overflow 楼层，和那些我压根没看懂的答案。

## 许可

MIT License，详见 [LICENSE](LICENSE)。

## 如何联系这个可怜的作者

联系这坨ai大便的作者请走zetadarkdragon@gmail.com或者可以在issue里面直球拉大便辱骂提意见什么的都可以。作者网瘾大理论上能及时看见。后续人多了会开设qq群。。。。求大佬轻点喷，我是真的，啥也不会。。。

再补一句实诚话：你看到的每一行代码，都是我不懂装懂、逼着 AI 一点点凑出来的，能跑起来已经算祖坟冒烟。这玩意儿哪天炸了别怀疑，肯定是我的锅，不是 AI 的——AI 只是个干活的，我才是那个瞎指挥的。
