package com.redreality.ferrybridge.core;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;

/**
 * 一键诊断包：按一次键，把「排查桥为什么没生效」需要的东西收成一份纯文本。
 *
 * <p><b>为什么要有这个类</b>
 *
 * <p>桥出问题时，玩家手上其实什么证据都没有：日志在 {@code logs/latest.log}、
 * 状态在 {@code ferrybridge-status.txt}、装了哪些模组在 {@code mods/}，
 * 让一个只想打游戏的玩家自己去凑齐这些、再找到作者发过去，中间每一步都会掉人。
 * 结果就是「有问题」三个字，没有下文。
 *
 * <p>所以这个类的目标只有一个：<b>让玩家按一下键，拿到一个可以整份发出去的文件。</b>
 * 内容全部是纯文本，直接粘贴或当附件都行。
 *
 * <p><b>为什么放在 core 包</b>：本类只读目录与文件，<b>不引用任何 Minecraft /
 * 加载器类</b>（加载器与版本号由 client 层作为字符串传进来），因此 Fabric 版可以原样复用。
 */
public final class FerryBridgeReport {

    /** 诊断包文件名。固定名（不接时间戳）是刻意的：好让玩家记住「就那一个文件」。 */
    public static final String FILE_NAME = "ferrybridge-diagnose.txt";

    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    /** 日志开头若干行 —— Minecraft 与各加载器的版本号都在这里。 */
    private static final int LOG_HEAD_LINES = 25;

    /** 日志结尾若干行 —— 真正的报错都在这附近。 */
    private static final int LOG_TAIL_LINES = 250;

    /** 采集文件只取前若干行，避免诊断包被撑成几兆。 */
    private static final int COLLECTED_LINES = 120;

    /** 日志超过这个大小就只读尾部，不整个塞进内存。 */
    private static final long LOG_MAX_BYTES = 8L * 1024 * 1024;

    private FerryBridgeReport() {
    }

    /**
     * 写诊断包到游戏目录。
     *
     * @param gameDir    游戏（实例）目录
     * @param loaderInfo 形如 {@code "Forge 47.3.0"}，由 client 层组装
     * @return 写出的文件路径；失败返回 {@code null} —— 诊断只是辅助，
     *         绝不能因为它出错而影响游戏。
     */
    public static Path write(Path gameDir, String loaderInfo) {
        if (gameDir == null) {
            return null;
        }
        try {
            Path target = gameDir.resolve(FILE_NAME);
            Files.write(target, build(gameDir, loaderInfo).getBytes(StandardCharsets.UTF_8));
            return target;
        } catch (IOException ex) {
            return null;
        }
    }

    /** 组装诊断包正文。抽出来是为了可测：不写盘也能检查内容。 */
    public static String build(Path gameDir, String loaderInfo) {
        StringBuilder sb = new StringBuilder(1 << 16);
        sb.append("摆渡桥 FerryBridge 诊断包\n");
        sb.append("生成时间: ").append(LocalDateTime.now().format(STAMP)).append('\n');
        sb.append("（纯文本。要反馈问题时，把整个文件发出去就行 —— 粘贴或当附件都可以。）\n\n");

        sb.append("【环境】\n");
        sb.append("  加载器/版本 : ")
          .append(loaderInfo == null || loaderInfo.isEmpty() ? "（未知）" : loaderInfo).append('\n');
        sb.append("  游戏目录    : ").append(gameDir).append('\n');
        sb.append("  桥 jar      : ").append(findBridgeJar(gameDir)).append('\n');
        sb.append('\n');

        sb.append("【桥的实时状态】\n");
        for (String line : FerryBridgeLive.hudLines()) {
            sb.append("  ").append(stripColor(line)).append('\n');
        }
        sb.append('\n');

        appendMods(sb, gameDir);
        appendCollected(sb, gameDir);
        appendLog(sb, gameDir);

        sb.append("【说明】\n");
        sb.append("  · 「生效路径 0 条」通常只代表本次启动还没出现过文本，不是故障；\n");
        sb.append("    进游戏打一句话、悬停一个物品，再导出一份就能看到变化。\n");
        sb.append("  · 下面【游戏日志】会包含你玩过的聊天内容和存档名。不想发的话，\n");
        sb.append("    把那两段删掉再发，不影响排查桥本身的问题。\n");
        return sb.toString();
    }

    /** 列出 mods 目录里的 jar —— 「是哪个模组的文本没翻」全靠这份清单。 */
    private static void appendMods(StringBuilder sb, Path gameDir) {
        List<String> jars = listJars(gameDir.resolve("mods"));
        sb.append("【模组列表】共 ").append(jars.size()).append(" 个 jar\n");
        if (jars.isEmpty()) {
            sb.append("  （mods 目录里没找到 jar）\n");
        } else {
            for (String name : jars) {
                sb.append("  ").append(name).append('\n');
            }
        }
        sb.append('\n');
    }

