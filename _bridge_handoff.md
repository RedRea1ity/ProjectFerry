# 桥接问题交接文档（给隔壁 / 给未来的自己）

> 写于 2026-10-04 深夜。状态：两条显示通路（LiteralBridge 聊天线、摆渡桥 tooltip 线）
> 在用户实例「1.20.1-单独仙境吃屎版」上均未生效，已布置对照实验等待游戏内结果。
> 本文档假设读者是另一个 AI 会话或排查者，直接接着干。

## 一、目标

让 The God / boh / tacz 这类模组的**硬编码文本**（聊天、NBT 物品名、Lore）
在游戏里显示中文。数据已就绪：翻译管线跑通了（sidecar `AI_Translation_LowPriority.zip.hardcoded.json`
里有 boh 73 条 / god 34 条 / tacz_project_pneuma 81 条 / anomaly 26 条，质量抽检合格）。

## 二、两条显示通路

### 通路 A：LiteralBridge（社区模组，Losketch 写的，1.2.0 Forge）
- 钩子：ChatComponentMixin（聊天）、EntityMixin（实体名）、GuiMixin（动作栏 setOverlayMessage）、
  PlayerList/ServerPlayer/LocalPlayer。**没有 tooltip 相关 mixin**（已拆 jar 确认）。
- 外部规则：`config/literalbridge/*.json`，WatchService 热重载（日志确认加载成功）。
- TranslationRule 字段（从 1.2.0 jar 常量池解析）：`original`(String) / `translation`(String) /
  `translationKey`(String, @SerializedName alternate="key") / `regex`(Boolean)。
  **1.2.0 没有 exactMatch 字段**（GitHub main 分支才有，默认 true = 整句匹配）。

### 通路 B：自研摆渡桥（bridge/ 目录，冻结中但已构建安装）
- 1.20.1 Forge，ItemTooltipEvent 钩 tooltip，读 `config/ferrybridge/translations.json`
  （`{"map": {英文原文: 中文}}`），剥 § 码后精确匹配。**无热重载，映射随游戏启动加载**。

## 三、已验证的事实

1. LiteralBridge 1.2.0 能加载我们的规则文件（日志 "Loaded external translation rules from ferry.json"）。
2. **子串匹配实锤**：早期导出里 `know→知道` 这条规则把聊天 "he knows" 拼成 "他知道s"；
   玩家名 Br1mstone_0011 也被单字符规则（s/N/0）啃过。→ 已修：导出过滤 <4 字符规则。
3. **锚定正则（^\Q原文\E$ + regex:true）在 1.2.0 上不生效**：规则加载了但整句替换一个没发生
   （连之前能翻的都不翻了）。原因未定：可能 1.2.0 的 regex 字段语义不同、或聊天消息带
   § 码导致整句匹配失败、或 1.2.0 根本没实现正则替换路径。
4. **translationKey 格式未验证**：用户 9 月 11 日手搓的 wonderland_chat_zhcn.json 用的就是
   original + translationKey 格式，但用户从未确认过它在游戏里生效。
5. 翻译数据侧全部就绪且验证过：boh 的 356 字符开场白（在 ScrollofLifePriShchielchkiePKMProcedure.class，
   之前被 160 字符上限挡住）、boh 刀具 Lore（§7"A favorite of killers..."，弯引号 + § 码被
   首字符检查挡住）都已进入候选，检测器四层过滤（§ 剥码 / 长度 400 / 弯引号白名单 / 2 词）
   已放宽并有测试覆盖。

## 四、当前已布置的对照实验（等用户进游戏跑）

`config/literalbridge/_ferrytest.json`（WatchService 会热加载，无需重启）+ 独立语言包
`resourcepacks/FerryBridgeLang.zip`（需在资源包菜单启用一次）。用户在游戏聊天框依次执行：

```
/tellraw @a "FERRYTEST_ALPHA"     ← direct translation 路径
/tellraw @a "FERRYTEST_BETA"      ← translationKey + 资源包语言补丁路径（需启用 FerryBridgeLang）
/tellraw @a "FERRYTEST_GAMMA"     ← regex 标记路径（1.2.0 可能不支持）
/tellraw @a "xx FERRYTEST_DELTA yy"  ← 部分拼接路径
```

判读：哪个显示中文 = 哪种形态在 1.2.0 可用 → 导出器按可用形态出规则。
全不亮 = LiteralBridge 对 tellraw 的纯字面量不钩（换 summon/重命名物品再测）。

tooltip 侧测试：`/give @s minecraft:stick{display:{Name:'{"text":"FERRYTEST5"}'}} 1`
→ 悬停看是否显示「NBT名称测试」（需重启过游戏让摆渡桥加载新映射）。

## 五、已知未解 / 待办

1. 若四种形态全灭：需要反编译 1.2.0 的 TranslationManager 看真实匹配代码
   （常量池显示有 matches×1 / find×2，但 methodref 归属没解析）。
2. god 的聊天句来源未定位：不在 .class 常量（字节级搜过 "watching you" 只在
   Ifcarma100Procedure / ScrollofLife 两个 Procedure 类里，且长句超旧上限），
   也不在 .mcfunction/.json/.txt/.lang。356 字符日志行本身已被旧规则拼接污染
   （结尾 "……s" 是拼接残留），原始文本需要从干净来源确认。
3. 用户 API key 已填（ferry_config.json，gitignored），新候选翻译由用户自己点
   「翻译全部待AI」完成——AI 侧不要代跑。
4. **推送冻结中**：用户明令在游戏验证通过前不许 push（v1.8.4 起 4 个本地提交待推）。
5. 自研摆渡桥（bridge/）保持冻结，若 LiteralBridge 路线彻底不通再解冻。

## 六、给 LiteralBridge 作者的话术（已拟，用户未发）

见 README 致谢区。要点：Apache-2.0 遵守、我们是供数方、问 translation 直填字段
在 1.2.0 的可用性、建议发 Release。
