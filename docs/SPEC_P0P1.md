# 摆渡计划 / ProjectFerry — P0 + P1 实施规格

> 给执行方（opencode）的施工单。请**逐条**执行，不要自由发挥。
> 本文档中所有「红线」为硬性约束，违反即为不合格。

---

## 0. 项目背景与铁律

### 0.1 这是什么

`D:\摆渡计划\mc_ai_translator.py`（核心库，~1900 行）+ `ProjectFerry.pyw`（Tkinter GUI）。
功能：扫描 Minecraft 实例的模组，找出 `en_us` 里有中文缺失的 key，用 AI 翻译后生成**低优先级资源包**。

### 0.2 六条红线（最高优先级，任何改动都不得违反）

| # | 红线 |
|---|---|
| R1 | **绝不修改任何已存在的译文**。模组自带 `zh_cn`、实例已装汉化包、社区汉化 —— 一律只读。检测到就"让位"，AI 不碰 |
| R2 | **绝不修改模组 JAR**。所有产出都是资源包（zip），删除即还原 |
| R3 | **占位符 / 格式码必须原样保留**。`%s` `%d` `%1$s` `{0}` `{player}` `§a` `$(br)` 等，校验失败就保留英文，不得硬写 |
| R4 | **单条失败不得中断整批**。失败的 key 记录、跳过，已完成的批次写入缓存 |
| R5 | **不做 OCR**，不处理纹理 / 图片内的文字。本规格也不涉及 |
| R6 | **API Key 绝不写入日志 / 导出文件 / 报错信息**，展示一律脱敏为 `sk-***` |

### 0.3 现有架构速查（改之前先读）

| 关注点 | 位置 |
|---|---|
| 语言文件匹配正则 | `LANG_FILE_RE`（L371），`language_files_in_zip`（L378），`language_files_in_directory`（L397） |
| 翻译主流程 | `translate_entries`（~L1640） |
| 占位符 mask / 还原校验 | `mask_placeholders`、`mask_terms`、`mask_all`、`restore_and_validate` |
| 写资源包 | `write_pack`（L1087） |
| 版本 → pack_format | `PACK_FORMATS`（L144）、`pack_format_for_version`（L1362）、`detect_pack_format`（L1375） |
| 让位逻辑 | `yield_to_community`（~L1150） |
| 缓存 | `ferry_cache/*.json`，key = `cache_key(entries, engine, model, ...)` |
| 配置 | `ferry_config.json`，字段白名单见 `DEFAULT_CONFIG`（L98） |
| GUI 主类 | `ProjectFerry.pyw` 的 `FerryApp` |

---

## P0-1　pack_format 探测增强（**当前正在静默产出废包，最高优先级**）

### 问题

`detect_pack_format`（L1375）只有两个探测源：

1. `mmc-pack.json`（仅 MultiMC / Prism Launcher）
2. 实例目录名里的正则 `1\.\d+(\.\d+)?`

**官方启动器 / HMCL / PCL 的实例目录名经常不含版本号**（就叫 `.minecraft`），
此时直接 `return 15`。后果：1.12.2 应为 `3`、1.7.10 应为 `1`，却拿到 `15` → 
`pack.mcmeta` 版本不匹配 → **游戏根本不认这个包，用户完全无感知**。
（`PACK_FORMATS` 表本身是完整且正确的，问题只在探测。）

### 要求

按下列优先级探测，**命中即停**；每级都要记 DEBUG 日志说明从哪命中。

| 优先级 | 探测源 | 取值方式 |
|---|---|---|
| 1 | `<实例>/mmc-pack.json` | `components[uid=="net.minecraft"].version`（已有逻辑，保留） |
| 2 | `<实例>/versions/<dir>/<dir>.jar` 或 `.json` | 取 `<dir>` 名；`.json` 存在时优先读其 `id` 字段 |
| 3 | `<实例>/.minecraft/versions/<dir>/...` | 同上（官方启动器布局） |
| 4 | `hmclversion.cfg`（HMCL） | 读含 `version` 的键值。**字段名需实机确认，读不到就跳过，不要瞎猜** |
| 5 | `PCL.ini`（PCL2） | 同上，需实机确认 |
| 6 | 模组元数据声明的 MC 版本 | 见下 |
| 7 | 实例目录名正则（已有逻辑） | 保留作为最后兜底 |

**第 6 级细节**（扫描 `mods/*.jar`）：
- Forge / NeoForge：`META-INF/mods.toml` → `[[dependencies.minecraft]] versionRange`
- Forge（1.12.2 及更早）：`mcmod.info` → `mcversion`
- Fabric / Quilt：`fabric.mod.json` → `depends.minecraft`
- **取众数**（出现次数最多的版本），**不要取最高值**
- 抽样上限 50 个 jar，避免大整合包扫描过慢

### 兜底策略（重要，不要沿用旧的静默 fallback）

- 全部探测失败时 **不得静默返回 15**。
- GUI：弹出「未检测到游戏版本，请选择或手填」下拉（列出 `PACK_FORMATS` 覆盖的全部版本 + 手动输入 pack_format）。
- CLI：报错退出并提示 `--pack-format N`；若带 `--yes` 强制非交互，才 fallback，并**在 stdout 和包内 README 里显著标注「版本未确认」**。
- **已存在的摆渡包复用其 pack_format**（读 `pack.mcmeta`），不要每次重算覆盖。
- GUI 常驻显示：`检测到 1.20.1 → pack_format 15`，并提供手动修改入口。

### 验收

