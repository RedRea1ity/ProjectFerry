package com.redreality.ferrybridge.core;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;

/**
 * 未命中文本采集器：把「游戏里出现过、但当前映射表没有对应译文」的英文记下来。
 *
 * <h2>为什么必须有这个类</h2>
 *
 * <p>2026-10-12 的症结：桥的注入点早就活了（HUD 显示生效注入点 ≥1），
 * 但「已替换 0 / 未命中 N」——因为映射表里根本没有那些文本。
 * 想让摆渡做到「大批量翻译 tooltip 和聊天栏」，光在游戏里等着是没用的，
 * 必须先<b>知道要翻什么</b>。
 *
 * <p>于是有了这个采集器：桥在查表未命中时，把原文丢进这里。
 * 玩家正常玩一圈，游戏目录下就多出一份 {@value #FILE_NAME}，
 * 里面按来源分好组（聊天栏 / 物品 tooltip），去重且过滤过噪音。
 * 这份文件就是摆渡批量翻译的**输入**，译完写进
 * {@link TranslationStore#EXTERNAL_NAME} 即可热重载生效。
 *
 * <h2>它不会做的事（红线）</h2>
 *
 * <p>它<b>只记录，不翻译，不改游戏行为</b>。采集到的文本不会自动进映射表——
 * 玩家自己敲进聊天框的话也会被记下来（因为对桥来说它就是一条"未命中的文本"），
 * 但它永远不会被翻译，除非有人真的把它写进表里。这正是
 * {@link TranslationStore} 那条「只做整条精确匹配」红线在起作用。
 *
 * <p>不引用任何 Minecraft / 加载器类，与其他 core 类一样可跨加载器复用。
 */
public final class FerryBridgeCollector {

    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    /** 采集文件名，位于游戏目录下。 */
    public static final String FILE_NAME = "ferrybridge-collected.txt";

    /** 每个来源最多记多少条，防止长时间挂机把文件撑成天文数字。 */
    private static final int MAX_PER_BUCKET = 4000;

    /** 少于这个字符数的文本不采（`OK` 这种太短，翻不翻都行，且噪音大）。 */
    private static final int MIN_LENGTH = 3;

    /** 写盘节流：最多两秒写一次。避免 tooltip 高频触发时反复读写整个文件。 */
    private static final long FLUSH_INTERVAL_MS = 2000L;

    /**
     * § 格式码。写盘前剥掉，理由：扫描端（摆渡 Python）从 class 常量取原文时也剥码，
     * 两侧口径必须一致，否则同一句话会在映射表里生成「带码 / 不带码」两份 key；
     * 而且 § 码对翻译引擎是纯噪声，会污染译文。
     */
    private static final java.util.regex.Pattern FORMAT_CODES = java.util.regex.Pattern.compile("\u00a7.");

    /** 来源分组名。用固定顺序输出，方便人读。 */
    public static final String BUCKET_CHAT = "聊天栏";
    public static final String BUCKET_TOOLTIP = "物品 tooltip";

    private static volatile boolean enabled = true;
    private static volatile Path outputPath;

    /** 分桶存储，桶内去重。用并发集合，因为会从渲染线程和网络线程同时写。 */
    private static final Map<String, Set<String>> buckets = new ConcurrentHashMap<>();
    private static volatile boolean dirty = false;
    private static volatile int totalCount = 0;

    private FerryBridgeCollector() {
    }

    /**
     * 初始化：定好输出路径、按需关闭、启动后台写盘线程。
     *
     * <p>默认<b>开启</b>：它是「大批量翻译」唯一的自动数据来源，
     * 而开关一旦默认关着，就等于没有。不想要可以启动参数
     * {@code -Dferrybridge.collect=false}。
     */
    public static void init(Path gameDir) {
        if (gameDir == null) {
            return;
        }
        outputPath = gameDir.resolve(FILE_NAME);

        String prop = System.getProperty("ferrybridge.collect");
        if (prop != null) {
            enabled = Boolean.parseBoolean(prop.trim());
        }
        LOGGER.info("FerryBridge collector {} -> {}", enabled ? "ON" : "OFF", outputPath);

        if (!enabled) {
            return;
        }
        Thread flusher = new Thread(() -> {
            while (!Thread.currentThread().isInterrupted()) {
                try {
                    Thread.sleep(FLUSH_INTERVAL_MS);
                } catch (InterruptedException ie) {
                    return;
                }
                if (dirty) {
                    dirty = false;
                    flush();
                }
            }
        }, "FerryBridge-Collector");
        flusher.setDaemon(true);
        flusher.start();
    }

    public static boolean isEnabled() {
        return enabled;
    }

    /** 当前已采集的总条数（去重后）。 */
    public static int size() {
        return totalCount;
    }

    /** 采集文件路径，可能为 null（未初始化）。 */
    public static Path getOutputPath() {
        return outputPath;
    }

