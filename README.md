# 摆渡计划 / ProjectFerry

> 人无语言则茫然无依，故有摆渡。
> Where words fail, we ferry.

Minecraft 临时 AI 汉化工具。作为没有人工/官方汉化时的临时渡船：扫描 Minecraft 实例中的模组语言文件（含旧版 `.lang`），找出没有中文的 key，再通过内置翻译引擎（默认 MyMemory，免费）生成低优先级资源包。检测到某模组已有人工/官方汉化时会自动让位——**渡你过河，到岸即离**。

灵感来自《边狱巴士》零协汉化组的一句话：「人无语言则茫然无依」。

## 双击启动

双击 `ProjectFerry.bat`。它会启动一个窗口，不会再出现“黑框闪一下就没了”。窗口启动后会自动探测本机常见 Minecraft 实例，并放进固定宽度的下拉框；选择实例后，在可滚动的模组列表中显示缺少中文 key 的模组。扫描失败会在窗口和错误弹窗里显示原因，也可以用“浏览”临时加入一个实例。

如果 Windows 已经把 `.pyw` 关联到 Python，也可以直接双击 `ProjectFerry.pyw`。

## 自动检测实例

```powershell
py mc_ai_translator.py detect
py mc_ai_translator.py scan --interactive
```

如果只有一个常见实例，会自动使用它；如果有多个，`--interactive` 会让你选。

## 手动指定实例

```powershell
py mc_ai_translator.py scan --instance "D:\Minecraft\instances\测试整合包"
```

工具默认读取：

```text
<实例目录>\mods
<实例目录>\resourcepacks
```

也可以单独指定 `--mods` 或 `--resourcepacks`。

## 生成资源包

默认用 **MyMemory** 引擎，免费、无需 API key（匿名约 5000 字符/天，可在配置里填 `mymemory_email` 提高到 5 万）：

```powershell
py mc_ai_translator.py translate --instance "D:\Minecraft\instances\测试整合包"
```

也支持其余引擎，需要各自 API key（在 `ferry_config.json` 或命令行配置）：

| 引擎 | 说明 |
| --- | --- |
| `mymemory` | MyMemory，免费，默认 |
| `deepl` | DeepL（默认免费版端点） |
| `openai` | OpenAI 兼容接口，可改 `openai_base_url` 接 DeepSeek、Kimi、Qwen 等 |
| `anthropic` | Anthropic（Claude）兼容 |
| `gemini` | Google Gemini 兼容 |
| `local` | 本地模型（Ollama / LM Studio，OpenAI 兼容，默认 `http://127.0.0.1:11434/v1`） |

例如用 OpenAI 兼容接口 + 分模组测试：

```powershell
py mc_ai_translator.py translate --instance "D:\Minecraft\instances\测试整合包" --engine openai --only-modid tacz
```

## 分模组汉化

加 `--only-modid <modid>`（可重复或逗号分隔）只汉化指定模组，方便单独测试：

```powershell
py mc_ai_translator.py translate --instance "..." --only-modid tacz,jei
```

GUI 里可以按住 Ctrl 多选模组，点“翻译选中模组”；或点“翻译全部待AI”。

## 默认过滤的前置/库模组

采用“明确黑名单过滤”策略：只默认隐藏纯前置、库、性能优化和底层修复模组，例如 Architectury、Cloth Config、GeckoLib、KuroLib、内存泄漏修复、星光、FerriteCore、Entity Culling、ModernFix 等。

地图、JEI、Jade、背包整理、枪械、任务、怪物和其他面向玩家的模组不默认过滤，即使它们属于辅助类，也允许正常参与翻译。所有被过滤的模组仍然留在原文件和游戏里，只是不进入“待汉化”列表。

如果某个被过滤的模组其实需要翻译，可以在 GUI 中点击“用户白名单”，添加它的 modid，保存后重新扫描。白名单保存在项目旁的 `ferry_whitelist.json`，以后启动仍然有效。

命令行也可以强制加入：

```powershell
py mc_ai_translator.py scan --instance "D:\Minecraft\versions\<实例目录名>" --include-modid kurolib
```

## 人工汉化自动降级