- [ ] 目录名为 `.minecraft`、内有 `versions/1.12.2/1.12.2.jar` 的实例 → 得到 `3`
- [ ] MultiMC 实例 → 行为与改动前一致
- [ ] 无任何线索的实例 → GUI 弹选择框，不静默出包
- [ ] 已有摆渡包时复用其 pack_format

---

## P0-1b　老版本输出格式（1.7.10 / 1.6.x / 1.12.2）— **与 P0-1 强耦合，必须同时完成**

### 问题（比 P0-1 更严重：格式根本不对，不只是版本号错）

Minecraft 的语言文件规范随版本变过三次（依据 Minecraft Wiki「语言」条目）：

- 1.6.1（13w24a）才把语言文件支持**加进资源包**；此前只能改 jar
- 1.11（16w32a）文件名改为**全小写** `en_us.lang`
- 1.13（18w02a）改为 **JSON** 格式 `en_us.json`

而 `write_pack`（L1087 起）**写死** `assets/<modid>/lang/zh_cn.json`。
结果是：

| 目标版本 | 实际需要的产物 | 现在产出 | 结论 |
|---|---|---|---|
| 1.13+ | `zh_cn.json` | `zh_cn.json` | ✅ 正常 |
| 1.11–1.12.2 | `zh_cn.lang`（key=value） | `zh_cn.json` | ❌ **包无效** |
| 1.6.1–1.10.2（含 1.7.10） | `zh_CN.lang`（**大写 CN**） | `zh_cn.json` | ❌ **包无效** |
| ≤1.5.2 | 资源包机制不存在 | — | ❌ 无解，必须拒绝 |

**注意**：读取侧没问题（`LANG_FILE_RE` 有 `IGNORECASE` + `locale.lower()`，
能正确识别老模组的 `en_US.lang` / `zh_CN.lang`）。坏的只有写入侧。

### 要求

1. 新增：
   ```python
   def lang_file_name(pack_format: int) -> str:
       if pack_format >= 4:  return "zh_cn.json"   # 1.13+
       if pack_format == 3:  return "zh_cn.lang"   # 1.11–1.12.2
       return "zh_CN.lang"                          # 1.6.1–1.10.2，大写
   ```
2. 新增 `.lang` 序列化：`key=value` 一行一条，保留 `#` 注释行能力；
   **编码 UTF-8 且绝不写 BOM**（BOM 会让第一行 key 损坏）。
   换行在 `.lang` 中写 `\n`，与 JSON 侧的转义保持一致。
3. `write_pack` 用 `lang_file_name(pack_format)` 决定 entry 名与序列化方式
   （该函数已接收 `pack_format` 参数，不用改签名）。
4. **同步修改读取侧**，否则删包/让位/合并会失效：
   - `load_pack_translations`（L1106）的正则 `assets/([^/]+)/lang/zh_cn\.json`
     → 改为同时接受 `.json` 与 `.lang`、大小写不敏感
   - `merge_pack_translations`、`remove_ai_translations`、`load_pack_reverted`
     中所有按路径匹配的地方一并检查
5. **≤1.5.2 明确拒绝**：检测到该范围时报错并说明「该版本无资源包机制，
   资源包汉化不可行」，**不要尝试改 jar**（违反 R2）。
6. GUI 显示一行：`目标 1.7.10 → 输出 zh_CN.lang（pack 1）`，让用户能核对。

### 依赖

**本项依赖 P0-1**：不知道版本就不知道该输出 `.json` 还是 `.lang`、大写还是小写。
两项必须一起做、一起验收。

### 验收

- [ ] 1.7.10 实例 → 包内为 `assets/<modid>/lang/zh_CN.lang`，内容 `key=中文`，UTF-8 无 BOM
- [ ] 1.12.2 实例 → 包内为 `zh_cn.lang`，`pack.mcmeta` 的 `pack_format` 为 3
- [ ] 1.20.1 实例 → 行为与改动前完全一致（`zh_cn.json`）
- [ ] 对上述老版本包执行「删除 AI 汉化」→ 能正确读出并移除（验证读取侧已同步改）

---

## P0-1c　新版本向上兼容（26.x 与 1.21.7+）— **2026-09-25 新增，与 P0-1 同级紧急**

> 行号会随 P0-1 改动漂移，一律**以函数名定位**：
> `MC_VERSION_RE`、`pack_format_for_version`、`PACK_FORMATS`、`write_pack`、`detect_pack_format`。

### 问题（三处硬伤，全部已发生）

MC 版本号方案已于 2025-12 变更，2026-03-24 发布 **26.1（Tiny Takeover）**，资源包格式 **84**；
26.2 快照已达 **88**。而当前代码：

1. **`MC_VERSION_RE = r"(?<![\d.])1\.\d+(?:\.\d+)?(?![\d.])"` 只匹配 `1.x`**
   → `26.1` / `26.2` 完全匹配不到 → `detect_minecraft_version` 返回 `None`
   → `detect_pack_format` 抛 `ValueError("未检测到游戏版本")`。**26.x 用户直接不可用。**
2. **`PACK_FORMATS` 止于 `((1, 21, 6), 63)`**，缺 1.21.7 ~ 1.21.11 与全部 26.x。
   → `pack_format_for_version` 遍历完**静默返回最后一个满足项 63**，不报错
   → 1.21.7+ 用户拿到 63 的包（应为 64/69/75/84）→ 能加载但有红色警告且每次需确认。**静默出错，最坏。**
3. **`pack_format` 现为浮点**（wiki 记 84.0）。代码全程 `int`（`write_pack(pack_format: int)`）。
   JSON 里 `84` 与 `84.0` 等价，暂不致命，但类型标注与比较逻辑需放行浮点。

