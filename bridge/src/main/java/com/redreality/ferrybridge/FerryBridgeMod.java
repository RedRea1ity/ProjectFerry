package com.redreality.ferrybridge;

import com.redreality.ferrybridge.core.FerryBridgeDiag;
import com.redreality.ferrybridge.core.FerryBridgeLive;
import com.redreality.ferrybridge.core.TranslationStore;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.loading.FMLEnvironment;
import net.minecraftforge.fml.loading.FMLPaths;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Forge 侧的模组入口。
 *
 * <p>这里刻意保持极薄：读一次自检配置 → 触发映射表加载 → 定好实时状态初值。
 * 真正的文本替换由 mixin 完成，本类不注册任何事件、不碰任何渲染逻辑。
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
            var gameDir = FMLPaths.GAMEDIR.get();

            // 先读配置：必须在注入点开始工作之前定好开关。
            boolean inject = FerryBridgeDiag.loadConfig(gameDir);

            TranslationStore store = new TranslationStore();
            TranslationStore.install(store);
            String result = store.loadFromJar();

            // 定好实时状态初值，之后由 FerryBridgeLive 持续更新。
            FerryBridgeLive.init(store.size(), inject);

            // 写两份文件：静态诊断报告（人话结论）+ 实时状态（每次变化刷新）。
            String report = FerryBridgeDiag.buildReport(gameDir, store, result);
            FerryBridgeLive.writeStatusFile(gameDir);

            LOGGER.info("FerryBridge ready: {} ({} entries, injection {})",
                    result, store.size(), inject ? "ON" : "OFF");
            LOGGER.info("FerryBridge diagnostic report:\n{}", report);
            LOGGER.info("FerryBridge: press F3+G in game for live status HUD");
        }
    }
}
