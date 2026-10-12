package com.redreality.ferrybridge.core;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 桥接实时状态中心：让玩家在游戏内就能看到"桥现在到底在干什么"。
 *
 * <p><b>为什么要有这个类</b>
 *
 * <p>2026-10-10的教训：诊断信息写进日志文件后，必须<b>重启游戏才能刷新</b>，
 * 于是「改了代码 → 重启 → 看报告 → 再改」这个循环每次都要几分钟，
 * 排查效率极低。而注入点是否生效、命中了多少次这些信息，
 * 完全是<b>运行时状态</b>，本该实时可见。
 *
 * <p>本类把状态拆成可实时读取的原子计数，并提供三个出口：
 * <ul>
 *   <li><b>游戏内 HUD</b>：按 {@code F9} 切换浮层（Forge 侧渲染），即时显示；</li>
 *   <li><b>状态文件</b>：每次状态变化即写盘，比日志更实时，且随时可查；</li>
 *   <li><b>诊断包</b>：{@code F9} 顺带导出（见 {@link FerryBridgeReport}），
 *       给「要把问题反馈给作者」用。</li>
 * </ul>
 *
 * <p>本类<b>不引用任何 Minecraft / 加载器类</b>，保持可跨加载器复用。
 */
public final class FerryBridgeLive {

    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    /** 状态文件名。写入频繁（仅在数值变化时），故单独放一份便于排查。 */
    public static final String STATUS_NAME = "ferrybridge-status.txt";

    // ── 实时计数（volatile，读写开销可忽略，够用且简单）────────────────
    // injectorLive 是"当前有多少条**不同**的替换路径被证实活着"，
    // 不是调用次数 —— tooltip 事件每帧都可能触发，若按次数计会瞬间上万，
    // 完全失去意义。所以这里用名字集合去重。
    private static final java.util.Set<String> liveInjectors =
            java.util.concurrent.ConcurrentHashMap.newKeySet();
    private static volatile int hitCount = 0;
    private static volatile int missCount = 0;
    private static volatile boolean injectionOn = true;
    private static volatile int mappingCount = 0;
    private static volatile String lastHit = "";
    private static volatile String lastMiss = "";
    private static volatile String lastReport = "";
    private static volatile long lastUpdateNanos = System.nanoTime();

    private FerryBridgeLive() {
    }

    /** 初始化：桥加载完成后调用一次，把基础状态定下来。 */
    public static void init(int mappings, boolean injectionEnabled) {
        mappingCount = mappings;
        injectionOn = injectionEnabled;
        touch();
    }

    /**
     * 替换路径真正执行时回报。这是"路径活着"的可靠信号。
     *
     * <p>按**名字去重**：同一个路径被调用一万次，也只算 1 条生效路径。
     * 否则 tooltip 这类每帧触发的事件会把计数撑爆，看不出"到底有几条路径是活的"。
     */
    public static void markInjectorLive(String name) {
        if (name == null) {
            return;
        }
        if (liveInjectors.add(name)) {
            LOGGER.info("FerryBridge injector live: {}", name);
            touch();
        }
    }

    /** 当前已被证实活着的替换路径条数（去重后）。 */
    public static int getInjectorLive() {
        return liveInjectors.size();
    }

    /** 查表命中一次（成功替换）。 */
    public static void recordHit(String english, String chinese) {
        hitCount++;
        // 只留前 80 字符，防止超长文本撑爆 HUD
        String e = english == null ? "" : english;
        String c = chinese == null ? "" : chinese;
        if (e.length() > 60) {
            e = e.substring(0, 60) + "…";
        }
        if (c.length() > 60) {
            c = c.substring(0, 60) + "…";
        }
        lastHit = "「" + e + "」→「" + c + "」";
        touch();
    }

    /** 查表未命中一次。命中为 0 且miss 巨大，说明映射表没覆盖到实际内容。 */
    public static void recordMiss() {
        missCount++;
        touch();
    }

    /** 切换注入开关（游戏内按键用）。 */
    public static void setInjectionOn(boolean on) {
        injectionOn = on;
        touch();
    }

    public static boolean isInjectionOn() {
        return injectionOn;
    }