### 需补入 `PACK_FORMATS` 的数据（来源：Minecraft Wiki「Java Edition 26.1」条目 + mcversions.net）

| 版本 | pack_format | 备注 |
|---|---|---|
| 1.21.7 / 1.21.8 | 64 | 现有代码缺 |
| 1.21.9 / 1.21.10 | 69 | 现有代码缺 |
| 1.21.11 | 75 | 现有代码缺 |
| 26.1 | 84 | wiki 记 84.0 |
| 26.2 | 88 | **快照期数值，需确认正式版后写入** |

> 上表 1.21.7+ 与 26.2 数值来自第三方整理，**实施时请对照 Minecraft Wiki
> 「Pack format / List of pack formats」逐条复核后再写死**。写错比不写更糟。

### 要求

1. **版本正则**：`MC_VERSION_RE` 改为能匹配新方案，例如
   `(?<![\d.])(?:1|2[0-9])\.\d+(?:\.\d+)?(?![\d.])`。
   必须同时通过：`1.20.1` `1.21.11` `26.1` `26.2` `26.10`（若将来有）；
   必须继续排除：`1.1`（老到无意义）、版本号内部片段。
2. **补齐上表**；`pack_format` 相关类型标注由 `int` 放宽为 `int | float`。
3. **未知版本禁止静默 fallback**（与 P0-1 兜底原则一致）：
   若探测到的版本**高于 `PACK_FORMATS` 最后一项**，必须走与「探测不到」相同的路径
   —— GUI 弹窗让用户选/手填，并在包内写 `README_VERSION_UNCONFIRMED.txt`。
   **绝不允许静默返回表内最大值。**
4. **表外置（强烈建议）**：把 `PACK_FORMATS` 移到 `pack_formats.json`（与 `ferry_config.json` 同级），
   启动时读文件，文件缺失则用内置副本兜底。
   MC 现在一年发数个版本，硬编码在 `.py` 里等于**每出一个版本就要发一次新版**，不可持续。

### 附：用 `supported_formats` 声明兼容范围（本项的核心收益）

MC **1.20.2（23w31a）** 起 `pack.mcmeta` 支持可选字段 `supported_formats`：

```json
{ "pack": {
    "pack_format": 15,
    "supported_formats": { "min_inclusive": 4, "max_inclusive": 88 },
    "description": "..."
} }
```

约束（必须遵守）：
- `pack_format` **仍是必需字段**，且其值**必须落在 `supported_formats` 范围内**；
- 1.20.2 之前的版本**忽略**该字段，退化为单版本包 → **向后安全，可放心加**。

**摆渡特别适合声明宽范围**：摆渡包内**只有 lang 文件**，不含纹理 / 模型 / item model，
而 `assets/<ns>/lang/*.json` 的结构自 1.13 起**从未变过**。
普通资源包不敢跨大版本声明（纹理与模型路径会变），摆渡敢。

规则：
- `pack_format >= 4`（1.13+）→ 可声明 `{"min_inclusive": 4, "max_inclusive": <当前已知最大>}`；
- `pack_format <= 3`（`.lang` 时代）→ **不要声明范围**，`.lang` 与 `.json` 不通用，跨了必然出问题。

> 待确认（实施者必须实测，不要照抄）：有资料称 **1.21.9 起改用 `min_format` / `max_format`**
> 替代 `supported_formats`。此说法来自第三方站，**未在 wiki 核实**。
> 请在 1.21.9+ / 26.x 实机验证两种写法哪种被接受；若新字段生效，按 `pack_format` 分段生成。

### 顺带修正：`.lang` 文件名大小写（与 P0-1b 冲突，现代码是错的）

当前 `write_pack` 实现为 `if pack_format <= 3:` → 写 `assets/<modid>/lang/zh_CN.lang`（**大写 CN**）。
但 P0-1b 要求 `pack_format == 3`（1.11–1.12.2）写 **`zh_cn.lang`（小写）**，
因为 1.11（16w32a）起文件名已改全小写。

**现状 1.12.2 输出大写 → 与 spec 不符，1.12.2 是老版本模组最活跃的分支，影响面大。**

务实做法（推荐，成本几乎为零）：`pack_format <= 3` 时**同时写入两个文件**
`zh_CN.lang` 与 `zh_cn.lang`，内容完全相同。
`pack_format 2`（1.9–1.10.2）用大写、`3`（1.11–1.12.2）用小写，双写可彻底规避大小写争议。
若确认某一版只认一种写法，再收敛为单文件。

### 验收

- [ ] `26.1` 实例 → 能正确识别版本 → `pack_format` 为 84（不是报错、不是 63）
- [ ] `1.21.11` 实例 → `pack_format` 为 75，不出现「为旧版本制作」警告
- [ ] 故意构造一个 `PACK_FORMATS` 里没有的版本（如 `99.1`）→ **弹窗要求用户确认**，且包内生成 `README_VERSION_UNCONFIRMED.txt`；**不得静默出包**
- [ ] 1.13+ 生成的包含 `supported_formats`，在 1.20.2+ 客户端加载**无红色警告**
- [ ] 1.13+ 的包放到 1.19.4 客户端仍能加载（旧版忽略该字段）
- [ ] `pack_format <= 3` 的包内同时存在 `zh_CN.lang` 与 `zh_cn.lang`
- [ ] `pack_formats.json` 外置生效；删除该文件后程序仍能启动（内置兜底）

---

## P0-2　jar-in-jar 嵌套 jar 支持（Fabric / Quilt）

### 问题

