package com.redreality.ferrybridge.client;

import com.mojang.blaze3d.platform.InputConstants;
import com.redreality.ferrybridge.core.FerryBridgeLive;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.client.event.RegisterKeyMappingsEvent;
import net.minecraftforge.client.event.RenderGuiEvent;
import net.minecraftforge.client.settings.KeyConflictContext;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.event.TickEvent;
import net.minecraftforge.eventbus.api.SubscribeEvent;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.event.lifecycle.FMLClientSetupEvent;
import net.minecraftforge.fml.loading.FMLPaths;
import org.lwjgl.glfw.GLFW;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 游戏内实时状态浮层（HUD）。
 *
* <p><b>解决的问题</b>：诊断信息原本只能写日志／文件，必须重启游戏才能刷新。
     * 排查「注入点到底生效没有」时，"改代码 → 重启 → 看报告 → 再改" 每轮几分钟，
     * 效率极低。改成游戏内浮层后，<b>按 F9 立刻看到当前状态</b>，
     * 且每收到一条聊天消息实时更新命中数。
 *
 * <p><b>为什么单独放 client 包</b>：本类引用 Minecraft 渲染 API 与 Forge 事件，
 * 是纯客户端专属代码。core 包里的东西保持零 MC 依赖，以便 Fabric 版复用。
 */
@Mod.EventBusSubscriber(modid = "ferrybridge", bus = Mod.EventBusSubscriber.Bus.MOD, value = Dist.CLIENT)
public final class FerryBridgeHud {

    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    /** 状态浮层开关按键，默认 F3+G（与 Minecraft 调试信息不冲突的组合）。 */
    private static final String KEY_CATEGORY = "key.categories.ferrybridge";
    public static final String KEY_TOGGLE_HUD = "key.ferrybridge.toggle_hud";
    public static final String KEY_TOGGLE_INJECT = "key.ferrybridge.toggle_inject";

    private static KeyMapping toggleHud;
    private static KeyMapping toggleInject;

    /** 浮层是否显示。默认关闭，不打扰正常游戏。 */
    private static boolean hudVisible = false;

    /** 累计命中/未命中，用于判断是否需要刷新状态文件。 */
    private static int lastWrittenHits = -1;
    private static int lastWrittenLive = -1;

    @SubscribeEvent
    public static void onRegisterKeys(RegisterKeyMappingsEvent event) {
        // 注意：KeyMapping 不支持组合键（如 F3+G），必须是单键。
        // 因此这里用 F9 切换浮层、F10 切换注入开关，都在游戏内单手可按。
        toggleHud = new KeyMapping(
                KEY_TOGGLE_HUD,
                KeyConflictContext.IN_GAME,
                InputConstants.Type.KEYSYM,
                GLFW.GLFW_KEY_F9,
                KEY_CATEGORY);
        toggleInject = new KeyMapping(
                KEY_TOGGLE_INJECT,
                KeyConflictContext.IN_GAME,
                InputConstants.Type.KEYSYM,
                GLFW.GLFW_KEY_F10,
                KEY_CATEGORY);

        event.register(toggleHud);
        event.register(toggleInject);
    }

    /**
     * 客户端初始化：注册渲染事件与 tick 监听。
     *
     * <p>用 {@code FMLClientSetupEvent.enqueueWork} 延迟到主线程注册，
     * 这是 Forge 1.20.1 里注册事件的正确姿势。
     */
    @SubscribeEvent
    public static void onClientSetup(FMLClientSetupEvent event) {
        event.enqueueWork(() -> {
            MinecraftForge.EVENT_BUS.addListener(FerryBridgeHud::onClientTick);
            MinecraftForge.EVENT_BUS.addListener(FerryBridgeHud::onRenderOverlay);
            LOGGER.info("FerryBridge HUD ready: F9 切换浮层, F10 切换注入开关");
        });
    }

    /** 每 tick 处理按键。 */
    private static void onClientTick(TickEvent.ClientTickEvent event) {
        if (event.phase != TickEvent.Phase.END) {
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null) {
            return;
        }

        // consumeClick 避免按住不放时反复触发
        while (toggleHud.consumeClick()) {
            hudVisible = !hudVisible;
            LOGGER.info("FerryBridge HUD: {}", hudVisible ? "shown" : "hidden");
        }
        while (toggleInject.consumeClick()) {
            boolean now = !FerryBridgeLive.isInjectionOn();
            FerryBridgeLive.setInjectionOn(now);
            FerryBridgeLive.writeStatusFile(FMLPaths.GAMEDIR.get());
            LOGGER.info("FerryBridge injection switched: {}", now ? "ON" : "OFF (safe mode)");
        }

        // 状态变化时即时写文件，保证 ferrybridge-status.txt 始终是新鲜的
        if (FerryBridgeLive.getHitCount() != lastWrittenHits
                || FerryBridgeLive.getInjectorLive() != lastWrittenLive) {
            lastWrittenHits = FerryBridgeLive.getHitCount();
            lastWrittenLive = FerryBridgeLive.getInjectorLive();
            FerryBridgeLive.writeStatusFile(FMLPaths.GAMEDIR.get());
        }
    }

    /**
     * 渲染浮层。
     *
     * <p><b>1.20.1 的确切 API（踩过三个坑才写对，记下来免得以后重犯）</b>：
     * <ul>
     *   <li>矩阵来源是 {@code event.getGuiGraphics().pose()}，
     *       <b>不是</b> {@code event.getMatrixStack()}（那是1.21+ 才有的）；</li>
     *   <li>{@code BufferBuilder.begin} 的参数顺序是
     *       {@code begin(VertexFormat.Mode, VertexFormat)} —— <b>Mode 在前</b>；</li>
     *   <li>结束绘制是 {@code end()}，没有 {@code endDraw()}。</li>
     * </ul>
     * 用 {@code GuiGraphics} 自带的填充方法也可，但这里手动画四边形，
     * 以便精确控制颜色与不受版本影响。
     */
    private static void onRenderOverlay(RenderGuiEvent.Pre event) {
        if (!hudVisible) {
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        var font = mc.font;
        var gui = event.getGuiGraphics();
        if (font == null || gui == null) {
            return;
        }
        var lines = FerryBridgeLive.hudLines();

        int lineHeight = font.lineHeight + 1;
        int width = 0;
        for (String line : lines) {
            width = Math.max(width, font.width(line));
        }

        int x = 4;
        int y = 4;
        int right = x + width + 3;
        int bottom = y + lineHeight * lines.length + 2;

        // 半透明黑底（约 69% 不透明），保证任何背景下都读得清
        gui.fill(x - 3, y - 3, right, bottom, 0xB0000000);

        // 逐行渲染，保留 § 颜色码
        for (int i = 0; i < lines.length; i++) {
            gui.drawString(font, lines[i], x, y + i * lineHeight, 0xFFFFFFFF, true);
        }
    }

    /** 供诊断报告复用的开关读取。 */
    public static boolean isHudVisible() {
        return hudVisible;
    }
}