    /**
     * 记录一条未命中的文本。
     *
     * <p>热路径：绝大多数调用会在 {@link #isInteresting} 里被挡掉（纯数字、
     * 纯符号、已含中文、过短），只有真正像英文句子的才会进集合。
     *
     * @param bucket 来源分组，见 {@link #BUCKET_CHAT} / {@link #BUCKET_TOOLTIP}
     * @param text   原文（带不带 § 都行，写盘前会剥掉）
     */
    public static void record(String bucket, String text) {
        if (!enabled || outputPath == null || bucket == null) {
            return;
        }
        if (!isInteresting(text)) {
            return;
        }
        Set<String> set = buckets.computeIfAbsent(bucket, k -> ConcurrentHashMap.newKeySet());
        if (set.size() >= MAX_PER_BUCKET) {
            return;
        }
        if (set.add(text)) {
            totalCount++;
            dirty = true;
        }
    }

    /**
     * 判断一条文本值不值得采集。
     *
     * <p>过滤规则（宁可少采，不要采一堆噪音回来）：
     * <ul>
     *   <li>太短的不要——`OK`、`+`、`x3` 之类</li>
     *   <li>不含 ASCII 字母的不要——纯数字、纯符号、纯中文</li>
     *   <li>已含 CJK 汉字的不要——说明它本来就是中文，不需要翻</li>
     * </ul>
     */
    private static boolean isInteresting(String text) {
        if (text == null) {
            return false;
        }
        String trimmed = text.trim();
        if (trimmed.length() < MIN_LENGTH) {
            return false;
        }
        boolean hasLatinLetter = false;
        for (int i = 0; i < trimmed.length(); i++) {
            char c = trimmed.charAt(i);
            if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) {
                hasLatinLetter = true;
            } else if (c >= 0x4E00 && c <= 0x9FFF) {
                // 含汉字 → 已经是中文，不是我们要找的英文原文。
                return false;
            }
        }
        return hasLatinLetter;
    }

    /**
     * 把当前采集结果写盘。
     *
     * <p>写成<b>人可读</b>的文本而不是 JSON：这份文件是要给人看的——
     * 牢红要能自己打开、扫一眼、甚至手改几条；而且它只是"待翻译清单"，
     * 摆渡真正产出的映射表是另一个 JSON 文件，两者分工明确。
     *
     * <p>写失败静默忽略：采集只是辅助功能，不能影响游戏。
     */
    private static void flush() {
        Path path = outputPath;
        if (path == null) {
            return;
        }
        try {
            StringBuilder sb = new StringBuilder();
            sb.append("摆渡桥 FerryBridge · 未命中文本采集\n");
            sb.append("更新时间: ").append(java.time.LocalDateTime.now()).append('\n');
            sb.append('\n');
            sb.append("【这是什么】\n");
            sb.append("  游戏里出现过、但当前映射表没有对应译文的英文。\n");
            sb.append("  按来源分好组，已去重。它是摆渡「批量翻译」的输入清单。\n");
            sb.append('\n');
            sb.append("【怎么用】\n");
            sb.append("  把下面的文本交给摆渡批量翻译，产出 ferrybridge-mappings.json\n");
            sb.append("  放进游戏目录，桥会热重载生效，不用重启游戏。\n");
            sb.append('\n');
            sb.append("【说明】\n");
            sb.append("  本文件只记录，不会自动翻译。玩家自己敲的话也可能被记下来，\n");
            sb.append("  但它永远不会被替换——映射表只做整条精确匹配，\n");
            sb.append("  没人把它写进表里，它就一直是原文。\n");
            sb.append('\n');
            sb.append("共 ").append(totalCount).append(" 条\n");

            // 固定顺序：先聊天栏，再 tooltip，最后其他（如果有）。
            appendBucket(sb, BUCKET_CHAT);
            appendBucket(sb, BUCKET_TOOLTIP);
            for (String key : buckets.keySet()) {
                if (!BUCKET_CHAT.equals(key) && !BUCKET_TOOLTIP.equals(key)) {
                    appendBucket(sb, key);
                }
            }

            Files.write(path, sb.toString().getBytes(StandardCharsets.UTF_8));
        } catch (Exception ex) {
            LOGGER.warn("FerryBridge failed to write collected file", ex);
        }
    }

    private static void appendBucket(StringBuilder sb, String bucket) {
        Set<String> set = buckets.get(bucket);
        if (set == null || set.isEmpty()) {
            return;
        }
        // 排序后输出：每次刷新顺序稳定，方便 diff 出"这次新增了什么"。
        List<String> sorted = set.stream().sorted().collect(Collectors.toList());
        sb.append('\n').append("===== ").append(bucket).append("（").append(sorted.size()).append(" 条）=====\n");
        for (String line : sorted) {
            // 剥 § 格式码：与扫描端口径一致，避免同一句话在映射表里生成两份 key。
            // （record 的注释说"写盘前会剥掉"，2026-10-12 才发现这里其实一直没剥。）
            String clean = FORMAT_CODES.matcher(line).replaceAll("");
            // 原文里的换行会破坏"一行一条"的格式，压成空格。
            sb.append(clean.replace('\n', ' ')).append('\n');
        }
    }

    /** 供诊断报告读取的分桶快照（不用于展示格式，只用于计数）。 */
    public static Map<String, Integer> bucketSizes() {
        Map<String, Integer> snapshot = new LinkedHashMap<>();
        for (Map.Entry<String, Set<String>> e : buckets.entrySet()) {
            snapshot.put(e.getKey(), e.getValue().size());
        }
        return snapshot;
    }
}
