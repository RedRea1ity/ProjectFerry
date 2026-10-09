package com.redreality.ferrybridge.core;

import com.google.gson.Gson;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
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
 * <p>映射表随 jar 一起分发，路径固定为 {@code /ferrybridge/data/translations.json}，
 * 由摆渡计划在每次导出时写入。这样玩家只需要把一个 jar 丢进 mods 目录，
 * 不需要任何额外配置。
 *
 * <p>查表 O(1)，未命中返回 null（原样显示）。支持带 § 格式码的文本：
 * 先剥码比对，命中后把原串开头连续的格式码接回来。
 */
public final class TranslationStore {
    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    private static final Gson GSON = new Gson();
    private static final Pattern FORMAT_CODES = Pattern.compile("\u00a7.");
    private static final Pattern LEADING_CODES = Pattern.compile("^(\u00a7.)*");

    /** 映射表在 jar 内的固定路径。摆渡写入端必须与此保持一致。 */
    public static final String RESOURCE_PATH = "/ferrybridge/data/translations.json";

    private final Map<String, String> exact = new HashMap<>();

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
     * 从 jar 内部加载映射表。用资源流而非文件路径，因此不依赖任何加载器的
     * 配置目录约定——这是 Forge / Fabric 共用同一份实现的前提。
     *
     * @return 返回给玩家看的结果描述
     */
    public String loadFromJar() {
        Map<String, String> loaded = new HashMap<>();
        try (InputStream in = TranslationStore.class.getResourceAsStream(RESOURCE_PATH)) {
            if (in == null) {
                LOGGER.warn("FerryBridge mapping resource not found: {}", RESOURCE_PATH);
                exact.clear();
                return "jar 内未找到映射表（用摆渡计划重新导出）";
            }
            try (InputStreamReader reader = new InputStreamReader(in, StandardCharsets.UTF_8)) {
                JsonObject root = GSON.fromJson(reader, JsonObject.class);
                if (root == null || !root.has("map") || !root.get("map").isJsonObject()) {
                    LOGGER.warn("FerryBridge mapping resource has no map object");
                    exact.clear();
                    return "映射表格式不对（缺少 map 字段）";
                }
                for (Map.Entry<String, JsonElement> entry : root.getAsJsonObject("map").entrySet()) {
                    String english = entry.getKey();
                    if (english == null || english.isBlank()) {
                        continue;
                    }
                    JsonElement value = entry.getValue();
                    String chinese = value != null && value.isJsonPrimitive() ? value.getAsString() : null;
                    if (chinese != null && !chinese.isBlank()) {
                        loaded.put(english, chinese);
                    }
                }
            }
        } catch (Exception ex) {
            // 读取失败必须降级为"不翻译"，绝不能让游戏开不了。
            LOGGER.error("FerryBridge failed to load mapping resource", ex);
            exact.clear();
            return "映射表读取失败：" + ex.getMessage();
        }
        exact.clear();
        exact.putAll(loaded);
        LOGGER.info("FerryBridge loaded {} mappings from jar resource", exact.size());
        return "已加载 " + exact.size() + " 条映射";
    }

    /**
     * 查表并翻译一段纯文本。
     *
     * @param plain 原始英文文本，可能带 § 格式码
     * @return 命中则返回中文（保留前导格式码），未命中返回 null
     */
    public String lookup(String plain) {
        if (plain == null || exact.isEmpty()) {
            return null;
        }
        String hit = exact.get(plain);
        if (hit != null) {
            return hit;
        }
        String stripped = FORMAT_CODES.matcher(plain).replaceAll("");
        hit = exact.get(stripped);
        if (hit == null) {
            return null;
        }
        Matcher leading = LEADING_CODES.matcher(plain);
        if (leading.find()) {
            hit = leading.group() + hit;
        }
        return hit;
    }

    public int size() {
        return exact.size();
    }
}