扫描时会区分三类中文来源：

- **模组自带 / 社区资源包的 `zh_cn`**：视为“人工汉化”。
- **本工具上次生成的 AI 包**：通过包内的 `AI_TRANSLATION_NOTICE.txt` 标记识别，不算人工汉化。

规则：检测到某模组存在人工汉化（哪怕不完整）时，默认**自动降级**——不为它生成 AI 翻译，保持人工汉化原样（可能没汉化完）。没有人工汉化的模组才进入“待 AI 翻译”。

命令行加 `--fill-community` 可只补“有人工汉化但不完整”的模组缺失 key，不改已有人工译文。

**可选的人工汉化语料补缺**：GUI 勾选「参考人工汉化语料补全缺失 key（不改原译文）」或命令行加 `--refine-community`，将同一模组现有的 `en_us` / 人工 `zh_cn` 配对当作术语和文风参考，只让 AI 翻译**人工汉化与现有 AI 包都尚未覆盖的 key**。已有人工译文从不作为待翻译项，也不写进 AI 包；原模组 JAR 和人工资源包不变。需要 OpenAI 兼容、Anthropic、Gemini 或本地模型（模型池不能混入 MyMemory / DeepL）。默认关闭；关掉后恢复人工汉化优先、AI 自动让位的规则。

模组列表中的「汉化完成」列只在英文 key 已全部由人工汉化和 AI 补全、且没有强制还原英文的键时显示「已完全汉化」。鼠标悬停在标识上可查看人工与 AI 各自的 key 数量和占总数的比例；相同 key 优先算人工汉化，摆渡包里标记为社区来源的 key 也计入人工，不重复计数。

若扫描到模组 JAR 的 `.class` 中有疑似直接写死的界面文字，列表显示「疑似硬编码文本」并取消“已完全汉化”标识。语言 key 已全部覆盖不等于游戏所有文字都可用资源包汉化；1.7.10 的 Inventory Pets 备注（如 `Favorite Food:`）就是代码常量，Legends 也有大量疑似硬编码文本。具体遗漏仍应结合游戏内原句判断。

`en_us` 中没有文字的空 key 不算待翻译项，也不会计入上述比例。点击已经覆盖全部有效 key 的模组时，会提示无需翻译。

每次扫描或开始翻译时，如果本工具的 AI 资源包中有后来由人工汉化覆盖的 key，会自动从 AI 包删掉这些重叠条目；人工汉化覆盖全模组后，该模组的 AI 条目全部退场。如果整个 AI 包因此为空，包文件也会删除。保留了用户主动选择的「强制还原英文」覆盖项。

**联网安装人工汉化包**：在模组列表中右键选中一个模组，点「联网安装人工汉化包…」，粘贴人工汉化资源包的 HTTPS ZIP **直链**。工具检查资源包根目录的 `pack.mcmeta` 和该模组的 `assets/<modid>/lang/zh_cn.json`（或 `.lang`），把 ZIP 放到当前实例的 `resourcepacks/ProjectFerry_Human_<modid>.zip`，随后重新扫描。已有同名导入包时会询问是否更新；来源不是合法资源包或不含所选模组的中文文件时会拒绝安装。请到游戏的资源包界面启用下载的包，必要时调整优先级；原模组和 AI 资源包不会被下载操作直接改写。联网查找具体资源包的站点尚未接入，因此需要由用户提供下载直链。

**社区基线（可关闭）**：开始翻译时按模组 ID 查询 CFPA 的 `zh_cn.json`，本地缓存 7 天。实例里已安装的汉化优先，其次采用社区命中的 key，AI 只补剩余缺口；断网或 404 自动退回原有翻译流程。`FERRY_SOURCES.json` 标明每条来自 AI 还是社区，包内含来源署名。首次联网前会弹出说明；可在 GUI 关闭。

**其他扫描与质检**：支持 Fabric/Quilt 内嵌 jar（最多 3 层）、只读扫描 KubeJS 语言文件，及 Patchouli `assets/.../patchouli_books/.../en_us/entries/` 的正文/标题；只输出完整翻译的条目页，宏与结构字段保持原样。底部「质量检查」只检查摆渡包的译文，可删除、重翻或锁定选中 key；「日志」提供只读查看、清空、打开目录。翻译区可测试连接。

