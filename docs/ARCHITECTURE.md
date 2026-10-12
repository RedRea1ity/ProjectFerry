# 摆渡计划 · 代码结构

> 这份文档是给「将来接手的人（包括你自己）」看的：**东西在哪、为什么这么分、改的时候别踩什么坑**。
> 2026-10-12 这一轮把 `mc_ai_translator.py` 从 **3809 行拆到 57 行**，实现全部下沉到 `ferrycore/`。

## 一、现在的样子

| 文件 | 行数 | 角色 |
|---|---|---|
| `mc_ai_translator.py` | ~57 | **薄壳**：把 `ferrycore` 的名字重新导出 + 把命令行转交 `ferrycore.cli` |
| `ProjectFerry.pyw` | ~3570 | tkinter GUI（单类 `FerryApp`） |
| `ferrycore/` | 18 个模块 | 真正的实现，按职责分层 |

**对外 API 没有变**：GUI 和测试仍然 `import mc_ai_translator as core`，
`core.xxx` 全部照旧 —— 薄壳里刻意保留「全量重新导出」（含 `_` 开头的私有名），
因为调用面太广，少导一个就是运行期崩。想找某个功能，**先去 `ferrycore/` 按模块名找**。

## 二、模块分层（依赖单向，下层不认识上层）

```
                       cli.py            命令行入口：argparse + command_*
                         │
        ┌────────────────┼────────────────┬───────────────┐
        ▼                ▼                ▼               ▼
    packio.py        online.py      literalbridge.py  community.py
  资源包读写/卸载   在线汉化查询    LiteralBridge 管理   CFPA 整合包 / 人工包
        │                │                │               │
        │                └────────┬───────┴───────────────┘
        ▼                         ▼
    engine.py                 update.py           ← 自更新（GitHub Release）
  翻译引擎 / 批次 / 重试
        │                         │
        ▼                         ▼
    hardcoded.py   bridge.py   net.py              ← 硬编码检测 / 桥映射 / HTTP
        │              │         │
        └──────┬───────┴─────────┘
               ▼
           scan.py            ← 实例发现、语言文件读取、缺失 key、模组状态
               │
        ┌──────┼─────────┬──────────────┐
        ▼      ▼         ▼              ▼
   glossary.py versions.py config.py  text.py
   术语表      版本/格式    配置/进度   解析/掩码/提示词/分批
        └──────┴─────────┴──────────────┘
               ▼
        models.py   secrets.py   base.py     ← 数据结构 / 密钥保护 / 日志与路径
```

| 模块 | 行数 | 收容什么 |
|---|---|---|
| `base` | 92 | 可写目录 `app_dir()`、日志 + 脱敏过滤器、`atomic_write_text`（零内部依赖） |
| `models` | 124 | `Entry` / `LangFile` / `PatchouliFile` / `ScanResult` / `ModTranslationStatus` + 状态标签 |
| `text` | 310 | 语言文件解析、占位符与术语掩码、提示词、`cache_key`、分批 |
| `secrets` | 172 | 密钥本地保护（Windows DPAPI 落盘加密） |
| `net` | 80 | HTTP GET / POST JSON、统一错误文案 |
| `config` | 200 | `DEFAULT_CONFIG`、配置读写（经 secrets 加密）、进度记录、git 忽略检测 |
| `versions` | 403 | 版本 ↔ 资源包格式（本地表 / 远程表 / 客户端 jar 探测）、`version_key` |
| `glossary` | 136 | 内置原版名词、官方术语下载、用户术语、批次术语提示 |
| `scan` | 507 | 实例发现、语言文件 / Patchouli 读取、缺失 key 计算、模组状态构建 |
| `hardcoded` | 383 | 扒 jar 的 class 常量池，挑玩家可见文本（`looks_like_hardcoded_text`） |
| `bridge` | 234 | 摆渡桥 / LiteralBridge 映射表构建导出、游戏内采集文件解析 |
| `update` | 93 | 查 GitHub 最新 Release、下载附件 |
| `community` | 197 | CFPA 整合包下载 / 解析、人工汉化包下载、参考语料 |
| `literalbridge` | 417 | LiteralBridge 探测已装版本、下载、自动升级 |
| `online` | 65 | 在线汉化查询 |
| `engine` | 448 | 各翻译引擎 API、批次翻译、重试、用量统计 |
| `packio` | 513 | 资源包生成 / 卸载 / 还原 / 让位，锁文件与质量检查 |
| `cli` | 361 | `argparse` 与各 `command_*` |

## 三、怎么拆的

用 `tools/split_module.py`：**按 AST 行区间整体搬运**，被搬的代码逐字不变，
不做任何语义改写；搬完自动生成新模块的 import 头、从原文件删除、在薄壳里
补上重新导出。命令形如：

