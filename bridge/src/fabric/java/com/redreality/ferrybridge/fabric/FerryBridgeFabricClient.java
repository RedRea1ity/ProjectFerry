package com.redreality.ferrybridge.fabric;

import com.redreality.ferrybridge.core.TranslationStore;
import net.fabricmc.api.ClientModInitializer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Fabric 版客户端入口。
 *
 * <p>与 Forge 版的差别只剩"谁在什么时候调用一次 init"：这里调onInitializeClient，
 * Forge 版调 @Mod 构造器。除此之外两边的桥接行为完全一致，因为映射表由
 * core.TranslationStore 从 jar 内资源流读取，而文本替换由
 * {@code ferrybridge.mixins.json} 里那两个与加载器无关的 mixin 完成。
 *
 * <p>同一份 mixin 配置文件被Forge 版与 Fabric 版共用——这是"一个 jar 通吃"
 * 能成立的关键。
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