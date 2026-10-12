package com.redreality.ferrybridge.core;

import com.google.gson.Gson;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 摆渡桥的共享核心：英文原文 -> 中文的精确映射表。
 *
 * <p>这一层刻意不引用任何 Minecraft 类，也不引用任何加载器 API（无
 * net.minecraftforge.*、无 net.fabricmc.*），因此同一份源码可以被打进
 * Forge / Fabric / NeoForge 三种 jar 而不冲突。
 *
 * <h2>两级表：jar 内置 + 游戏目录外部表</h2>
 *
 * <p><b>jar 内置表</b>随 jar 一起分发，路径固定为
 * {@code /ferrybridge/data/translations.json}，由摆渡计划在打包时写入。
 * 玩家只需要把一个 jar 丢进 mods 目录，不需要任何额外配置。
 *
 * <p><b>外部表</b>（{@value #EXTERNAL_NAME}）放在游戏目录下，是本类为「大批量翻译」
 * 专门留的口子，原因是 2026-10-12 想清楚的一件事：
 *
 * <blockquote>
 *   只要映射表还锁在 jar 里，每加一条译文就要「改源码 → 重新构建 → 换 jar → 重启游戏」，
 *   一轮五分钟起步。tooltip 和聊天栏的文本动辄几百上千条，靠这条路永远做不成「大批量」。
 * </blockquote>
 *
 * <p>于是把表拆成两层：jar 内是随包分发的基线，外部文件是**可热重载的工作面**。
 * 摆渡（或玩家手工）往外部文件里写译文，桥每秒比对一次 mtime，变了就地重载，
 * <b>游戏不用重启</b>。这条设计直接照抄了社区里 LiteralBridge 那个「改完自动热重载」的手感——
 * 我们的桥本来就该是这个手感。
 *
 * <p>同一条 key 两边都有时，<b>外部表优先</b>：外部是后写的，理应更新。
 *
 * <h2>查表语义（红线，不要动）</h2>
 *
 * <p><b>只做整条精确匹配</b>，连大小写都不容错。这不是偷懒，是刻意的安全边界：
 * 玩家自己敲进聊天框的话永远不可能正好等于表里的 key，所以「玩家内容被乱翻」
 * 这件事在结构上就不可能发生。任何"子串替换 / 模糊匹配"的改法都会破坏这条性质
 * （一个短词会啃掉玩家名，v1.8.0 的规则导出已经吃过这个亏），<b>不要加</b>。
 *
 * <p><b>唯一的例外是「格式噪声归一化」</b>（{@link #normalize}）：剥掉 § 格式码、
 * 把排版省略号 … 折叠成 ...，两侧（入库与查询）同口径执行。它不改变「整条匹配」的性质，
 * 只抹平「同一条文本在不同环节的编码差异」——这是实测里最常见的假性未命中来源。
 *
 * <p>查表 O(1)，未命中返回 null（原样显示）。
 */
public final class TranslationStore {
    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    private static final Gson GSON = new Gson();
    private static final Pattern FORMAT_CODES = Pattern.compile("\u00a7.");
    private static final Pattern LEADING_CODES = Pattern.compile("^(\u00a7.)*");

    /**
     * 排版省略号（… / ……）折叠成三个 ASCII 点。
     *
     * <p>2026-10-12 实测：同一个模组的同一条聊天长句，<b>编译进 class 常量的形态</b>
     * 与<b>运行时 getString() 拿到的形态</b>并不总是逐字符相同 —— 前者是 `...`，
     * 后者可能是 `……`（U+2026）。扫描端从 class 常量取原文，运行时从 getString() 取原文，
     * 两侧不归一化就永远匹配不上，表现为「明明扫出来翻了，游戏里还是英文」。
     */
    private static final Pattern ELLIPSIS = Pattern.compile("\u2026+");

    /** jar 内置映射表的固定路径。摆渡写入端必须与此保持一致。 */
    public static final String RESOURCE_PATH = "/ferrybridge/data/translations.json";

    /**
     * 外部映射表的固定文件名，位于<b>游戏目录</b>下。
     *
     * <p>与 jar 内置表同一套 JSON 结构（{@code {"map": {"英文": "中文"}}}），
     * 摆渡批量翻译后写这里，桥热重载即生效，无需重新打包 jar。
     */
    public static final String EXTERNAL_NAME = "ferrybridge-mappings.json";

    /** jar 内置表：随 jar 分发，启动后只读，因此可以无锁并发读。 */
    private final Map<String, String> jarEntries = new HashMap<>();

    /**
     * 外部表：整体替换，用 volatile 引用保证读侧无锁。
     * 重载线程构造好新 Map 后一次性换上去，读到半个表的情况不存在。
     */
    private volatile Map<String, String> externalEntries = new HashMap<>();

    /** 外部表路径，由加载器入口在初始化时设置；未设置时为 null（等价于"没有外部表"）。 */
    private volatile Path externalPath;

    /** 上次读到的外部表 mtime，用于热重载判断。Long.MIN_VALUE 表示"还没读过"。 */
    private volatile long externalMtime = Long.MIN_VALUE;

    /**
     * 全局单例。Mixin 注入点在任意包、任意加载器下都需要访问同一个实例，
     * 而 Mixin 无法直接调用实例方法，因此由桥接入口在客户端初始化时赋值。
     */
    private static volatile TranslationStore instance = new TranslationStore();

    /** 供加载器入口在初始化时调用，注入 Mixin 之后使用。 */
    public static void install(TranslationStore store) {
        if (store != null) {
            instance = store;
        }
    }

    /** 供 Mixin 注入类使用；未初始化时返回一个空表而不是 null。 */
    public static TranslationStore get() {
        return instance;
    }

    /**
     * 设置外部表路径。必须在 {@link #startExternalWatcher()} 之前调用。
     *
     * @param path 通常是 {@code <游戏目录>/ferrybridge-mappings.json}
     */
    public void setExternalPath(Path path) {
        this.externalPath = path;
    }

    public Path getExternalPath() {
        return externalPath;
    }

    /**
     * 从 jar 内部加载内置映射表。用资源流而非文件路径，因此不依赖任何加载器的
     * 配置目录约定——这是 Forge / Fabric 共用同一份实现的前提。
     *
     * @return 返回给玩家看的结果描述
     */
    public String loadFromJar() {
        Map<String, String> loaded;
        try (InputStream in = TranslationStore.class.getResourceAsStream(RESOURCE_PATH)) {
            if (in == null) {
                LOGGER.warn("FerryBridge mapping resource not found: {}", RESOURCE_PATH);
                jarEntries.clear();
                return "jar 内未找到映射表（用摆渡计划重新导出）";
            }
            loaded = parseMap(decode(in.readAllBytes()));
        } catch (Exception ex) {
            // 读取失败必须降级为"不翻译"，绝不能让游戏开不了。
            LOGGER.error("FerryBridge failed to load mapping resource", ex);
            jarEntries.clear();
            return "映射表读取失败：" + ex.getMessage();
        }
        jarEntries.clear();
        jarEntries.putAll(loaded);
        LOGGER.info("FerryBridge loaded {} mappings from jar resource", jarEntries.size());
        return "内置表 " + jarEntries.size() + " 条";
    }

    /**
     * 从游戏目录的外部文件重载映射表。写坏了（比如摆渡正写到一半）就保留旧表，绝不清空。
     *
     * <p>本方法可能被启动线程和热重载线程同时调到，因此加锁；实际调用频率极低
     * （启动一次 + 文件变更时一次），锁开销可以忽略。
     *
     * @return 返回给玩家看的结果描述
     */
    public synchronized String reloadExternal() {
        Path path = externalPath;
        if (path == null) {
            return "外部表未配置";
        }
        if (!Files.exists(path)) {
            // 没有外部表是**正常情况**（只用 jar 内置表），不是错误。
            externalEntries = new HashMap<>();
            externalMtime = -1L;
            return "无外部表（仅用内置表）";
        }
        try {
            long mtime = Files.getLastModifiedTime(path).toMillis();
            Map<String, String> loaded = parseMap(decode(Files.readAllBytes(path)));
            externalEntries = loaded;
            externalMtime = mtime;
            LOGGER.info("FerryBridge external mapping loaded: {} entries from {}", loaded.size(), path);
            return "外部表 " + loaded.size() + " 条";
        } catch (Exception ex) {
            // 关键：解析失败时**不动** externalEntries，保留上一次的好表。
            // 否则摆渡写到一半被读到，就会把一整表译文清空，玩家看到"全部变回英文"。
            LOGGER.warn("FerryBridge failed to reload external mapping (kept previous)", ex);
            return "外部表重载失败（保留旧表）：" + ex.getMessage();
        }
    }

    /**
     * 启动后台守护线程，每秒比对一次外部表的 mtime，变了就热重载。
     *
     * <p>用轮询而不是 WatchService：WatchService 在部分平台上对"编辑器保存"的
     * 写文件模式（先删再建 / 原子重命名）行为不一致，而 mtime 轮询在所有平台上
     * 都是同一个语义，也不会有句柄泄漏的问题。代价是每秒一次 stat，可忽略。
     *
     * <p>线程是 daemon，游戏退出自动结束；任何异常都吞掉，热重载坏掉绝不影响游戏。
     */
    public void startExternalWatcher() {
        final Path path = externalPath;
        if (path == null) {
            return;
        }
        Thread watcher = new Thread(() -> {
            // 启动时先记下当前 mtime，避免第一次循环马上又重载一遍。
            try {
                externalMtime = Files.exists(path) ? Files.getLastModifiedTime(path).toMillis() : -1L;
            } catch (Exception ignored) {
                externalMtime = -1L;
            }
            while (!Thread.currentThread().isInterrupted()) {
                try {
                    Thread.sleep(1000L);
                } catch (InterruptedException ie) {
                    return;
                }
                try {
                    long mtime = Files.exists(path) ? Files.getLastModifiedTime(path).toMillis() : -1L;
                    if (mtime != externalMtime) {
                        reloadExternal();
                    }
                } catch (Exception ignored) {
                    // 静默：热重载只是便利功能，坏了也不能影响游戏。
                }
            }
        }, "FerryBridge-MappingWatcher");
        watcher.setDaemon(true);
        watcher.start();
        LOGGER.info("FerryBridge external mapping watcher started: {}", path);
    }

    /**
     * 查表并翻译一段纯文本。
     *
     * @param plain 原始英文文本，可能带 § 格式码
     * @return 命中则返回中文（保留前导格式码），未命中返回 null
     */
    public String lookup(String plain) {
        if (plain == null) {
            return null;
        }
        Map<String, String> external = externalEntries;
        Map<String, String> jar = jarEntries;
        if (external.isEmpty() && jar.isEmpty()) {
            return null;
        }

        // 外部表优先：它是后写的，代表更新的译法。
        String hit = external.get(plain);
        if (hit == null) {
            hit = jar.get(plain);
        }
        if (hit != null) {
            return hit;
        }

        // 含格式噪声（§ 码 / 排版省略号）时，归一化再试一次。
        // key 在入库时已按同一口径归一化（见 parseMap），所以这里能对上。
        String normalized = normalize(plain);
        if (normalized.equals(plain)) {
            // 本来就没有噪声，上面已经试过了，不用重复查。
            return null;
        }
        hit = external.get(normalized);
        if (hit == null) {
            hit = jar.get(normalized);
        }
        if (hit == null) {
            return null;
        }
        // 命中后把开头连续的格式码接回去，不然§c 这类颜色前缀会丢。
        Matcher leading = LEADING_CODES.matcher(plain);
        if (leading.find()) {
            hit = leading.group() + hit;
        }
        return hit;
    }

    /**
     * 把文本归一化成「查表口径」：剥掉 § 格式码，折叠排版省略号。
     *
     * <p>入库（{@link #parseMap}）与查询（{@link #lookup}）都走这里，
     * 保证两侧口径严格一致。扫描端（摆渡计划 Python）生成 key 时用的是同一套规则。
     */
    private static String normalize(String text) {
        return ELLIPSIS.matcher(FORMAT_CODES.matcher(text).replaceAll("")).replaceAll("...");
    }

    /** 总条数（两级表的近似和；两边有重复 key 时会略微高估，仅用于展示）。 */
    public int size() {
        return jarEntries.size() + externalEntries.size();
    }

    public int jarSize() {
        return jarEntries.size();
    }

    public int externalSize() {
        return externalEntries.size();
    }

    /**
     * 解析 {@code {"map": {"英文": "中文"}}} 结构。
     *
     * <p>结构与 jar 内置表完全一致，摆渡导出两类表时共用同一套代码。
     */
    private static Map<String, String> parseMap(String json) {
        Map<String, String> loaded = new HashMap<>();
        JsonObject root = GSON.fromJson(json, JsonObject.class);
        if (root == null || !root.has("map") || !root.get("map").isJsonObject()) {
            throw new IllegalArgumentException("缺少 map 字段");
        }
        for (Map.Entry<String, JsonElement> entry : root.getAsJsonObject("map").entrySet()) {
            String english = entry.getKey();
            if (english == null || english.isBlank()) {
                continue;
            }
            // 统一归一化（剥 § 码 + 折叠省略号），与 lookup 侧口径一致。
            // 这样无论摆渡写进来的是 `...` 还是 `……`、带不带 § 码，都能和运行时文本对上。
            english = normalize(english);
            JsonElement value = entry.getValue();
            String chinese = value != null && value.isJsonPrimitive() ? value.getAsString() : null;
            if (chinese != null && !chinese.isBlank()) {
                loaded.put(english, chinese);
            }
        }
        return loaded;
    }

    /**
     * 解码为 UTF-8 字符串，并剥掉可能存在的 BOM。
     *
     * <p>剥 BOM 是为玩家好：用 Windows 记事本编辑外部表会默认加 BOM，
     * 而 Gson 遇到 BOM 会直接报 "Use JsonReader.setLenient(true)"，
     * 那份表就整份作废。多加这五行，能省掉一整类"改了没生效"的困惑。
     */
    private static String decode(byte[] bytes) {
        int offset = 0;
        if (bytes.length >= 3
                && (bytes[0] & 0xFF) == 0xEF
                && (bytes[1] & 0xFF) == 0xBB
                && (bytes[2] & 0xFF) == 0xBF) {
            offset = 3;
        }
        return new String(bytes, offset, bytes.length - offset, StandardCharsets.UTF_8);
    }
}
