# 摆渡计划 · 规模化路线（大批量翻译 / 多加载器 / 多版本）

> 状态：方案草案，2026-10-12 起草。写给「牢红 + 一帮 agent」这个只有两个人的维护团队。
> 本文件不表示任何功能已验收 —— 凡未实机验证的，下文都明写「未验证」。

---

## 0. 先给结论

三个诉求，体检结果完全不同：

| 你的诉求 | 真实状态 | 距离 |
|---|---|---|
| 大批量多模组 tooltip + 聊天翻译 | 器官造好了 ~80%，**4 根导线没接** | 半天 |
| 多加载器 | 社区有成熟工具；**你的项目还没有** | 等接线完成之后 |
| 多版本 / 版本桥模组模板生成器 | 你 10-08 已写过方案，**前提条件现在才具备** | 等接线完成之后 |

一句话：**你现在缺的不是方案，是先把已经造好的东西插上电。**

三个诉求里，最省命的一步不是往前冲，而是把 `bridge` 从 **Mixin 注入** 换成 **加载器官方事件** ——
它一次性干掉项目里 90% 的痛苦来源，并且**顺带让「模板生成器」从「不可行」变成「可行」**。

---

## 1. 体检：哪些是真的，哪些是假的

### 1.1 已经造好、但没接线的器官

这几天在 `bridge/` 里其实已经埋好了「大批量翻译」需要的关键结构，只是没人调用：

| 组件 | 文件 | 造好了什么 | 接线状态 |
|---|---|---|---|
| 两级映射表 + 热重载 | `core/TranslationStore.java` | jar 内置表 + **游戏目录外部表**，每秒比对 mtime **热重载**，改完不用重启 | ❌ `setExternalPath()` / `startExternalWatcher()` **从未被调用** |
| 未命中文本采集器 | `core/FerryBridgeCollector.java` | 未命中的原文按「聊天栏 / 物品 tooltip」分桶、去重、写成人可读的 `ferrybridge-collected.txt` | ❌ `record()` **从未被调用**，是个死类 |
| 共享查表内核 | `core/TranslationStore#lookup` | 只做整条精确匹配（红线），剥 § 码 | ✅ 在用 |

**这意味着什么**：「采集 → 翻译 → 灌表 → 热重载」这条链，除了中间「翻译」那段归主程序管，
其他全部已经存在。缺的只是把线接上：

1. `FerryBridgeMod` 里加 `store.setExternalPath(gameDir.resolve(EXTERNAL_NAME))` + `store.startExternalWatcher()`
2. `FerryBridgeMod` 里加 `FerryBridgeCollector.init(gameDir)`
3. `FerryBridgeTooltip` / 聊天注入点未命中时调 `FerryBridgeCollector.record(桶名, 原文)`
4. 主程序 `mc_ai_translator.py` 加一个分支：读 `ferrybridge-collected.txt` → 翻译 → 写 `ferrybridge-mappings.json`

第 4 条之所以便宜，是因为主程序**已经有完整的扫描、检测、翻译、术语表、多模型并发引擎**
（`user_glossary.json` 29KB 术语表、硬编码检测器、`.hardcoded.json` sidecar 都在），
现在只是把「输出 literalbridge 规则」换成/增加「输出 ferrybridge 映射表」。
**不是新建工程，是接一根输出线。**

### 1.2 注入点现状（2026-10-10 实机）

```
生效注入点 12 个    ← 聊天注入点在跑（日志有 injector live: ChatComponentMixin#addMessage）
已替换  0 次        ← 因为映射表里没有那些文本
未命中 12 次        ← 正常：映射表 58 条全是 boh 的 tooltip，聊天内容不在表内
```

**注入点早就不是问题了。问题是「表里没有要翻的东西」** —— 而这正好就是第 1.1 节要解决的。
换句话说：接线一完成，「未命中 N」就会自动变成「采集到 N 条待翻译」，这就是批量翻译的原料。

### 1.3 「仙境有汉化」的真相

`mods/wonderland_zhcn-1.0.0.jar` —— 仙境官方汉化整合包（Losketch 翻译 + 你打包），
覆盖 `assets/the_wonderland/lang/zh_cn.json`。**这是独立语言包，和摆渡桥没有关系。**
所以「仙境的硬编码文本是否被桥翻掉」这件事，至今**没有被观察过**。别把它记成桥的功劳。

---

## 2. 核心判断：Mixin 是负债，官方事件是资产

把项目最近踩的坑列出来，会发现**它们全都来自同一个源头**：