Fabric / Quilt 支持把依赖库**内嵌**在 `META-INF/jars/*.jar`。
当前 `language_files_in_zip` 只扫顶层 entry，嵌套模组**整个漏掉** → 
这些模组永远显示为「无中文」且翻不了。这是 MC 多加载器支持上唯一真实的缺口。

### 要求

1. 在 `language_files_in_zip` 中，除 `assets/...` 外，额外匹配 `META-INF/jars/[^/]+\.jar`（忽略大小写）。
2. 读到内嵌 jar 的原始 bytes，用 `io.BytesIO` + `zipfile.ZipFile` 递归解析。
3. **递归深度上限 3**，防止病态嵌套。
4. 内嵌 jar 的 modid 取**它自己**的 `assets/<ns>/lang/` 命名空间（不要用外层 jar 的）。
5. 同一 namespace 的多个 lang 文件按现有规则合并去重。
6. 内嵌 jar 里的 `zh_cn` **同样参与让位判断**（即：内嵌模组若自带中文，AI 不翻）。
7. source 路径标注为 `outer.jar!META-INF/jars/inner.jar!assets/x/lang/en_us.json`，便于排查。

### 红线

- 不展开、不解压、不写任何临时文件到磁盘，全程内存操作。
- 内嵌 jar 损坏 / 非 zip → 记 WARNING 日志并跳过，不得中断整个外层 jar 的扫描。

### 验收

- [ ] 造一个含 `META-INF/jars/dep.jar`、且 `dep.jar` 内有 `assets/depmod/lang/en_us.json` 的测试 jar → 能扫出 `depmod`
- [ ] `dep.jar` 内同时有 `zh_cn.json` → 该模组被判定为已有人工汉化，不进入待翻列表
- [ ] 坏 zip 内嵌 → 仅 WARNING，外层扫描继续

---

## P0-3　Patchouli 手册汉化（唯一值得做的非 lang 文本）

### 为什么是它

Patchouli 1.20+ 强制书本内容走资源包系统，官方机制**天生就是"只放你翻过的条目"**：

- 英文原文：`assets/<ns>/patchouli_books/<book>/en_us/{entries,categories,templates}/**/*.json`
- 译者产出：`assets/<ns>/patchouli_books/<book>/zh_cn/entries/**/*.json`
- 官方文档原话：*"For translators: Please don't include in your folder anything you aren't overriding."*
- 未翻译的条目**自动 fallback 到 en_us**。

这与本项目的 R1 红线完全吻合。

**已确认不可做、不要碰**：`data/<ns>/patchouli_books/<book>/book.json`（书名 / landing_text）在 `data/` 下，资源包覆盖不了，需要数据包，超出范围。

### 要求

1. **扫描**：匹配 `assets/([^/]+)/patchouli_books/([^/]+)/en_us/(entries|categories)/.*\.json`
2. **跳过条件**（任一命中即跳过，R1）：
   - 同 book 下 `zh_cn/` 已存在同名相对路径文件
   - 该条目在摆渡自己的包里已翻过
3. **只翻译这些字段**（其余字段原样保留，不得增删）：
   - `name`（条目标题）
   - `pages[].text`
   - `pages[].title`（若存在）
4. **绝不翻译**：`icon`、`category`、`sortnum`、`type`、`entry`、`advancement`、`recipe`、`flag`、`priority`、`pages[].type` 及一切 ResourceLocation / ID 字段。
5. **输出**：`assets/<ns>/patchouli_books/<book>/zh_cn/entries/<与 en_us 相同的相对路径>`
6. **只输出真正翻了的条目文件**，不输出未翻的、不输出 `templates/`（模板含宏结构，翻了容易炸）。`categories/` 可选，建议首版不翻。

### 宏保护（关键，最容易翻车的地方）

Patchouli 正文含内联宏，翻译时**必须 mask 后还原**，否则整页渲染错乱：

| 宏形式 | 示例 |
|---|---|
| 无参 | `$(br)`、`$(item)`、`$(thing)` |
| 成对 | `$(bold)文字$()`、`$(italic)文字$()`、`$(4)文字$()` |
| 带参 | `$(l:entry_id)文字$(/l)`、`$(k:key)` |

建议正则：
- 开标签：`\$\([a-z0-9_]+(?::[^\s)]+)?\)`
- 闭标签：`\$\(/[a-z]+\)` 与 `\$\(\)`

复用现有 `mask_placeholders` / `restore_and_validate` 的机制，**不要另起一套**。

### 架构改动（必须做，别绕）

现有 `Entry` 是扁平的 `(modid, key, value)`，装不下嵌套 JSON。建议：
- 给 `Entry` 加 `kind` 字段：`"lang"`（默认，向后兼容）或 `"patchouli"`
- Patchouli 条目额外携带 `book`、`rel_path`、`json_pointer`（如 `/pages/0/text`）
- `cache_key` 必须把 `rel_path` + `json_pointer` 纳入，否则缓存会串

### 验收

- [ ] Botania `lexicon` 或 Ars Nouveau 手册 → 生成 `zh_cn/entries/**` 且游戏内书本正常渲染
- [ ] 宏 `$(br)` `$(bold)...$()` 在译文中完整保留
- [ ] 已有 `zh_cn/entries/xxx.json` 的条目 → 被跳过，文件字节不变
- [ ] `icon` / `category` / `recipe` 等 ID 字段与 en_us 完全一致

---

## P0-4　KubeJS 语言文件扫描

### 要求

1. 新增扫描源：`<实例>/kubejs/assets/<ns>/lang/*.json`（`<ns>` 任意，常见为 `kubejs`、`ftbquests`）。
2. 与模组 jar 里的 lang **同等对待**：参与缺失检测、参与让位判断。
3. 该目录是 KubeJS 的**虚拟资源包**，玩家资源包优先级高于 mod 资源，摆渡的覆盖包能正常盖住它。
4. **只读**，绝不写回 `kubejs/` 目录（R1/R2）。