    /** 采集文件 = 「表里还没有、但游戏里真实出现过」的原文，是最有价值的线索。 */
    private static void appendCollected(StringBuilder sb, Path gameDir) {
        Path file = gameDir.resolve(FerryBridgeCollector.FILE_NAME);
        List<String> lines = readLines(file);
        sb.append("【已采集的待翻译文本】");
        if (lines.isEmpty()) {
            sb.append("（还没有。进游戏跑一跑，聊天 / tooltip 里没翻的会自动收进来）\n\n");
            return;
        }
        sb.append("共 ").append(lines.size()).append(" 行，这里给出前 ")
          .append(Math.min(COLLECTED_LINES, lines.size())).append(" 行\n");
        for (int i = 0; i < Math.min(COLLECTED_LINES, lines.size()); i++) {
            sb.append("  ").append(lines.get(i)).append('\n');
        }
        sb.append('\n');
    }

    /** 日志头（版本号）+ 日志尾（报错）。中间的游戏过程对排查没用，省掉。 */
    private static void appendLog(StringBuilder sb, Path gameDir) {
        Path log = gameDir.resolve("logs").resolve("latest.log");
        if (!Files.isRegularFile(log)) {
            sb.append("【游戏日志】找不到 ").append(log).append('\n');
            sb.append("  （如果游戏还没启动过，或用了别的日志目录，这条忽略即可）\n\n");
            return;
        }

        List<String> head = readLines(log);
        sb.append("【游戏日志 · 开头】（版本号在这里）\n");
        if (head.isEmpty()) {
            sb.append("  （读不出来）\n");
        } else {
            for (int i = 0; i < Math.min(LOG_HEAD_LINES, head.size()); i++) {
                sb.append("  ").append(head.get(i)).append('\n');
            }
        }
        sb.append('\n');

        List<String> tail = tailLines(log, LOG_TAIL_LINES);
        sb.append("【游戏日志 · 结尾】（最近 ").append(tail.size()).append(" 行，报错都在这附近）\n");
        for (String line : tail) {
            sb.append("  ").append(line).append('\n');
        }
        sb.append('\n');
    }

    /** 桥自己的 jar 文件名（从 mods 目录里按名字找，不依赖加载器 API）。 */
    private static String findBridgeJar(Path gameDir) {
        for (String name : listJars(gameDir.resolve("mods"))) {
            if (name.toLowerCase().contains("ferrybridge")) {
                return name;
            }
        }
        return "（mods 目录里没找到，可能是集成进别的 jar 了）";
    }

    private static List<String> listJars(Path modsDir) {
        if (!Files.isDirectory(modsDir)) {
            return List.of();
        }
        try (var stream = Files.list(modsDir)) {
            return stream
                    .filter(Files::isRegularFile)
                    .map(p -> p.getFileName().toString())
                    .filter(n -> n.toLowerCase().endsWith(".jar"))
                    .sorted()
                    .collect(Collectors.toList());
        } catch (IOException ex) {
            return List.of();
        }
    }

    /** 读整个小文件的全部行；读不动就返回空表（诊断不能用异常干扰游戏）。 */
    private static List<String> readLines(Path path) {
        try {
            if (!Files.isRegularFile(path)) {
                return List.of();
            }
            if (Files.size(path) > LOG_MAX_BYTES) {
                return new ArrayList<>(tailLines(path, LOG_TAIL_LINES));
            }
            return Files.readAllLines(path, StandardCharsets.UTF_8);
        } catch (IOException ex) {
            return List.of();
        }
    }

    /**
     * 只取文件结尾的若干行。
     *
     * <p>日志可能有几十兆，不能整个读进来 —— 超过上限时定位到文件末尾往前读一段，
     * 并按第一个换行切开（开头那半行是截断的，丢掉）。
     */
    private static List<String> tailLines(Path path, int count) {
        try {
            if (!Files.isRegularFile(path)) {
                return List.of();
            }
            long size = Files.size(path);
            String text;
            if (size <= LOG_MAX_BYTES) {
                text = Files.readString(path, StandardCharsets.UTF_8);
            } else {
                byte[] buf;
                try (InputStream in = Files.newInputStream(path)) {
                    in.skipNBytes(size - LOG_MAX_BYTES);
                    buf = in.readAllBytes();
                }
                text = new String(buf, StandardCharsets.UTF_8);
                int newline = text.indexOf('\n');
                if (newline >= 0) {
                    text = text.substring(newline + 1);
                }
            }
            String[] all = text.split("\r?\n");
            int from = Math.max(0, all.length - count);
            List<String> out = new ArrayList<>(all.length - from);
            for (int i = from; i < all.length; i++) {
                if (!all[i].isEmpty()) {
                    out.add(all[i]);
                }
            }
            return out;
        } catch (IOException ex) {
            return List.of();
        }
    }

    /** 去掉 § 颜色代码，写文件要可读。 */
    private static String stripColor(String s) {
        return s == null ? "" : s.replaceAll("\u00a7.", "");
    }
}
