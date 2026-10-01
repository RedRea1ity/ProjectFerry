package com.redreality.ferrybridge;

import com.mojang.blaze3d.platform.InputConstants;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraft.network.chat.Component;
import net.minecraftforge.client.event.RegisterKeyMappingsEvent;
import net.minecraftforge.client.settings.KeyConflictContext;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.event.TickEvent;
import net.minecraftforge.event.entity.player.ItemTooltipEvent;
import net.minecraftforge.eventbus.api.IEventBus;
import net.minecraftforge.fml.javafmlmod.FMLJavaModLoadingContext;
import net.minecraftforge.fml.loading.FMLPaths;
import org.lwjgl.glfw.GLFW;

import java.nio.file.Path;

/**
 * 全部客户端逻辑集中在这里：服务端类加载时永远不会触碰本类（见 FerryBridgeMod 的 dist 判断）。
 */
final class ClientHandler {
    private static final TranslationStore STORE = new TranslationStore();
    private static final KeyMapping RELOAD = new KeyMapping(
            "key.ferrybridge.reload",
            KeyConflictContext.IN_GAME,
            InputConstants.Type.KEYSYM,
            GLFW.GLFW_KEY_UNKNOWN,
            "key.categories.ferrybridge");

    private ClientHandler() {
    }

    static void init() {
        IEventBus forgeBus = MinecraftForge.EVENT_BUS;
        forgeBus.addListener(ClientHandler::onTooltip);
        forgeBus.addListener(ClientHandler::onClientTick);
        FMLJavaModLoadingContext.get().getModEventBus().addListener(ClientHandler::onRegisterKeys);
        STORE.load(mappingFile());
    }

    private static void onRegisterKeys(RegisterKeyMappingsEvent event) {
        event.register(RELOAD);
    }

    private static Path mappingFile() {
        return FMLPaths.CONFIGDIR.get().resolve("ferrybridge").resolve("translations.json");
    }

    private static void onTooltip(ItemTooltipEvent event) {
        var lines = event.getToolTip();
        for (int i = 0; i < lines.size(); i++) {
            Component line = lines.get(i);
            String plain = line.getString();
            if (plain == null || plain.isBlank()) continue;
            String mapped = STORE.lookup(plain);
            if (mapped == null || mapped.equals(plain)) continue;
            lines.set(i, Component.literal(mapped).withStyle(line.getStyle()));
        }
    }

    private static void onClientTick(TickEvent.ClientTickEvent event) {
        if (event.phase != TickEvent.Phase.END) return;
        Minecraft minecraft = Minecraft.getInstance();
        if (minecraft.player == null) return;
        while (RELOAD.consumeClick()) {
            String result = STORE.load(mappingFile());
            minecraft.player.displayClientMessage(
                    Component.literal("[摆渡桥] " + result + "（" + STORE.size() + " 条映射）"), false);
        }
    }
}