### 验收

- [ ] `kubejs/assets/kubejs/lang/en_us.json` 里的 key 出现在待翻列表
- [ ] 执行后 `kubejs/` 目录文件哈希不变

---

## P1-5　社区汉化基线（联网拉现成汉化，AI 只补缺口）

### 目标

翻之前先查社区是否已有该模组的汉化，命中就直接用，**不要浪费 token 重翻一遍**。

### 数据源

| 源 | 用途 | 建议拉取方式 |
|---|---|---|
| `CFPAOrg/Minecraft-Mod-Language-Package` | 按 modid 取现成 `zh_cn.json` | jsDelivr：`https://cdn.jsdelivr.net/gh/CFPAOrg/Minecraft-Mod-Language-Package@main/assets/<modid>/lang/zh_cn.json`（**走 CDN，避开 GitHub API 60 次/小时限流**） |
| `CFPATools/i18n-dict` | 术语对齐 | 同样走 jsDelivr |

- 数据源 URL 写进 `ferry_config.json`（`community_base_url`、`community_dict_url`），可改。
- **必须本地缓存**（建议 `ferry_cache/community/<modid>.json`，带 TTL，默认 7 天）。

### 三层优先级（新增中间层）

1. **最高**：实例已装汉化包 / 模组自带 `zh_cn` → AI 完全不碰（现有逻辑）
2. **新增（中）**：社区库联网命中 → 直接采用社区译文，**AI 不翻这条**
3. **最低**：AI 补剩余缺口

### 红线

- 社区译文**仍然低于**玩家实例里实际在用的汉化（第 1 层）。即：若第 1 层已覆盖某 key，社区和 AI 都不得写它。
- 网络失败 / 404 / 超时 → **静默降级为 AI 翻译**，记 WARNING，绝不中断任务。
- 必须在包内写 `FERRY_SOURCES.json`，记录 `modid → key → "ai" | "community"`，供让位时按来源精确退场。
- 提供 GUI 开关（建议默认开启，**首次使用时弹一次说明**），可随时关。
- 包内保留来源署名（CFPA 及其许可证信息），不要白嫖不留名。

### 验收

- [ ] 断网时行为与改动前完全一致（只是慢一点/多花 token）
- [ ] 社区命中的 key 不进入 AI 批次（用量报告里能看出）
- [ ] 玩家后来装了真汉化包 → 重扫描时社区来源的条目正确退场

---

## P1-6　QC 质检面板

### 检查项

| 等级 | 检查 |
|---|---|
| 🔴 错误 | 译文 == 原文（漏翻） |
| 🔴 错误 | 空译文 / 仅空白字符 |
| 🔴 错误 | 占位符丢失（对比 source 与 target 的占位符**多重集**，不是简单包含） |
| 🔴 错误 | 格式码错乱（`§` 后跟非法字符） |
| 🟡 警告 | 译文不含任何中文字符，且原文长度 > 15（可能是残留英文） |
| 🟡 警告 | 长度异常：> 原文 3 倍 或 < 原文 1/5 |
| 🟡 警告 | 陈旧 key：摆渡包里有，但 `en_us` 里已不存在（模组更新过） |
| 🟡 警告 | 同一 key 在不同模组下译法不一致（术语不统一） |

### UI

- 列表：模组 / key / 原文 / 译文 / 问题类型 / 等级
- 支持多选，三个动作：**重翻选中**、**删除选中（下次重翻）**、**加入不翻译词表**
- 入口：扫描后底部按钮「质量检查」，有问题时该按钮高亮/带数量角标

### 验收

- [ ] 手工造一条"译文=原文"的条目 → 被标红列出
- [ ] 占位符 `%1$s` 丢失 → 被标红
- [ ] 「重翻选中」只重翻选中项，不动其他

---

## P1-7　自动启用资源包（改 options.txt）

### 要求

1. 目标文件：`<实例>/options.txt`（版本隔离时确认实际路径，HMCL/PCL 各有布局）。
2. 字段格式：
   - 1.13+：`resourcePacks:["file/<包名>.zip"]`
   - ≤1.12.2：格式不同（逗号分隔，无方括号引号）——**需实机确认后再实现，不要照抄新版格式**
3. **追加，不覆盖**：保留玩家已启用的包，只插入摆渡的包。

### 红线（这一条风险最高，务必照做）

- ⚠️ **必须先确认 Minecraft 未运行**。检测到 MC 进程时，禁止写入并提示"请先关闭游戏"（MC 退出时会重写 options.txt，覆盖我们的修改）。
- ⚠️ **写入前备份**为 `options.txt.ferrybak`，提供「撤销自动启用」入口。
- ⚠️ **顺序需实机验证**：MC 资源包列表中"靠上的优先级更高"，但 options.txt 数组顺序与 GUI 显示顺序的对应关系**必须先实测确认**再决定插到数组头还是尾。摆渡是**低优先级**包，必须放在会被其他包覆盖的位置。实测前该功能默认关闭。
- ⚠️ 解析失败 / 文件损坏 → 放弃修改，提示用户手动启用，**绝不写一个可能损坏的文件**。

### 验收

- [ ] MC 运行时尝试启用 → 拒绝并提示
- [ ] 启用后原 options.txt 的包列表完整保留
- [ ] 撤销后 options.txt 与备份字节一致

---

## P1-8　锁定词条 / 单条重翻

在模组列表右键菜单新增：