| 踩过的坑 | 根源 |
|---|---|
| refmap 从来没生成过（折腾好几天） | Mixin |
| tsrg2 → SRG 格式转换（专门写了个 Python 脚本） | Mixin |
| `FerryBridgeAccess` 在 mixin 包导致 `IllegalClassLoadError` 崩溃 | Mixin 包隔离 |
| 注入点静默失效（`require = 0` 吞掉失败） | Mixin |
| `Component` 是 interface，`ComponentMixin` 无解只能废弃 | Mixin |
| 构建缓存让映射改动静默失效 | Mixin 的 AP 缓存 |
| 混淆映射随版本重排，refmap 编译期绑定 | Mixin |

**换掉 Mixin，这些坑一次性全部消失。**

而 Forge 早就给了现成的官方事件，覆盖我们两条主要显示通路：

| 通路 | 官方事件 | 状态 |
|---|---|---|
| 物品 tooltip | `ItemTooltipEvent` | ✅ 代码已写（`client/FerryBridgeTooltip.java`），待实机验证 |
| 聊天栏 | `ClientChatReceivedEvent`（有 `getMessage()` / `setMessage(Component)`） | ✅ 已改（2026-10-12 L1，**未实机验证**） |

> `ClientChatReceivedEvent` 已核实存在于 Forge 1.20.1（Forge 从 1.12 时代就有，一直保留）。
> 用它替换 `ChatComponentMixin` 后，**整个 bridge 可以零 Mixin** —— 没有 refmap、没有混淆、
> 没有 mixin 包隔离、没有注入点失效。跨版本时改动面从「改字节码注入目标」降到「改事件 API 签名」。

**这是一次性的投入，换的是以后每一次版本升级的命。**

---

## 3. 四层路线

> 顺序是硬的：**L0 不通不要做 L1，L1 不通不要做 L2。**
> 记忆里那条「先跑通 1，再乘 9」不是保守，是防止得到「3 加载器 × 3 版本 = 9 个全都翻不了」的 jar。

### L0 — 接线（本轮就能做，半天）

把第 1.1 节的 4 根导线接上，主程序加输出分支。

**做完之后你能看到什么**：进游戏正常玩一圈 → 游戏目录多出 `ferrybridge-collected.txt`，
里面按「聊天栏 / 物品 tooltip」分好组，列出所有没翻的英文；交给摆渡批量翻译 → 写出
`ferrybridge-mappings.json` → 桥**不用重启**就生效。

**这直接就是你要的「大批量多模组 tooltip + 聊天翻译」。**

### L1 — 去 Mixin（一次性重构）

`ChatComponentMixin` → `ClientChatReceivedEvent`。做完 bridge 零 Mixin。

- 收益：消灭第 2 节全部 7 个坑；为 L2/L3 铺路。
- 成本：一次重构 + 一次实机验证。
- **风险点**：事件覆盖度可能和 Mixin 不完全一致（比如某些系统消息、动作栏）。要实机比对
  「聊天栏里哪些文本能翻」。不够的部分再单独想办法。

> **状态（2026-10-12）：已实现，构建绿（`./build.sh`），但【未实机验证】。**
> 落地内容：
> - 新增 `client/FerryBridgeChat.java`（挂 `ClientChatReceivedEvent`），
>   删除 `mixin/ChatComponentMixin.java` 与 `ferrybridge.mixins.json`。
> - `mods.toml` 去掉 `[[mixins]]`；`build.gradle` 的 jar manifest 去掉 `MixinConfigs`。
> - **整套 Mixin 构建工具链拆除**：删掉 mixin/gson/guava 依赖、Mixin AP 的全部
>   `-A*` 参数、SRG 映射的编译输入与 `doFirst`、`verifyRefmap` 与 `showAp` 两个任务。
>   实测把 30 MB 的 `gradle/mappings/` 整个移走后构建仍全绿 —— SRG 依赖确已归零。
> - CI 的 `bridge.yml` 去掉已失效的 `verify` 作业（核验注入点签名，零 Mixin 后无意义）。
> - `utils/tsrg2_to_srg.py`、`copy_reobf_mappings.py`、`verify_mixin_signatures.py`
>   三个脚本保留但标注「已停用」，不再参与构建。
> - 报告/HUD 措辞同步：`生效注入点` → `生效路径`，删掉「查日志 [ERROR] [mixin]」这类
>   已经不再成立的提示；诊断报告现在读 `FerryBridgeLive` 的去重计数（原来那个
>   `FerryBridgeDiag.liveInjectors` 每条消息 +1，数值没有意义）。
>
> **实机要重点看**：① 聊天栏的玩家消息 / 系统消息能不能翻；② 动作栏（overlay）要不要翻、
> 翻了会不会乱；③ 别的模组**在客户端直接塞进聊天栏**的消息（不走网络）这个事件收不到，
> 漏了就记录在案，别为此回退 Mixin。

