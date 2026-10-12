package com.redreality.ferrybridge.core;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

/**
 * 桥接自检器：让摆渡桥可以在「只报告、不注入」模式下运行。
 *
 * <p><b>为什么需要这个类</b>
 *
 * <p>2026-10-10 连续三次把游戏改崩，每次都是同一种失败模式：
 * 替换路径在加载阶段出问题（当时是 Mixin 注入点被静默丢弃，或直接 FATAL），
 * 而玩家只能靠崩溃报告反推原因。核心问题是——<b>「替换到底挂上没有」没有"活着"的反馈</b>：
 *
 * <ul>
 *   <li>当时 Mixin 被丢弃时，日志只有一条 ERROR，游戏照常启动，玩家看到"部分生效"，
 *       完全无从判断是"映射表不够"还是"路径根本没挂上"；</li>
 *   <li>FATAL 时游戏开不起来，代价是玩家的整个游戏时间。</li>
 * </ul>
 *
 * <p>2026-10-12（L1）起，替换路径已全部改成 Forge 官方事件，本模组零 Mixin：
 * 「事件有没有注册上」比「字节码注入有没有命中」可靠得多。但"到底生效没有"
 * 仍然需要反馈，所以本类的安全模式与诊断报告继续保留这个职责。
 *
 * <p>本类提供两个能力：
 *
 * <ol>
 *   <li><b>安全模式</b>：把 {@code enabled} 设为 false，所有注入点变成
 *       空操作（只留日志），游戏保证能启动。此时用来验证"映射表加载正常吗"、
 *       "版本对不对"，而不用拿崩溃试错。</li>
 *   <li><b>诊断报告</b>：无论开关如何，桥加载时都会把关键状态写进日志，
 *       并在游戏目录下生成一份 {@code ferrybridge-diagnostic.txt}，
 *       内容是人话（见 {@link #buildReport}）。</li>
 * </ol>
 *
 * <p><b>这个类不引用任何 Minecraft 类与加载器 API</b>，与 TranslationStore
 * 同属可跨加载器复用的内核。
 */
public final class FerryBridgeDiag {

    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    /** 配置文件名。放在游戏目录下，与 forge/fabric 各自的 config 目录无关。 */
    public static final String CONFIG_NAME = "ferrybridge.json";

    /** 诊断报告文件名。 */
    public static final String REPORT_NAME = "ferrybridge-diagnostic.txt";

    /** 安全模式标记：注入点查到后直接返回，不做替换。 */
    private static volatile boolean injectionEnabled = true;

    private FerryBridgeDiag() {
    }

    /**
     * 读配置决定是否注入。
     *
     * <p><b>缺省为 true（注入）</b>——配置文件不存在时保持原行为，
     * 不因为加了这个类就改变既有玩家体验。安全模式需要显式写
     * {@code "enabled": false}，或用 {@code -Dferrybridge.safe=true} 启动。
     *
     * @param gameDir 游戏目录（由加载器入口传入）
     * @return 最终是否启用注入
     */
    public static boolean loadConfig(Path gameDir) {
        // 系统属性优先级最高：便于启动器里临时加参数做安全排查，
        // 不用改文件、不用进游戏再退出来改。
        String sysProp = System.getProperty("ferrybridge.safe");
        if (sysProp != null) {
            boolean safe = Boolean.parseBoolean(sysProp.trim());
            injectionEnabled = !safe;
            LOGGER.info("FerryBridge safety switch from -Dferrybridge.safe={} -> injection {}",
                    sysProp, injectionEnabled ? "ON" : "OFF (safe mode)");
            return injectionEnabled;
        }

        Path configPath = gameDir.resolve(CONFIG_NAME);
        if (!Files.exists(configPath)) {
            // 不写文件、不打扰玩家：只在日志里说明即可。
            injectionEnabled = true;
            LOGGER.info("FerryBridge no {} found, injection enabled (default)", CONFIG_NAME);
            return injectionEnabled;
        }

        try {
            String text = new String(Files.readAllBytes(configPath), StandardCharsets.UTF_8);
            // 极简解析：只找 "enabled" 字段，不引入 JSON 库依赖。
            // 保持"标准库即可运行"的既有约定（requirements.txt 无第三方依赖）。
            boolean enabled = parseBooleanField(text, "enabled", true);
            injectionEnabled = enabled;
            LOGGER.info("FerryBridge config {} -> injection {}", configPath, enabled ? "ON" : "OFF (safe mode)");
            return injectionEnabled;
        } catch (IOException ex) {
            // 配置读不了就退回默认，绝不让游戏开不起来。
            LOGGER.warn("FerryBridge failed to read {}, keep injection enabled", configPath, ex);
            injectionEnabled = true;
            return injectionEnabled;
        }
    }

    /** 注入点是否应该真的做替换。注入点在方法开头调用它。 */
    public static boolean isInjectionEnabled() {
        return injectionEnabled;
    }

