package com.redreality.ferrybridge;

import com.google.gson.Gson;
import com.google.gson.JsonObject;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 英文原文 -> 中文 的精确映射表。查表 O(1)，未命中返回 null（原样显示）。
 * 支持带 § 格式码的文本：先剥码比对，命中后把原串开头连续的格式码接回来。
 */
final class TranslationStore {
    private static final Gson GSON = new Gson();
    private static final Pattern FORMAT_CODES = Pattern.compile("\\u00a7.");
    private static final Pattern LEADING_CODES = Pattern.compile("^(\\u00a7.)*");

    private final Map<String, String> exact = new HashMap<>();

    /** 返回给玩家看的结果描述。线程安全：tooltip 与主线程共用。 */
    synchronized String load(Path file) {
        Map<String, String> loaded = new HashMap<>();
        try {
            if (!Files.isRegularFile(file)) {
                return "未找到映射表（先用摆渡计划导出）";
            }
            JsonObject root = GSON.fromJson(Files.readString(file), JsonObject.class);
            if (root == null || !root.has("map") || !root.get("map").isJsonObject()) {
                return "映射表格式不对（缺少 map 字段）";
            }
            for (Map.Entry<String,?> entry : root.getAsJsonObject("map").entrySet()) {
                String english = entry.getKey();
                if (english == null || english.isBlank()) continue;
                String chinese = entry.getValue() != null && entry.getValue().isJsonPrimitive()
                        ? entry.getValue().getAsString() : null;
                if (chinese != null && !chinese.isBlank()) {
                    loaded.put(english, chinese);
                }
            }
        } catch (Exception ex) {
            return "读取失败：" + ex.getMessage();
        }
        exact.clear();
        exact.putAll(loaded);
        return "已加载 " + file.getFileName();
    }

    synchronized int size() {
        return exact.size();
    }

    synchronized String lookup(String plain) {
        if (exact.isEmpty()) return null;
        String hit = exact.get(plain);
        if (hit != null) return hit;
        String stripped = FORMAT_CODES.matcher(plain).replaceAll("");
        hit = exact.get(stripped);
        if (hit == null) return null;
        Matcher leading = LEADING_CODES.matcher(plain);
        if (leading.find()) {
            hit = leading.group() + hit;
        }
        return hit;
    }
}