| 菜单项 | 行为 |
|---|---|
| 锁定此译文 | 写入 `ferry_locks.json`（`modid → key → 译文`）。锁定后：不重翻、不让位删除、不被 QC 标红 |
| 此 key 不翻译 | 写入 `ferry_config.json` 已有的 `keep_untranslated` |
| 重翻此条 | 清该条缓存 → 单独发一次翻译请求 → 更新包 |
| 重翻整个模组 | 清该模组全部缓存 → 重翻 |

- 锁定项在 GUI 中要有可见标记。

---

## P1-9　日志面板 + 测试连接

### 日志

- 落盘 `<项目>/ferry.log`，按天滚动，保留 7 天。
- 记录：扫描结果摘要、每批次（引擎 / 条数 / 耗时 / 成功失败）、失败原因、网络异常。
- GUI 新增「日志」页：只读文本框 + 「清空」+「打开日志目录」。
- 🔴 **红线 R6**：任何情况下不写 API Key。展示和落盘一律 `sk-***` 脱敏。

### 测试连接

- GUI 引擎设置区加「测试连接」按钮。
- 发一条最小请求（如单个短 key），显示：成功 / 模型名 / 延迟 / 失败原因。
- 失败时给出可操作的提示（key 错？base_url 错？模型名错？网络？）。

---

## P2-A　成本预估（重点：穷鬼友好）

### 翻译前预估

1. 统计：待翻条目数、源文本总字符数、按模组拆分。
2. Token 估算（写在代码注释里，标注为估算）：
   - 英文源文本：`chars / 4`
   - 中文译文：`chars × 0.8`（实际约 0.7~1.0 token/字）
   - 输入 token = 源 chars/4 + system prompt 固定开销（实测一次，约 300）+ 每条目 JSON 结构开销（约 10）
   - 输出 token ≈ 输入 token × 0.8
3. 单价表放 `ferry_config.json` 的 `pricing`（美元 / 百万 token），结构：
   ```json
   "pricing": {
     "openai":  {"input": 0.15, "output": 0.6},
     "deepseek": {"input": 0.07, "output": 1.1}
   }
   ```
   内置常见模型默认值，用户可覆盖；未配置的模型按 `openai` 默认值并标注"估算值"。
4. GUI：翻译前弹确认框，显示 **预计花费区间（低～高，±50%）**，底部状态栏常驻「本次已用 ¥X」。

### 免费引擎单独处理

| 引擎 | 显示 |
|---|---|
| MyMemory | 字符额度消耗 / 剩余（匿名 5000 字符/天；填 `mymemory_email` 后 5 万/天） |
| DeepL Free | 月度额度消耗（50 万/月） |

### 翻译后对账

- 若接口返回 `usage`（OpenAI 兼容的 `prompt_tokens` / `completion_tokens`）→ 用真实值。
- **接口不返回 usage → 显示"未知"并标灰显示估算值，不要假装精确**。
- 多模型池时按模型分别统计（现有 `format_usage` 已统计条数/批次/字符，扩展为 + token + 费用）。

---

## P2-B　词典导入导出

### 数据源

| 文件 | 内容 |
|---|---|
| `user_glossary.json` | 术语表（官方 MC 术语 + 自定义） |
| `ferry_cache/*.json` | 翻译缓存 |
| `ferry_config.json` 的 `glossary` / `keep_untranslated` | 用户自定义术语 / 不翻译词 |
| `ferry_locks.json`（P1-8 新增） | 锁定译文 |

### 导出

- 格式：**CSV** 和 **JSON** 两种。
- CSV 列：`en,zh,scope,source`
  - `scope`：modid 或空（全局）
  - `source`：`official` / `user` / `cache` / `community` / `lock`
- ⚠️ **CSV 必须 UTF-8 with BOM**，否则 Excel 打开中文乱码（本机是中文 Windows）。
- 导出范围可选：全部 / 指定 modid / 仅术语表 / 仅缓存 / 仅锁定项。
- 默认文件名 `ferry_glossary_export_<YYYYMMDD>.csv`。

### 导入

- 接受 CSV / JSON，自动识别。
- 冲突策略**必须让用户三选一**（不要替他决定）：跳过 / 覆盖 / 仅补充缺失。
- 导入前校验并列出问题行：空 `en`、重复 `en`（给出重复次数）、`zh` 含未闭合占位符 / 格式码异常。
- 🔴 **红线**：导入只影响**未来的翻译与术语还原**，**绝不回写任何已有的 `zh_cn` 文件**（R1）。

### UI

GUI 新增「词典」页：搜索 / 编辑 / 删除 / 导入 / 导出 / 清空，分页或虚拟滚动（缓存可能有上万条）。

---

## 未来兼容预留（多语言 / 国际化 — 现在就把路留好）

> 用户目前只是畅想，**本轮不实现**。但要求：本轮改动**不得把路堵死**，顺手做下面 3 件零成本的事。

1. 所有字面量 `"zh_cn"` → `settings["target_lang"]`（默认 `"zh_cn"`）；所有 `"en_us"` → `settings["source_lang"]`（默认 `"en_us"`）。
2. 语言文件名匹配保持大小写不敏感（现有 `.lower()` 已正确，别动）。
3. 术语表按语言对分文件：现在是 `user_glossary.json`；预留 `user_glossary.<lang>.json`，
   代码按 `target_lang` 查找，找不到就回退默认文件。