### L2 — 多加载器（等 L1 验证通过）

用 **Architectury Loom**（社区标准方案，支持 Fabric / Forge / NeoForge / Quilt）：

- 现状：`core/` 包**已经零 MC 依赖**，这是最大的红利 —— 换加载器时内核一行不用改。
- 做法：`common/`（core）+ 各加载器薄壳（只写「注册事件钩子 + 读配置」那几十行）。
- 注意：Architectury Loom 官方自称 experimental，1.20.1 上不要为了迁移冒险；
  **等桥在 Forge 上彻底稳定、且真有人要 Fabric 版时再动**。

### L3 — 多版本（靠 CI，不靠模板生成器手工维护）

见第 4 节。

---

## 4. 关于「版本桥模组模板生成器」

**你 2026-10-08 已经写过这个方案了** —— `PLAN_GENERATED_BRIDGE_MOD.md`。
核心设计是：开发时预编译桥模板（按「MC 版本 × 加载器」各一份），
摆渡生成时用 Python 标准库 `zipfile` 把 `translations.json` 写进模板，玩家不需要 JDK。

**这个设计成立，但它有一个前提条件，之前不满足、现在才满足：**

> 模板能否跨版本复用，取决于**桥里有多少编译期绑定的东西**。
> - 桥用 **Mixin** → 注入点编译期绑定字节码，混淆映射每次发布重排 → 模板与版本**死绑**，
>   每升一版都要重新写注入代码。**「模板生成器」在这条路上不成立。**
> - 桥用 **官方事件** → 事件 API 稳定得多 → 模板只需按大版本编译一次，
>   数据注入完全不需要编译。**「模板生成器」在这条路上成立。**

所以：**不是推翻你的设想，是 L1（去 Mixin）让这个设想重新可用。**

L3 的准确形态：

```
① 模板层：按（MC 版本 × 加载器）预编译桥模板        ← 数量可控，agent 编译一次
② 数据层：Python zipfile 注入 translations.json      ← 零编译，纯体力活
③ 检测层：CI 定时查 Mojang 新版本 → 自动改版本号 → 开 PR
④ 验证层：矩阵 CI 自动构建 + 验 refmap/事件签名 → 绿灯才让人看
```

**你在 L3 里的工作只剩两件事**：看 CI 是红是绿；绿了点合并。红叉旁边要有人话说明
（「本次升级坏了：聊天事件签名变了 → 要改 XxxClient.java 第 N 行」），让你知道**坏的是什么、要不要修**。

---

## 5. 你在整条路线里要做什么

| 阶段 | agent 干 | 你干 |
|---|---|---|
| L0 接线 | 全部 | 进游戏玩一圈，把 `collected.txt` 丢给摆渡 |
| L1 去 Mixin | 全部 | 进游戏验证「聊天栏能不能翻」 |
| L2 多加载器 | 全部 | 决定「要不要做」 |
| L3 多版本 | 全部 + CI | 看红绿、点合并 |

你的成本随层数**下降**，不是上升 —— 这就是「最省命」的定义：
**把适配成本转移到机器能干的活上，你只保留判断权。**

---

## 6. 顺带发现的问题（与方案无关，但要修）

1. **README 死链**：README 引用了 `docs/BRIDGE_PLAN.md` 与 `docs/SPEC_P0P1.md`，
   但这两个文件在 `.gitignore` 里（第 72-74 行），**GitHub 上点开是 404**。
   要么把文件补进仓库，要么把 README 里的链接改成「本地文档」说法。
2. **README 描述过时**：第 59 行说自研桥「冻结着」，但实际已解冻并跑通注入点；
   第 10 行说「桥模组生成器尚未开工」，与本文件的关系应说明。
3. **未推送提交积压**：当前有 7 个本地提交未 push（含 refmap 修复）。
   按你的要求，游戏内验收前不 push —— 这条继续遵守，只提醒别忘。

---

## 7. 明确不做的事

- 不在 L0 闭环前动 L1/L2/L3。（否则得到 9 个全都翻不了的 jar）
- 不把「子串 / 模糊匹配」加进 `TranslationStore` —— 那会啃掉玩家名（v1.8.0 已经吃过亏）。
- 不在玩家电脑上编译模组 / 装 JDK。
- 不在渲染时联网调 AI。
- 不承诺「万能加载器兼容」。
