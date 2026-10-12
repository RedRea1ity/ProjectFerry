"""摆渡计划的内核包。

`mc_ai_translator.py` 只负责把这里的东西重新导出（`core.xxx` 这个对外 API 入口），
真正的实现在这里，按职责分层 —— 依赖单向，下层不认识上层。

完整布局、依赖方向与拆分约定见 `docs/ARCHITECTURE.md`。

模块一览（自底向上）：

- `base`          可写目录 / 日志 / 脱敏 / `atomic_write_text`（零内部依赖）
- `models`        Entry / LangFile / PatchouliFile / ScanResult / ModTranslationStatus
- `text`          语言文件解析、占位符与术语掩码、提示词、cache_key、分批
- `secrets`       密钥本地保护（Windows DPAPI 落盘加密）
- `net`           HTTP GET / POST JSON、统一错误文案
- `config`        DEFAULT_CONFIG、配置读写（经 secrets 加密）、进度记录
- `versions`      版本 ↔ 资源包格式、客户端 jar 探测、`version_key`
- `glossary`      内置原版名词、官方术语下载、用户术语、术语提示
- `scan`          实例发现、语言文件 / Patchouli 读取、缺失 key、模组状态
- `hardcoded`     class 常量池硬编码文本检测
- `bridge`        摆渡桥 / LiteralBridge 映射表构建导出、采集文件解析
- `update`        自更新（GitHub Release）
- `community`     CFPA 整合包 / 人工汉化包、参考语料
- `literalbridge` LiteralBridge 探测 / 下载 / 自动升级
- `online`        在线汉化查询
- `engine`        翻译引擎、批次翻译、重试、用量统计
- `packio`        资源包生成 / 卸载 / 还原 / 让位、锁文件、质量检查
- `cli`           命令行入口
"""
