package com.redreality.ferrybridge;

import com.redreality.ferrybridge.core.FerryBridgeDiag;
import com.redreality.ferrybridge.core.TranslationStore;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.loading.FMLEnvironment;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Forge 侧的模组入口。
 *
 * <p>这里刻意保持极薄：只负责把 core 单例装上、读一次自检配置、
 * 触发一次映射表加载，然后写一份诊断报告。真正的文本替换由 mixin 完成，
 * 本类不注册任何事件、不碰任何渲染逻辑。
 *
 * <p>桥接逻辑本身（core 与 mixin 两个包）不含任何 Forge API，因此同一份代码
 * 可以被 Fabric 版复用；只有"谁在什么时候调用一次 init"这一步是加载器专属的。
 */
@Mod(FerryBridgeMod.MODID)
public class FerryBridgeMod {
    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    public static final String MODID = "ferrybridge";

    public FerryBridgeMod() {
        if (FMLEnvironment.dist == Dist.CLIENT) {
            // 先读配置：必须在注入点开始工作之前定好开关。
            // 顺带让玩家一进游戏就能看到"桥当前是开是关"。
            boolean inject = FerryBridgeDiag.loadConfig(
                    net.minecraftforge.fml.loading.FMLPaths.GAMEDIR.get());

            TranslationStore store = new TranslationStore();
            TranslationStore.install(store);
            String result = store.loadFromJar();

            String report = FerryBridgeDiag.buildReport(
                    net.minecraftforge.fml.loading.FMLPaths.GAMEDIR.get(), store, result);

            LOGGER.info("FerryBridge ready: {} ({} entries, injection {})",
                    result, store.size(), inject ? "ON" : "OFF");
            LOGGER.info("FerryBridge diagnostic report:\n{}", report);
        }
    }
}
