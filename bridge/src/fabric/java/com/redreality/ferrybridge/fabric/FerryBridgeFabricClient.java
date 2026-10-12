package com.redreality.ferrybridge.fabric;

import com.redreality.ferrybridge.core.TranslationStore;
import net.fabricmc.api.ClientModInitializer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Fabric 版客户端入口（**尚未接线**，当前只是个骨架）。
 *
 * <p>与 Forge 版的差别只剩"谁在什么时候调用一次 init"：这里调 onInitializeClient，
 * Forge 版调 @Mod 构造器。映射表加载完全一致，因为 core.TranslationStore
 * 从 jar 内资源流读取。
 *
 * <p>2026-10-12（L1）：文本替换已不再靠 mixin —— Forge 版改成了两个官方事件
 * （聊天栏 ClientChatReceivedEvent、物品 tooltip ItemTooltipEvent）。
 * Fabric 版要挂的是 fabric-api 对应的事件（物品 ItemTooltipCallback、
 * 聊天消息的 ClientReceiveMessageEvents 之类），这部分**还没做**，
 * 等做多加载器（L2）时再落地。core 包零 MC 依赖，届时不用改它一行。
 */
public class FerryBridgeFabricClient implements ClientModInitializer {

    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    @Override
    public void onInitializeClient() {
        TranslationStore store = new TranslationStore();
        TranslationStore.install(store);
        String result = store.loadFromJar();
        LOGGER.info("FerryBridge ready: {} ({} entries)", result, store.size());
    }
}