```bash
python tools/split_module.py analyze                        # 看各符号的相互引用
python tools/split_module.py move <模块名> <符号1> <符号2> ...
```

搬运前会**校验依赖**：被搬符号引用到的顶层符号，必须一起搬、或已经在
`ferrycore` 某个模块里 —— 否则直接拒绝（那会形成循环 import）。

## 四、拆的时候必须知道的两件事

### 1. `mock.patch.object(core, "X")` 会静默失效

测试用 `mock.patch.object(core, "某函数")` 拦截内部调用。**一旦「被 patch 的函数」
和「调用它的函数」被拆到不同模块，patch 就不生效了** —— 模块 A 里的函数不会去读
`mc_ai_translator` 上的同名属性。

判断规则：**看调用者在哪**。调用者搬到 `ferrycore/xxx.py` 之后，patch 目标就得是
`ferrycore.xxx.那个名字`（尤其注意「同一个函数被不同模块 import 之后，各有一份引用」，
例如 `download_release_asset` 在 `community` 和 `literalbridge` 里各是一份）。

这一轮改过的 patch 目标（测试里已同步）：

| 被 patch 的 | 改成 patch |
|---|---|
| `is_git_ignored` | `ferrycore.config` |
| `_fetch_json`（版本表） | `ferrycore.versions` |
| `_fetch_json`（自更新） | `ferrycore.update` |
| `literalbridge_releases` / `_retire_old_literalbridge` / `download_release_asset`（下载桥） | `ferrycore.literalbridge` |
| `check_online_translation` | `ferrycore.online` |
| `_http_post_json` / `_translate_batch_with_retry` / `translate_batch` | `ferrycore.engine` |
| `_write_pack_file` / `load_locks` / `translate_entries`（重翻） | `ferrycore.packio` |
| `load_config` / `translate_entries`（CLI 翻译）/ `update_progress` | `ferrycore.cli` |

注意最后一行：`translate_entries` 在 `packio` 和 `cli` 里**各有一份引用**，patch 哪一份
取决于调用者是谁 —— 这是同一个符号多处引用最容易踩的地方。

**漏改的后果不只是测试变红。** `test_spec_p0.py` 里那两个 CLI 测试原本 patch 的是薄壳
`core.load_config`，而 `command_translate` 走的是 `ferrycore.cli` 里 import 的名字 ——
于是测试**真的读了本机的 `ferry_config.json`**（并触发密钥迁移、写回了磁盘），**还真的
打了 MyMemory 的网络请求**。跑测试会污染开发者的真实配置，这是 2026-10-12 实测抓到的
（见下面第三条第 3 点）。所以：**测试里任何 patch 外部依赖的地方，都要按「调用者在哪」核对一遍。**

测试是资产不是圣物，**改测试是允许的** —— 但改完必须全绿。

### 2. 每拆一步就跑测试

```bash
python -m unittest test_refine test_spec_p0     # 99 项，必须全绿
python -m py_compile ProjectFerry.pyw mc_ai_translator.py ferrycore/*.py
```

再顺手验一遍 GUI 的调用面（防止漏导出）：

```bash
python -c "import mc_ai_translator as core; print(len(dir(core)))"
```

## 五、这一轮顺手修掉的三个真缺陷

1. **`app_dir()` 目录跑偏**（平移 `base.py` 时踩到，已修）：
   它原来靠 `Path(__file__).parent` 定位仓库根；文件搬进 `ferrycore/` 之后必须
   `.parent.parent`，否则配置会写到 `ferrycore/ferry_config.json`。

2. **`_version_key` 被定义了两次**（拆 `update` / `literalbridge` 时才暴露）：
   同一个名字在模块里出现两次，后者**静默覆盖**前者 —— 也就是 `latest_github_release`
   一直在用一个它「没打算用」的实现。拆分会把这两处分到不同模块，行为就分叉了。
   现在合并成 `ferrycore.versions.version_key`（唯一口径），全项目共用一份。

3. **测试污染真实配置**（2026-10-12 实测抓到）：见第四节第 1 条的说明 —— 两个 CLI 测试
   patch 错了目标，导致跑 `unittest` 会读真实 `ferry_config.json`、写回磁盘、并打真实
   网络请求。已把 patch 目标改成 `ferrycore.cli`，并用「明文探针」实验验证修复有效
   （把真实配置临时换成一份已知明文，跑完测试后它仍是明文 = 没被碰）。

## 六、还剩什么

- **`ProjectFerry.pyw`（3570 行）**：GUI 还没拆。它的 UI 结构本身要重做，
  等 UI 重构那一轮一起拆，别现在动。
- 剩下两个大文件在 `ferrycore/` 里也算不上小（`packio` 513、`scan` 507），
  但**它们已经各自是单一职责**了，除非继续长，不必再切。