    public static int getHitCount() {
        return hitCount;
    }

    public static int getMissCount() {
        return missCount;
    }

    public static int getMappingCount() {
        return mappingCount;
    }

    public static String getLastHit() {
        return lastHit;
    }

    /**
     * 记下最近一次导出的诊断包文件名。
     *
     * <p>光把文件写出去还不够 —— 玩家得知道它叫什么、在哪。写进 HUD 与状态文件后，
     * 「按 F9 导出，然后找那个文件」这句话才有落点。
     */
    public static void setLastReport(String name) {
        lastReport = name == null ? "" : name;
        touch();
    }

    /** 最近一次导出的诊断包文件名；没导出过则为空串。 */
    public static String getLastReport() {
        return lastReport;
    }

    /** 距离上次状态变化过去了多少毫秒（用于判断"是不是卡住了"）。 */
    public static long millisSinceUpdate() {
        return (System.nanoTime() - lastUpdateNanos) / 1_000_000L;
    }

    private static void touch() {
        lastUpdateNanos = System.nanoTime();
    }

    /**
     * 生成 HUD 要显示的几行文本。
     *
     * @return 形如"行/行"的字符串数组，每行一行
     */
    public static String[] hudLines() {
        return new String[] {
            "§6摆渡桥 FerryBridge §7（实时状态，F9 关闭并导出诊断包）",
            "§f映射表  §e" + mappingCount + " §7条",
            "§f注入开关§e " + (injectionOn ? "§a开启（正常替换）" : "§c关闭（安全模式）"),
            "§f生效路径  §e" + getInjectorLive() + " §7条（去重）",
            "§f已替换  §a" + hitCount + " §7次",
            "§f未命中  §7" + missCount + " §7次",
            "§f已采集  §b" + FerryBridgeCollector.size() + " §7条 §8(批量翻译原料)",
            "§f最近命中§7 " + (lastHit.isEmpty() ? "§8（还没有）" : lastHit),
            "§f诊断包  §7" + (lastReport.isEmpty()
                    ? "§8按 F9 导出（在游戏目录里）" : "§b" + lastReport),
            "§8" + describeState(),
        };
    }

    /** 一句话状态，供 HUD 底部与状态文件共用。 */
    public static String describeState() {
        if (!injectionOn) {
            return "安全模式：不替换任何文本，游戏正常";
        }
        if (mappingCount == 0) {
            return "映射表为空 → 无法翻译";
        }
        if (hitCount > 0) {
            return "正常工作，已替换 " + hitCount + " 次";
        }
        if (getInjectorLive() == 0) {
            return "替换路径尚未触发 → 打一句话 / 悬停物品试试";
        }
        int collected = FerryBridgeCollector.size();
        if (collected > 0) {
            return "注入点在跑，已采集 " + collected + " 条待翻（表里还没有）";
        }
        return "已加载，等文本出现（悬停物品看 tooltip 试试）";
    }

    /**
     * 写实时状态文件。
     *
     * <p>刻意写得比日志更频繁：只要状态有变化就写，这样随时打开都是最新的，
     * 不用重启游戏。写失败静默忽略——状态文件只是辅助，绝不能影响游戏。
     */
    public static void writeStatusFile(Path gameDir) {
        if (gameDir == null) {
            return;
        }
        try {
            StringBuilder sb = new StringBuilder();
            sb.append("摆渡桥 FerryBridge 实时状态\n");
            sb.append("更新时间: ").append(java.time.LocalDateTime.now()).append('\n');
            sb.append("（本文件随状态变化即时刷新，无需重启游戏）\n\n");
            for (String line : hudLines()) {
                // 去掉颜色代码，写文件要可读
                sb.append(stripColor(line)).append('\n');
            }
            Files.write(gameDir.resolve(STATUS_NAME), sb.toString().getBytes(StandardCharsets.UTF_8));
        } catch (IOException ex) {
            LOGGER.warn("FerryBridge failed to write status file", ex);
        }
    }

    /** 去掉 § 颜色代码，得到纯文本。 */
    private static String stripColor(String s) {
        return s.replaceAll("\u00a7.", "");
    }
}