    /**
     * 产出人话诊断报告。
     *
     * <p>刻意用中文写清楚"哪个环节断了"，因为使用者（牢红）看不懂 Java 堆栈，
     * 需要的是「坏了什么、要不要修」而不是 200 行 stacktrace。
     *
     * <p><b>本报告只在启动时写一次</b>，是「进入游戏那一刻的快照」。
     * 实时状态请看 {@code ferrybridge-status.txt}（随变化即时刷新）或按 F9 开 HUD。
     */
    public static String buildReport(Path gameDir, TranslationStore store, String loadResult) {
        StringBuilder sb = new StringBuilder();
        String bar = "=".repeat(60);

        sb.append(bar).append('\n');
        sb.append("摆渡桥FerryBridge 诊断报告\n");
        sb.append("生成时间: ").append(java.time.LocalDateTime.now()).append('\n');
        sb.append(bar).append('\n').append('\n');

        sb.append("【一句话结论】\n");
        int entries = store == null ? 0 : store.size();
        int live = FerryBridgeLive.getInjectorLive();
        if (!injectionEnabled) {
            sb.append("  当前是【安全模式】：桥已加载但不替换任何文本，游戏保证能启动。\n");
            sb.append("  这是排查模式，不是故障。\n");
        } else if (entries == 0) {
            sb.append("  映射表是空的 → 桥无法翻译任何东西。\n");
            sb.append("  要修：用摆渡计划重新导出桥接 jar。\n");
        } else if (live == 0) {
            // 关键：区分「路径没挂上」与「还没触发过」——
            // 后者是正常情况，不能报成故障，否则会误导人去查不存在的 bug。
            sb.append("  映射表有 ").append(entries).append(" 条；两条替换路径已随模组正常加载。\n");
            sb.append("  「生效路径 0」只说明【本次启动还没收到过聊天 / 悬停过物品】，不是故障。\n");
            sb.append("  要验证：进游戏打一句话或悬停一个物品，再看实时状态\n");
            sb.append("  （ferrybridge-status.txt，或按 F9 开 HUD）。本报告是启动快照，不会自己更新。\n");
        } else {
            sb.append("  映射表 ").append(entries).append(" 条，").append(live)
              .append(" 条替换路径已生效 → 桥在正常工作。\n");
            sb.append("  若某些文本仍是英文，多半是它们不在映射表里（属正常，属覆盖范围问题）。\n");
        }

        sb.append('\n').append("【细节】\n");
        sb.append("  映射表加载结果: ").append(loadResult).append('\n');
        sb.append("  映射条数      : ").append(entries)
          .append("（内置 ").append(store == null ? 0 : store.jarSize())
          .append(" + 外部 ").append(store == null ? 0 : store.externalSize()).append("）").append('\n');
        sb.append("  注入开关      : ").append(injectionEnabled ? "开启（正常替换）" : "关闭（安全模式）").append('\n');
        sb.append("  生效路径      : ").append(live)
          .append(live == 0 && entries > 0 ? "（尚未触发，非故障）" : "").append('\n');
        sb.append("  内置表路径    : ").append(TranslationStore.RESOURCE_PATH).append('\n');
        sb.append("  外部表路径    : ").append(describeExternalPath(store)).append('\n');
        sb.append("  重启游戏可刷新本报告 → ").append(REPORT_NAME).append('\n');

        sb.append('\n').append("【生效范围】（都走 Forge 官方事件，本模组零 Mixin）\n");
        sb.append("  聊天栏        : 已挂 ClientChatReceivedEvent")
          .append(live > 0 ? "（本次启动已触发过）" : "（打一句话即触发，未触发不是故障）").append('\n');
        sb.append("  物品tooltip  : 已挂 ItemTooltipEvent（悬停物品才触发）\n");

        sb.append('\n').append("【批量翻译数据】\n");
        if (FerryBridgeCollector.isEnabled()) {
            sb.append("  采集器        : 开启 → 未命中的英文记进 ")
              .append(FerryBridgeCollector.FILE_NAME).append('\n');
            sb.append("  已采集        : ").append(FerryBridgeCollector.size())
              .append(" 条（玩游戏时实时增长）\n");
            sb.append("  用法          : 把该文件交给摆渡批量翻译，产出 ")
              .append(TranslationStore.EXTERNAL_NAME)
              .append(" 即热重载生效（不用重启游戏）。\n");
        } else {
            sb.append("  采集器        : 关闭（启动参数 -Dferrybridge.collect=false）\n");
        }

        sb.append('\n').append("【安全模式怎么开】\n");
        sb.append("  方式一：在游戏目录建 ").append(CONFIG_NAME).append(' ')
          .append("内容写 {\"enabled\": false}\n");
        sb.append("  方式二：启动参数加 -Dferrybridge.safe=true（临时，最方便）\n");
        sb.append("  两种方式下游戏都保证能启动，可放心排查。\n");

        String report = sb.toString();
        try {
            Files.write(gameDir.resolve(REPORT_NAME), report.getBytes(StandardCharsets.UTF_8));
        } catch (IOException ex) {
            LOGGER.warn("FerryBridge failed to write diagnostic report", ex);
        }
        return report;
    }

    /**
     * 外部映射表路径的描述，供诊断报告展示。未配置时返回「未配置」。
     */
    private static String describeExternalPath(TranslationStore store) {
        if (store == null || store.getExternalPath() == null) {
            return "未配置";
        }
        return store.getExternalPath().toString();
    }

    /**
     * 从 JSON 文本里取一个 boolean 字段。
     *
     * <p>手写而非用库：整个项目对外承诺"标准库即可运行"，这里不引入新的依赖。
     * 支持 {@code "enabled": false} 与 {@code "enabled": false} 两种写法。
     */
    private static boolean parseBooleanField(String json, String field, boolean fallback) {
        int idx = json.indexOf('"' + field + '"');
        if (idx < 0) {
            return fallback;
        }
        int colon = json.indexOf(':', idx);
        if (colon < 0) {
            return fallback;
        }
        // 跳过冒号后的空白与引号
        int i = colon + 1;
        while (i < json.length() && Character.isWhitespace(json.charAt(i))) {
            i++;
        }
        if (i < json.length() && json.charAt(i) == '"') {
            i++;
        }
        while (i < json.length() && Character.isWhitespace(json.charAt(i))) {
            i++;
        }
        return json.startsWith("true", i);
    }
}