1.6.1 之前的版本没有资源包机制，因此**不做支持**；本项目所有汉化都靠资源包覆盖，不修改模组文件。

**游戏版本与向上兼容**：优先读取实例客户端 jar 里的 `version.json`，直接取权威的 `pack_version.resource_major/resource_minor`（例：1.21.10→69.0，26.3→97.1），因此未来版本无需改代码，版本也对得上（含 minor 与快照 id）。读不到时再退回版本表：1.21.8→64，1.21.9→69.0，1.21.11→75.0，26.1→84.0，26.2→88.0，26.3→97.1，26.4 快照→98.0（数据来自 minecraft.wiki 的 Pack_format 资源包格式历史）。游戏已改用年份版本号（如 `26.2`、`26.3`），填 `1.26.3` 会自动归一为 `26.3`。从 1.21.9（25w31a）起 `pack.mcmeta` 使用 `min_format` + `max_format`（整数或 `[major, minor]` 数组），更早版本仍用 `pack_format`；读取时都识别。比已知最新版本更新的实例采用最新已知格式并可在 GUI 手动改。已有摆渡包时：检测到的格式比旧包新则升级，否则尊重已有/手动值。未知版本时 GUI 手动选择或输入（支持小数如 97.1）；CLI 用 `--pack-format N`，或用 `--yes` 明确强制 15（包内标注版本未确认）。HMCL/PCL 私有配置字段尚未实机确认；自动修改 `options.txt` 保持关闭。1.6.1 之前无资源包机制，不做支持；早期版本向下兼容暂停，留待后续版本处理。

**1.6.1–1.12.2 旧版语言包**：`pack_format` 1–3 写入 `assets/<modid>/lang/zh_CN.lang`（UTF-8），1.13+ 仍写 `zh_cn.json`。如果旧版实例已有本工具生成的 JSON 语言包，再次扫描或翻译时会将其迁移成 `.lang`，先把原 ZIP 备份为同目录的 `.pre-lang-backup`。资源包能被游戏启用不等于模组文字确实被覆盖；请在游戏内观察启用/关闭前后的物品名变化。当前 1.7.10 Forge 测试实例的旧包已迁移，游戏内复测仍待完成。

## 右键删除 / 还原汉化

工具不修改模组 JAR，删除汉化只能通过资源包覆盖来实现。在模组列表里右键选中项：

- **删除本工具 AI 汉化（回退人工/官方）**：把该模组的 AI 译文从本工具生成的资源包中卸载，使其回退到模组自带的人工/官方汉化；如果它没有人工汉化，则显示英文。卸载的译文保存在同目录下的 `<资源包文件名>.uninstalled.json`，模组列表标为「AI 汉化已卸载」，不会被“继续翻译”悄悄重新生成。
- **重载已卸载的 AI 汉化**：仅在选中已卸载模组时可用，直接从记录恢复，不调用模型。已有人工作品或强制还原英文的 key 不会被覆盖。重载后重新扫描；没有可恢复 key 时，该模组随人工汉化自动退场。
- **强制还原英文（生成覆盖资源包）**：生成/更新覆盖资源包，把该模组在 `en_us` 里的全部键以英文写进 `zh_cn`，连模组自带的人工/官方汉化一起盖掉。包内用 `FERRY_REVERTED.json` 记录被还原的键，重新扫描时会显示「已还原英文（不翻译）」，点“翻译全部待AI”也不会把它们再翻回去。

两种操作都会清除该模组的失败记录，完成后自动重新扫描。卸载状态同样适用于命令行翻译。要撤销强制还原英文，可移除相应资源包；卸载过的 AI 译文请在模组列表右键选择「重载」。

## 配置

翻译参数会从项目旁的 `ferry_config.json` 读取（API key、base URL、模型、引擎、pack_format、`fill_community` 开关等）。没有该文件时使用内置默认值。

云模型网络规则：OpenAI、Anthropic、Gemini、DeepL 和用户配置的同类云接口必须使用 HTTPS 远程 Base URL；填写 `localhost`、`127.0.0.1` 或其他本地地址会在开始翻译前拒绝。Ollama / LM Studio 等本地模型请明确选择 `local` 引擎，保留作为省钱选项。