**已知将来会踩的坑（先记着）**：
- `zh_tw` 需繁体输出，prompt 要明示；简繁转换需 `opencc`（可选依赖，不要硬依赖）。
- `ru_ru` / `pl_pl` 等有复数形式，MC lang 无复数机制，部分 mod 用 ICU `Format` → 复数占位符极易翻错。
- `ja_jp` 的术语表需单独维护（日文官方译名与中文不同源）。
- 部分游戏/模组只有 `ja` 原文没有英文 → 源语言必须可配（这一点通用化时是硬需求）。

---

## 执行顺序建议

```
P0-1 + P0-1b + P0-1c  (版本探测 + 老版本输出格式 + 新版本向上兼容)  ← 必须一起做，正在产废包
P0-2 (jar-in-jar)
P0-4 (KubeJS)       ← 与 P0-2 同为扫描层，一起做
P0-3 (Patchouli)    ← 需改 Entry 结构，单独做
P1-9 (日志)         ← 先有日志，后面才好调试
P1-5 (社区基线)
P1-6 (QC)
P1-8 (锁定/重翻)
P1-7 (自动启用)     ← 最后做，要先实测 options.txt 顺序
P2-A (成本预估)
P2-B (词典导入导出)
```

## 全局验收

- [ ] 六条红线（R1~R6）逐条自查通过
- [ ] 对一个含 20+ 模组、含 Fabric 嵌套 jar、含 Patchouli 手册、含 KubeJS 的整合包跑通全流程
- [ ] 卸载（删除资源包）后实例目录与改动前完全一致（用文件哈希对比校验）

---

# 附录 C　UI 改造规格（2026-09-25 基于最新代码）

> **实施时机：务必放在 P0/P1 全部功能完成之后。** UI 大改与功能开发并行必然冲突（两边都在动 `_build_ui` 与回调函数）。
>
> 行号会漂移，一律**以函数名 / 变量名定位**。行号取自 2026-09-25 的 `ProjectFerry.pyw`。

## 现状（`_build_ui`，L233-399，自上而下）

| 区块 | 行 | 评价 |
|---|---|---|
| 标题 2 行 | L245-246 | 副标题文案有错，见 P2-1 |
| 实例框（下拉 + 5 按钮） | L251-270 | 「汉化限制」按钮放错位置，见 P1-5 |
| 状态行 | L272-273 | OK |
| pack_format 行 | L274-278 | OK，P0-1 的成果，保留 |
| **表格** | L280-314 | **主角，但被下方设置区压扁** |
| **翻译设置框（6 行 + 进度条）** | L316-381 | **低频配置常驻，是主要问题** |
| 底部栏（提示 + 5 按钮） | L383-394 | OK |

**核心矛盾：低频的「翻译设置」占了约 180px，把高频的表格挤扁；而真正高频的「翻译」按钮
（`run_bar`，L370-378）被压在整个设置区的最底部，跟它要作用的表格隔了六行。**

---

## P0-1　并发控件重复，两个入口会互相覆盖（**真 bug，优先修**）

`_build_ui` L361-363 有 `self.concurrency_spin` / `self.concurrency_var`；
`_show_settings` L879-880 **又建了一个独立的** `concurrency_var` Spinbox。

复现路径：
1. 用户在主界面把并发改成 8，**没点「保存设置」**；
2. 打开「设置」弹窗 → 弹窗从 `self.config` 读到的仍是旧值 1；
3. 点保存 → `self.concurrency_var.set(1)` → **主界面刚改的 8 被悄悄覆盖回 1**。

用户完全无感知，只会觉得"这软件怎么老变慢"。

### 要求

- **并发只保留一个入口**。建议：保留设置弹窗里的（旁边有说明文字更好），
  主界面 L360-363 那一对 `Label + Spinbox` 改为**只读的当前值显示**（如「并发 4」标签），
  或干脆删除。
- 若两处都保留，则必须：**弹窗打开时用 `self.concurrency_var.get()` 初始化**（不是从 config 读），
  且任一处修改立即双向同步 —— 但**不推荐**，两个入口本身就是设计问题。

---

## P0-2　API Key 明文（L338-341）

```python
self.api_key_entry = ttk.Entry(grid_frame, textvariable=self.api_key_var)   # 无 show 参数
```

旁边有人、截图求助、录屏演示时直接泄露。

### 要求

- 改 `show="•"`（默认掩码）。
- 右侧加一个「显示」切换按钮（`ttk.Checkbutton` 或 `Button`），在 `show="•"` 与 `show=""` 间切换。
- **落盘与日志仍遵守 R6**：本项只改显示，不改存储策略。

---

## P0-3　翻译按钮离表格太远（L370-378）

`run_bar` 现在挂在 `grid_frame` 的 `row=5`，位于整个「翻译设置」框的最底部。

用户真实动线是：**看表格 → 选模组 → 点翻译**。现在中间隔着引擎、Key、URL、模型、预设、
5 个按钮、2 个复选框共六行。

### 要求

- 把 `run_bar`（停止 / 翻译全部 / 翻译选中）**从 `grid_frame` 移出**，
  放到 `table_frame` 底部（`self.selection_var` 标签那一行，L312 附近）或紧贴表格下方。
- 进度条（L380）也一并移到表格下方，与按钮同区。
- `pack()` 顺序要跟着改：确保空间不足时**优先压缩表格**，按钮与进度条始终可见
  （现有注释 L396-399 已经意识到这点，改造后要继续保持）。

---

## P1-1　「翻译设置」6 行整体收起

引擎 / 接口 / 预设 / API Key / Base URL / 模型 / 保存预设 / 保存设置 / 测试连接 / 更新术语表 /
并发 / 2 个复选框 —— 全是**低频**操作，不该常驻。

### 要求

