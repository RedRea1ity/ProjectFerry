# 摆渡计划 / ProjectFerry

> 人无语言则茫然无依，故为摆渡。
> Where words fail, we ferry.

一个 Minecraft 模组 AI 汉化工具。我本身没什么编程基础，这算是靠 AI 一点点憋出来的、第一个敢往 GitHub 上放的东西。

它干的事其实挺简单：进你的整合包，看看哪些模组没中文，然后用 AI 把缺的那部分补上。**只补缺口，绝不动你已经有的人工/官方汉化**——一旦检测到别人翻过了，它就自己退场（渡你过河，到岸即离）。所有改动都写在资源包里，删掉就恢复原样，模组本体一根毛都不碰。

灵感来自《边狱巴士》零协汉化组那句「人无语言则茫然无依」。

## 怎么打开

- **不想装 Python**：直接双击仓库根目录的 `ProjectFerry.exe`，免安装。
- 装了 Python：双击 `ProjectFerry.bat`（不会有黑框闪一下就没了这回事）。
- 或者双击 `ProjectFerry.pyw`（前提是系统已经关联了 Python）。

打开之后它会自己找本机的 MC 实例，填进上面的下拉框。选一个，下面的列表就是缺少中文的模组。

## 它能做啥、不能做啥

**能做**：扫描模组语言文件（含旧版 `.lang`），找出没中文的 key，用翻译引擎（默认免费的 MyMemory）生成一个低优先级的资源包。有人工/官方汉化就自动让位。

**不能直接做、但绕路做了的**：写死在模组代码里的文字、存在物品 NBT 里的描述（lore）——这些压根不在语言文件里，资源包永远覆盖不了。摆渡的办法是配合一个显示层替换的模组（见下面的「桥接」章节），把「英文原文 → 中文」的规则交给它在游戏里替换显示。存档照样一个字节不动。

**真做不了的**：图片/纹理里烤死的字。这个谁都没辙，除非你把美术也 AI 了。

每次扫描完，底部「汉化限制」按钮会列出疑似含硬编码文本的模组（启发式检测，会翻 `.class` 文件里的字符串常量，可能混进点技术字符串，仅供参考）。

## 桥接：NBT 和硬编码的显示层替换

先说清楚为什么会硬编码：有些模组作者把物品备注、任务文本直接写死在代码里，不走语言文件。这种东西资源包拿它毫无办法——但游戏渲染文本的时候，总得经过渲染层，所以在渲染层做替换就行了。

**现在的方案是给 LiteralBridge 供数据**（写这篇的时候我刚拆完它的 jar，发现人家把活干完了，遂改为给人家供数据，自研模组原地冻结）：

1. 扫描 + 翻译完，点底部「**导出桥接映射**」。它会写两样东西：
   - `<实例>/config/literalbridge/ferry.json`：英文原文 → 翻译键 的规则文件（LiteralBridge 的标准外部格式，改完自动热重载）；
   - 资源包里补 `assets/literalbridge/lang/zh_cn.json`：翻译键 → 中文。