```powershell
py mc_ai_translator.py config          # 查看当前配置（API Key 已打码）
py mc_ai_translator.py config --init   # 生成默认配置文件
```

### 密钥安全

- `ferry_config.json` 存有你的 API Key，**已被 `.gitignore` 忽略，不要提交到公开仓库**。需要模板时复制不含密钥的 `ferry_config.example.json`。
- 日志（`ferry.log`）、`ferry_cache/`、`ferry_progress.json`、`ferry_locks.json`、`user_glossary.json`、`__pycache__/`、`*.pre-lang-backup` 都不该入库，`.gitignore` 已一并忽略。
- `config` 命令的输出和日志、错误弹窗都会对 Key 打码；请求失败信息也会先脱敏再写日志。
- 若配置所在目录已初始化 git 且 `ferry_config.json` 还没被忽略，程序启动时会弹「密钥安全提示」提醒。
- 如果这个 Key 曾经出现在公开过的地方，请到服务商后台**吊销并重新生成**。

## 保护规则

- AI 只翻译人工汉化尚未覆盖的 `en_us` key；主动开启语料补缺时，人工译文仅用作当前模组的参考，绝不覆盖已有人工译文。
- 不内置第三方汉化词典。
- 资源包不修改原模组 JAR，删除即可还原。
- 占位符校验失败时保留英文。
- 生成的资源包默认写入实例的 `resourcepacks` 目录，`pack_format` 按实例版本自动检测（`--pack-format` 可覆盖）。
- 翻译支持随时停止（产出只含已翻译部分、可直接用的不完整包）、单批失败自动重试并跳过（相关 key 保留英文），已完成的批次写入缓存供下次断点续传。

## 汉化能力说明与限制

摆渡计划只能替换 Minecraft 语言文件（lang key）。**启动时**会弹出一次能力说明，以下内容无法通过资源包覆盖：

- 硬编码在模组代码里的文本（部分模组把物品备注、对话、配置界面文字直接写死在代码中）；
- 图片 / 纹理里的文字；
- 存在物品 NBT / 数据组件里的描述（lore）。

**每次扫描实例后**，底部「汉化限制」按钮会列出疑似含硬编码文本、无法完全汉化的模组（启发式检测，可能含少量技术字符串，仅供参考）。

## 引擎配置

翻译引擎及各自 API key 都保存在 `ferry_config.json`（`engine` 默认 `mymemory`，其余字段如 `openai_api_key`、`anthropic_api_key`、`gemini_api_key`、`deepl_api_key`、`mymemory_email` 等）。GUI 里有“引擎 / API Key / 保存设置”入口。

GUI 引擎选择分三层：先选「免费（MyMemory / DeepL Free）/ 自定义接口」，自定义时再选「兼容接口（OpenAI / Anthropic / Gemini）」。自定义接口可「保存预设」，把多套 API（key / Base URL / 模型）存成名字，下次下拉直接切换。

## 翻译进度与断点续传

- 扫描表显示每个模组的「AI 已翻 / 缺中文 key / 状态（待翻译 / 部分翻译 / 已完成）」，翻一半中断后重新扫描能直接看到进度。
- 检测到上次未翻完的模组时，顶部会提示剩余条数，「翻译全部待AI」按钮自动变为「继续翻译」。
- 失败的 key 记录到 `ferry_progress.json`，下次扫描在表格中标红「上次失败 N 条」；翻译成功的 key 会自动从失败记录里移除。

## 术语表

翻译前会保护 Minecraft 官方术语（苦力怕、红石、精准采集等），翻译后统一还原成官方译名，保证跨模组术语一致；同时保护模组名和 Forge/NeoForge 等品牌词不被乱翻。

联网更新官方术语表（从 Mojang 官方语言文件提取实体 / 附魔 / 效果 / 物品名）：

```powershell
py mc_ai_translator.py glossary --update   # 联网下载并提取术语表
py mc_ai_translator.py glossary            # 查看术语表概况
py mc_ai_translator.py glossary --show     # 列出全部术语条目
```