- 方案 A（推荐）：`translate_frame` 默认**折叠**，标题栏右侧放「展开 / 收起」按钮，
  展开状态记入 config。主界面默认只留一行状态摘要，例如
  `引擎：DeepSeek · 模型：deepseek-chat · 并发 4　[更改…]`（点击打开设置弹窗）。
- 方案 B：把 Key / Base URL / 模型 / 预设 / 并发整体并入「设置」弹窗
  （`_show_settings`，L868-920），主界面只留引擎选择 + 两个复选框。
- 无论哪个方案，`run_bar` 与进度条都要按 P0-3 移到表格下方，**不随设置区折叠**。

---

## P1-2　表格缺搜索 / 筛选 / 排序（L280-314）

200+ 模组的整合包现在只能靠滚。

### 要求

1. **搜索框**：放在表格上方，按模组名（及 modid）模糊过滤，输入即筛，不回车。
2. **筛选**：至少提供「只看有缺口」（missing > 0）与「全部」两档，用 `ttk.Combobox` 或 Checkbutton。
3. **列头排序**：给 `self.tree.heading(col, ...)` 加 `command`（L286-288 的循环里），
   点表头按该列升降序切换。ttk 原生支持，成本极低。
   - 排序列要给视觉反馈（如标题后加 ▲/▼）。
   - 数值列（missing / ai / community / complete）按数字排，不要按字符串排。

---

## P1-3　状态只有颜色，没有图标（L291-298）

现在 8 个 `tag_configure` 全靠 `foreground` 区分。色盲用户不可用，快速扫视也慢。

### 要求

状态文字前加符号，做成**图标 + 颜色双编码**：

| 状态 | 符号 | 颜色 |
|---|---|---|
| 完成 | ✓ | #137A3F |
| 部分 | ◐ | #B26A00 |
| 待翻译 | ○ | #1A1A1A |
| 人工汉化 | ◈ | #4C5A68 |
| 已还原 | ⊘ | #8A6D00 |
| 已卸载 | ⊗ | #9A5A22 |
| 失败 | ✗ | #C00000 |

（符号用 Unicode，不要引入图片资源。）

---

## P1-4　「汉化完成」与「状态」两列语义重叠

`complete`(105px) + `status`(210px) 合计 315px，说的是同一件事（完成度）。

### 要求

- 合并为一列：`complete` 显示百分比 + 迷你进度条，`status` 只放符号 + 短文字。
- 省下的宽度给 `modid`（现 170px）。

---

## P1-5　「汉化限制」按钮位置错（L269）

它跟「实例」毫无关系，却放在实例框里（跟「刷新实例 / 扫描当前实例 / 浏览… / 用户白名单」并列）。

### 要求

移到底部按钮栏（L387-394，与「质量检查 / 日志 / 设置 / 关于」同排），或移到工具栏。

---

## P1-6　「实例」列单实例时永远同值（L281-284）

只有 1 个实例时该列每行的值都一样，白占 130px。

### 要求

- 实例数 ≤ 1 时从 `columns` 中移除（或 `self.tree.column("instance", width=0, stretch=False)` 隐藏）。
- 实例数变化时动态重建列。

---

## P2-1　副标题文案与功能矛盾（L246）

现文案：

> 只扫描英文语言文件和已有中文 key，不修改模组，**不读取汉化译文**。

但程序现在有 `refine_community`（参考人工译文补全缺失 key，L365-366）与
`community_enabled`（联网查社区汉化，L367-368）—— **明明会读**。

### 要求

改成与真实行为一致，例如：

> 只补缺口，不覆盖也不改动任何已有的人工译文。

（要体现"不覆盖"这个真正的红线，而不是"不读取"。）

---

## P2-2　底部提示遗漏右键功能（L386）

现文案只提「Ctrl 多选」和「双击翻译」。右键菜单里明明有：

删除 AI 译文 / 还原英文 / 单条重翻 / 跳过该 key / 安装人工汉化包。

### 要求

补全提示，例如：

> Ctrl 多选；双击翻译该模组；右键可删除译文 / 还原英文 / 重翻 / 跳过词条 / 安装人工汉化包。

---

## P2-3　双击直接开翻，无确认且花钱（L299 → `_on_tree_double`）

双击 = 立刻调 API 烧 token，手滑一下就是钱。

### 要求

双击改为弹出确认框，并把 P2-A 的成本预估接进来：

```
create · 31 条待翻 · 预计 ¥0.42
        [确认翻译]  [取消]
```

若 P2-A 尚未完成，至少显示条数（`create · 31 条`）并要求确认。

---

## P2-4　进度条没有文字（L380）

孤零零一条 bar，不知道在翻谁、翻到哪。

### 要求

进度条上方加一行实时文本，例如：`正在翻译 create（7/12 批）· 已完成 143/214 条`。

---

## 验收

- [ ] 主界面改并发 → 打开设置弹窗 → 看到的是**同一个值**；在弹窗改 → 主界面同步（P0-1）
- [ ] API Key 默认显示为掩码，点「显示」才明文（P0-2）
- [ ] 窗口高度压到最小 → 翻译按钮与进度条**始终可见**，被压缩的是表格（P0-3）
- [ ] 表格支持搜索、筛选、列头排序，数值列按数字排（P1-2）
- [ ] 状态列有符号，仅靠黑白打印也能区分（P1-3）
- [ ] 单实例时「实例」列消失（P1-6）
- [ ] 副标题文案与 `refine_community` / `community_enabled` 的实际行为一致（P2-1）
- [ ] 双击翻译会先弹确认，并显示条数与预估花费（P2-3）
- [ ] 翻译过程中有实时文字说明当前进度（P2-4）