2. 确认实例 `mods` 里有 [LiteralBridge](https://github.com/Losketch/LiteralBridge)（没有的话导出时会自动帮你打开项目页，下载一个丢进 mods）。
3. 进游戏。硬编码文本在显示层变成中文；删掉模组和规则文件就回到原样。

硬编码文本的翻译：设置摘要行的「**翻译硬编码文本**」勾上后，检测器扫出来的疑似硬编码英文（会翻 `.class` 的字符串常量）会随翻译批一起交给 AI，译文存资源包旁的 `.hardcoded.json`，导出时一并并入规则。检测是启发式的，会混进一些奇奇怪怪的句子，翻出来偶尔有废话，别惊讶——反正精确匹配，游戏里没这句话就永远不显示。

**感谢 [Losketch](https://github.com/Losketch) 的 LiteralBridge**——我本来头铁要自己写一个模组把所有版本都适配了，拆开他的 jar 发现人家连 mixin 都铺好了，遂跪。他出钩子，我出数据，你出整合包。

（自研的摆渡桥模组在 `bridge/` 目录里冻结着，作为 LiteralBridge 没覆盖到的版本/加载器的兜底，方案见 [docs/BRIDGE_PLAN.md](docs/BRIDGE_PLAN.md)。暂时不投入。）

## 默认会过滤掉的模组

纯前置、库、性能优化、底层修复类的模组默认不翻译，比如 Architectury、Cloth Config、GeckoLib、KuroLib、内存泄漏修复、星光、FerriteCore、Entity Culling、ModernFix 之类。

地图、JEI、Jade、背包整理、枪械、任务、怪物这些面向玩家的都不过滤。被过滤的模组还是老老实实待在游戏里，只是不进「待汉化」列表。

如果你觉得某个被过滤的模组其实该翻，GUI 点「用户白名单」把它的 modid 加进去，保存后重扫就行，白名单存在 `ferry_whitelist.json`，下次打开还在。

## 人工汉化优先（这条是底线）

扫描时会把中文来源分成几类：模组自带/社区包里的 `zh_cn` 算「人工汉化」；本工具上次生成的 AI 包靠包里的 `AI_TRANSLATION_NOTICE.txt` 认出来，不算。

**规则**：只要某模组有人工汉化（哪怕只翻了一半），默认就不给它生成 AI 翻译，保持原样。没有人翻的才进「待 AI 翻译」。

几个可选玩法：

- `--fill-community`：只补「有人工汉化但不完整」的模组缺的那几个 key，不改已有译文。
- GUI 勾「参考人工汉化语料补全缺失 key」或 `--refine-community`：把同一个模组现有的 `en_us` / 人工 `zh_cn` 配对当术语和文风参考，只让 AI 翻**人工和现有 AI 包都没覆盖的 key**。已有人工译文永远不会被当待翻译项，也不会写进 AI 包。需要 OpenAI 兼容 / Anthropic / Gemini / 本地模型（并发池里不能混 MyMemory / DeepL）。默认关闭。
- 列表里的「汉化完成」列，只在英文 key 全被人工 + AI 覆盖、且没有强制还原英文的键时，才显示「✓ 已完全汉化」。鼠标悬停能看到人工和 AI 各占多少。同一个 key 优先算人工，不重复计。
- `en_us` 里没有文字的空 key 不算待翻译，也不计入比例。

如果扫到模组 JAR 的 `.class` 里有疑似写死的界面文字，列表会标「⚠ 疑似硬编码」，并取消「已完全汉化」。语言 key 全翻了不等于游戏里全中文——1.7.10 的 Inventory Pets 备注（`Favorite Food:` 那种）就是代码常量，Legends 也一大堆。具体哪条没翻，还是得对着游戏里的原句看。

## 右键菜单

列表里右键选中项：

- **删除本工具 AI 汉化**：把这个模组的 AI 译文从摆渡包里卸掉，回退到模组自带的人工/官方汉化；没有的话就显示英文。卸载的译文存到 `<资源包名>.uninstalled.json`，列表标「AI 汉化已卸载」，不会被「继续翻译」偷偷加回来。
- **重载已卸载的 AI 汉化**：直接从记录恢复，不调模型。人工译文和「强制还原英文」的 key 不会被覆盖。
- **强制还原英文**：生成/更新覆盖包，把这个模组 `en_us` 里所有键用英文写进 `zh_cn`，连人工/官方汉化一起盖掉。包内用 `FERRY_REVERTED.json` 记录，重扫显示「已还原英文（不翻译）」，点「翻译全部待AI」也不会再翻回去。
- **重翻整个模组 / 联网安装人工汉化包 / 设置不翻译 key**。
- **加入不翻译名单**：这个模组完全跳过 AI 翻译，状态列显示「∅ 已排除（不翻译）」；「翻译全部待AI」和双击都会自动跳过它。右键选「移出不翻译名单」即可恢复。

删完会自动重扫。想撤销「强制还原英文」就把对应资源包删掉；卸载过的 AI 译文在右键里选「重载」。

## 联网功能

- **查询在线汉化**：表格里选中模组后点底部按钮，自动查 CFPA 社区整合包（内置来源）和你自定义的查询源，看哪些模组已有现成汉化；结果里可直接打开链接。查询源在「设置 → 编辑查询源…」里添加，每行一个网址、用 `{modid}` 代替模组 ID。
- **检查更新**：启动时每周自动查一次 GitHub 最新版本（可在弹窗里关掉），「关于」页也能手动查。发现新版本可以直接自动更新（下载后替换 exe，重启生效），或去发布页手动下载。
- **社区汉化基线**：拉取 CFPA 的整合包（14 天缓存一次）本地抽取。更早版本的按模组逐个访问 CDN 的方式因为 CFPA 目录结构调整而失效，已弃用。
- **版本表联网更新**：见下面「版本兼容」。

## 版本兼容

1.6.1 之前没有资源包机制，所以**不做**。所有汉化都靠资源包覆盖，不碰模组文件。

**1.6.1–1.12.2 旧版**：`pack_format` 1–2（1.6.1–1.10.2）写 `assets/<modid>/lang/zh_CN.lang`，`pack_format` 3（1.11–1.12.2）起资源包内文件名强制全小写、改写 `zh_cn.lang`；1.13+ 才写 `zh_cn.json`。旧实例里如果已经有本工具生成的 JSON 包，重扫/翻译时会自动迁成 `.lang`，原 ZIP 先备份成同目录的 `.pre-lang-backup`。

**新版向上兼容**：优先读实例客户端 jar 里的 `version.json`，直接拿 `pack_version.resource_major/resource_minor`（比如 1.21.10→69.0，26.3→97.1），所以以后出新版本基本不用改代码。读不到才退回版本表：1.21.8→64，1.21.9→69.0，1.21.11→75.0，26.1→84.0，26.2→88.0，26.3→97.1，26.4→99.0（当前快照值，正式版以 version.json 为准）。游戏现在是年份版本号（`26.2`、`26.3`），你要是填成 `1.26.3` 它会自动当成 `26.3`。1.21.9（25w31a）起 `pack.mcmeta` 用 `min_format` + `max_format`（整数或 `[major, minor]`），更早的还是 `pack_format`，读取时两种都认。

**版本表联网更新**：出新的 Minecraft 版本而本地版本表还没跟上时，工具会去仓库根目录的 `pack_formats.json` 拉最新表（raw.githubusercontent 主源 + jsDelivr 备源，结果缓存 24 小时到 `ferry_cache/`），拉不到就继续用内置表，再不行才弹手动选择。所以以后每次游戏更新，只要在 GitHub 上给这个 JSON 补一行，旧安装不用重新下载程序。不想联网可以在 `ferry_config.json` 里把 `online_pack_format` 设为 `false`。

未知版本时 GUI 会让你手动选/填（支持 97.1 这种小数），命令行用 `--pack-format N`，或者 `--yes` 硬按 15 走（包里会标「版本未确认」）。HMCL/PCL 私有配置我还没实机确认，`options.txt` 自动启用也一直关着。

> 资源包能被游戏加载 ≠ 模组文字真的变了，这个还是得进游戏看启用/关闭前后的物品名。

## 翻译进度 / 断点续传

- 列表里显示每个模组的「AI 已翻 / 缺中文 key / 状态」，翻一半中断了重扫也能看到进度。
- 检测到上次没翻完的，顶部提示剩余条数，「翻译全部待AI」会自动变成「继续翻译」。
- 失败的 key 记在 `ferry_progress.json`，下次扫描标红「上次失败 N 条」；翻成功后自动从失败记录里去掉。

## 术语表

翻译时会把 Mojang 自动词库作为上下文术语提示（例如 `Redstone Dust` → 红石粉），不再把 `Lead`、`Map` 这类多义短词脱离句子强行替换。你在 `ferry_config.json` 的 `glossary` 里亲自填写的词仍会严格固定；模组名及 Forge/NeoForge 等品牌词也会保护。

联网更新官方术语表（从 Mojang 官方语言文件里提取实体/附魔/效果/物品名）：

```powershell
py mc_ai_translator.py glossary --update   # 联网下载并提取
py mc_ai_translator.py glossary            # 看概况
py mc_ai_translator.py glossary --show     # 列全部条目
```

结果存在 `user_glossary.json`（可以手动改）；`ferry_config.json` 里的 `glossary` / `keep_untranslated` 也能加自定义术语 / 不翻译的词。

## 引擎

| 引擎 | 说明 |
| --- | --- |
| `mymemory` | MyMemory，免费，默认（匿名约 5000 字符/天，填 `mymemory_email` 能提到 5 万） |
| `deepl` | DeepL（默认免费版端点） |
| `openai` | OpenAI 兼容，可改 `openai_base_url` 接 DeepSeek、Kimi、Qwen 等 |
| `anthropic` | Anthropic（Claude）兼容 |
| `gemini` | Google Gemini 兼容 |
| `local` | 本地模型（Ollama / LM Studio，OpenAI 兼容，默认 `http://127.0.0.1:11434/v1`） |

引擎和 key 都存在 `ferry_config.json`。GUI 里分三层选：先选「免费 / 自定义接口」，自定义再选「兼容接口」，还能「保存预设」把多套 API 存成名字，下次直接切。

云接口必须用 HTTPS 远程地址；填 `localhost` / `127.0.0.1` 会在开翻前直接拦下。想在本地跑就走 `local` 引擎。

## 并发与多模型加速

默认串行。`concurrency` 控制同时翻几个批次：

```powershell
py mc_ai_translator.py translate --instance "..." --concurrency 3
```

多模型池在 `ferry_config.json` 里配 `model_pool`，按批次轮换给不同引擎，配合 `concurrency` 并行提速：

```json
{
  "model_pool": [
    { "engine": "mymemory", "weight": 3 },
    { "engine": "openai", "api_key": "sk-...", "model": "gpt-4o-mini", "weight": 1 },
    { "engine": "local", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5", "weight": 2 }
  ]
}
```

批次按「字符量 ÷ weight」贪心分，贵的/量大的少分，免费便宜的给高 `weight` 多分，尽量让各模型花的钱差不多。翻完会报告每个模型用了多少条/多少批/多少字。GUI「设置」里能图形化加模型、从预设一键拉进池子。

> MyMemory 免费额度有限，并发别太高，2~4 差不多；付费/本地引擎可以往高调。

## 命令行（给爱折腾的）

```powershell
py mc_ai_translator.py detect
py mc_ai_translator.py scan --interactive
py mc_ai_translator.py scan --instance "D:\Minecraft\instances\测试整合包"
py mc_ai_translator.py translate --instance "D:\Minecraft\instances\测试整合包"
py mc_ai_translator.py translate --instance "..." --engine openai --only-modid tacz
py mc_ai_translator.py translate --instance "..." --only-modid tacz,jei
```

默认读 `<实例目录>\mods` 和 `<实例目录>\resourcepacks`，也能单独指定 `--mods` / `--resourcepacks`。

## 配置与密钥

参数从 `ferry_config.json` 读（key、base URL、模型、引擎、pack_format、各种开关）。没有这个文件就用内置默认值。

```powershell
py mc_ai_translator.py config          # 看当前配置（Key 已打码）
py mc_ai_translator.py config --init   # 生成默认配置
```

## 保护规则（写代码时就守着的）

- AI 只翻人工汉化没覆盖的 `en_us` key；主动开语料补缺时，人工译文只当参考，绝不覆盖。
- 不内置第三方汉化词典。
- 资源包不改原模组 JAR，删了就还原。
- 占位符（`%s` `%d` `%1$s` `{0}` `§a` `$(br)` 等）校验失败就保留英文。
- 翻译随时能停，产出是个只含已翻部分、但能直接用的不完整包；单批失败自动重试并跳过，已完成的批次进缓存，下次接着来。

## 界面速览

- 翻译设置默认折叠成一行摘要（引擎、模型、并发），点「▸ 翻译设置」展开；「并发 / 模型池…」和「翻译硬编码文本」开关常驻在旁。
- 翻译按钮和进度条就贴在模组列表下面，看表 → 选模组 → 点翻译。
- 表格能搜索（输入即筛，占位符写着可以搜什么）、筛选（全部 / 只看有缺口 / 只看失败 / 只看含人工汉化 / 只看疑似硬编码 / 只看已完成）、点列头排序（数值按数字排，带 ▲/▼）。
- 「完成度」显示百分比（悬停看构成）；「状态」用符号 + 短文字（✓ 完成 ◐ 部分 ○ 待翻 ◈ 人工 ⚠ 疑似硬编码 ∅ 已排除 ⊘ 已还原 ⊗ 已卸载 ✗ 失败），悬停「状态」列可看完整标记。
- 只有一个实例时自动隐藏「实例」列。
- API Key 默认打码，点「显示」才明文；双击模组翻译前会弹确认框，告诉你要翻几条。
- 列宽能用鼠标拖，排序不会把宽度重置。窗口大小位置会记住。

## 更新日志

**v1.8.0**

- 桥接改投 LiteralBridge 格式：导出时写 `config/literalbridge/ferry.json` 规则文件 + 资源包语言补丁，热重载即生效；实例里没装 LiteralBridge 会自动打开它的项目页。
- 硬编码检测升级为返回全量候选，开启「翻译硬编码文本」后随翻译批交给 AI，译文并入桥接规则。
- 自研摆渡桥模组冻结为兜底（`bridge/` 目录，方案见 docs/BRIDGE_PLAN.md）。

**v1.7.2**

- 「翻译硬编码文本」开关挪到常驻设置行（原来藏在折叠区第七个位置，我自己都找不到）。

**v1.7.1**

- 硬编码文本进翻译管线：候选以 `nbt:<hash>` 为 key 随翻译批交给 AI，译文存资源包旁的 `.hardcoded.json`；「翻译全部待AI」自动纳入只有硬编码候选的模组。

**v1.7.0**

- 摆渡桥模组 M1（1.20.1 Forge，tooltip 显示层精确替换）+「导出桥接映射」按钮。

**v1.6.2**

- 修复翻译线程被社区整合包下载卡死（进度条长时间不动）。

**v1.6.1**

- 表格右键新增「加入不翻译名单」：模组完全跳过 AI 翻译（状态列 ∅ 已排除），翻译全部 / 选中 / 双击都会自动跳过，右键可移出恢复，不用再手动输 modid。

**v1.6.0**

- 新增 GitHub 检查更新：启动时每周自动查一次，「关于」页可手动查；发现新版本可直接自动更新（下载替换 exe，重启生效），也可跳转发布页手动下载。
- 新增「查询在线汉化」：选中模组一键查各来源有没有现成汉化，内置 CFPA 社区整合包源；自定义查询源在「设置 → 编辑查询源…」里加，每行一个网址、用 `{modid}` 代替模组 ID。
- 修复社区汉化基线静默失效：CFPA 改版后旧的 `assets/{modid}` CDN 路径全部 404，本版起改为拉取官方整合包（6.8MB，14 天缓存）本地抽取。

**v1.5.0**

- 界面全面改版：渡口水色配色、卡片式布局、三步上手引导、表格自适应与短状态列、「使用说明」弹窗。
- 修复「下次不再显示」从不生效的问题（配置键没进默认表，弹窗每次启动都回来）。
- 兼容修复：1.11–1.12.2（pack_format 3）按官方要求写小写 `zh_cn.lang`；26.4 兜底格式更新为 99.0。
- 版本表支持联网更新：游戏出新版本后只要在仓库 `pack_formats.json` 里补一行，旧安装自动拿到，不用重新下载程序。
- 新增 CI：打 `v*` tag 自动构建 exe 并发布 Release。

## 开发 / 测试

- Python 3.11+，只用标准库（`tomllib`、tkinter 等），没有第三方依赖。
- 跑测试：

```powershell
python -m unittest -v test_refine test_spec_p0
```

- CI：`.github/workflows/tests.yml`，push / PR 时在 Python 3.11、3.12 上跑上面这些测试。
- 桥接模组在 `bridge/`（1.20.1 Forge），`.github/workflows/bridge.yml` 会构建它；自研桥当前冻结，主推 LiteralBridge 供数路线。
- 设计文档在 `docs/`（`SPEC_P0P1.md` 是完整规格，`BRIDGE_PLAN.md` 是桥接模组的方案与多版本路线）。
- 自己打包 exe：

```powershell
py -m PyInstaller --onefile --windowed --name ProjectFerry --icon ferry_icon.ico --add-data "ferry_icon.png;." ProjectFerry.pyw
```

## 致谢

- Minecraft 官方语言文件（Mojang）——术语表来源
- PandaDevOfficial / Minecraft-All-Lang——语言文件镜像
- CFPAOrg / Minecraft-Mod-Language-Package——社区汉化基线（遵守其许可证与署名）
- Losketch / [LiteralBridge](https://github.com/Losketch/LiteralBridge)——硬编码文本的通用显示层桥接。我本来头铁要自己写，拆开他的 jar 发现人家把活干完了，现在摆渡负责给他供数据。Apache-2.0，好人一生平安。
- Minecraft Wiki · Pack_format——资源包格式与版本号参考
- Python 标准库——它真的一个第三方依赖都没加

## 许可

MIT License，详见 [LICENSE](LICENSE)。

## 如何联系这个可怜的作者

我好尴尬。。。好想死。。想跳楼呃呵呵呵呵呵。。这是我第一次觉得我拉的ai大便有点用处，然后就传GitHub上了。。。。联系可以走zetadarkdragon@gmail.com。。。求求大佬不要喷我。。。。我尴尬的相似。。。