更新结果存在 `user_glossary.json`（可手动编辑）；`ferry_config.json` 里的 `glossary` / `keep_untranslated` 字段可追加自定义术语 / 不翻译词。

## 并发与多模型加速

翻译默认串行。`concurrency`（`ferry_config.json` 或 `--concurrency`）控制并发翻译的线程数：

```powershell
py mc_ai_translator.py translate --instance "..." --concurrency 3
```

多模型池：在 `ferry_config.json` 里配置 `model_pool`（多个引擎），翻译时按批次轮换给不同引擎，配合 `concurrency` 实现多模型并行提速：

```json
{
  "model_pool": [
    { "engine": "mymemory", "weight": 3 },
    { "engine": "openai", "api_key": "sk-...", "model": "gpt-4o-mini", "weight": 1 },
    { "engine": "local", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5", "weight": 2 }
  ]
}
```

资费平摊：批次按「字符量 ÷ weight」贪心分给各模型，工作量大（≈资费高）的模型少分，免费的/便宜的给更高 `weight` 多分，使各模型承担的翻译量和开销尽量均衡。权重默认 1，可在 GUI「设置 → 添加模型 → 权重」里调。翻译完成后会报告各模型的用量（条数 / 批次数 / 字符数），方便对账。

GUI 底部有「设置」页，可图形化配置并发数和多模型池：既可以「添加模型」逐个填引擎 / Key / Base URL / 模型 / 权重，也可以「从预设添加」把你保存过的多个自定义接口一键拉进并发池，还能「编辑选中」。多选保存后即用你提供的多个 API 并发翻译。「关于」页列出了本项目使用的开源项目与数据来源。

扫描结果会缓存：点「翻译选中模组 / 继续翻译」时直接进入翻译，不会重新扫描实例；只有翻译完成（写包或有失败）或你手动点「扫描当前实例」时才重新扫描。

注意：MyMemory 免费额度有限，并发太高容易触发限流，建议 2~4；付费/LLM/本地引擎可适当调高。

## 界面速览

- 主界面默认折叠「翻译设置」，只显示一行摘要（引擎 · 模型 · 并发）；点右侧「展开」或底部「设置」修改。
- 翻译按钮（翻译选中 / 翻译全部待AI / 停止）与进度条紧贴在模组列表下方，看表 → 选模组 → 点翻译一条动线。
- 表格支持搜索（按模组名模糊过滤，输入即筛）、筛选（全部 / 只看有缺口）、点列头排序（数值列按数字排，标题带 ▲/▼）。
- 「完成度」列显示迷你进度条 + 百分比；「状态」列用 Unicode 符号 + 短文字双编码（✓完成 ◐部分 ○待翻 ◈人工 ⊘已还原 ⊗已卸载 ✗失败）。
- 单一实例时自动隐藏「实例」列。
- API Key 默认掩码，点「显示」才明文；双击模组翻译前会弹确认框并显示待翻条数。
- 表格默认约 15 行、正文字号加大；列宽可用鼠标拖动分隔线调整，排序时不会被重置。

## 开发与测试

- 运行环境：Python 3.11+（用到标准库 	omllib），仅 tkinter 等标准库，无第三方依赖。
- 运行测试：

`powershell
python -m unittest -v test_refine test_spec_p0
`

- 持续集成：.github/workflows/tests.yml 会在 push / PR 时于 Python 3.11、3.12 上跑上述测试。
- 设计与计划文档在 docs/（SPEC_P0P1.md 为完整规格，PLAN_LEGACY_RESOURCE_PACK.md 为早期版本覆盖验证计划）。
- 密钥与运行期文件（erry_config.json、erry.log、erry_cache/、user_glossary.json 等）已在 .gitignore 中，请勿提交；配置模板见 erry_config.example.json。

## 发布前

- 把 ProjectFerry.pyw 顶部的 PROJECT_URL 改成你的仓库地址（关于页会显示）。
- erry_icon.png 是应用图标与关于页图标；没有该文件时程序会用代码画一个兜底。
- 窗口大小与位置保存在 erry_ui.json（已 gitignore），不随仓库分发。

