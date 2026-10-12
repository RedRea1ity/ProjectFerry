package com.redreality.ferrybridge.client;

import com.redreality.ferrybridge.core.FerryBridgeCollector;
import com.redreality.ferrybridge.core.FerryBridgeLive;
import com.redreality.ferrybridge.core.TranslationStore;
import net.minecraft.network.chat.Component;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.event.entity.player.ItemTooltipEvent;
import net.minecraftforge.eventbus.api.SubscribeEvent;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.event.lifecycle.FMLClientSetupEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;

/**
 * 物品悬浮说明（tooltip）的文本替换。
 *
 * <h2>为什么用事件而不是 Mixin</h2>
 *
 * <p>2026-10-10 的血泪教训：原本想用 {@code ComponentMixin}（对
 * {@code net.minecraft.network.chat.Component} 接口做 {@code @ModifyVariable}）
 * 来覆盖所有 {@code getString()} 调用，结果在 Mixin 0.8.5 下**无解** ——
 * 三条约束互相锁死（interface mixin 方法必须 public / 注入方法必须 private /
 * mixin 形态必须与目标类一致），强行启用会让游戏 FATAL。
 *
 * <p>而 tooltip 这一条路径，Forge 早就给了**现成的官方事件**
 * {@link ItemTooltipEvent}：它直接把手柄里的 {@code List<Component>} 交给你，
 * 可以原地替换。于是：
 *
 * <ul>
 *   <li><b>不需要 Mixin</b> → 不需要 refmap → 不会有混淆名/签名问题；</li>
 *   <li><b>不需要碰 interface</b> → 不存在那三约束互锁；</li>
 *   <li>跨版本最稳：Forge 事件 API 比字节码注入稳定得多。</li>
 * </ul>
 *
 * <p><b>但要注意</b>：这是 Forge 专属 API。Fabric 版得换
 * {@code ItemTooltipCallback}（fabric-api 提供），核心查表逻辑
 * （{@link TranslationStore}）完全不用改 —— 这正是把 core 层做成零 MC 依赖的回报。
 */
@Mod.EventBusSubscriber(modid = "ferrybridge", bus = Mod.EventBusSubscriber.Bus.MOD, value = Dist.CLIENT)
public final class FerryBridgeTooltip {

    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    /** 本注入点在对外的名字里出现一次即可，统计用的标识。 */
    private static final String INJECTOR_NAME = "FerryBridgeTooltip#ItemTooltipEvent";

    private FerryBridgeTooltip() {
    }

    @SubscribeEvent
    public static void onClientSetup(FMLClientSetupEvent event) {
        event.enqueueWork(() -> {
            MinecraftForge.EVENT_BUS.addListener(FerryBridgeTooltip::onItemTooltip);
            LOGGER.info("FerryBridge tooltip hook ready ({}s)", "ItemTooltipEvent");
        });
    }

    /**
     * 物品 tooltip 生成时调用。
     *
     * <p>{@code event.getToolTip()} 返回的就是稍后要渲染的那个可变列表，
     * 原地替换元素即可让玩家看到中文。列表元素是
     * {@link Component}（多数是可变的 {@code MutableComponent}），
     * 但我们不假设可变性 —— 直接 {@code List.set} 换新对象，最安全。
     */
    private static void onItemTooltip(ItemTooltipEvent event) {
        if (event == null) {
            return;
        }
        // 与聊天路径（FerryBridgeChat）共用同一个开关：
        // 关掉注入（F10）时两边一起停，玩家只需记一个开关。
        if (!FerryBridgeLive.isInjectionOn()) {
            return;
        }
        TranslationStore store = TranslationStore.get();

        List<Component> lines = event.getToolTip();
        if (lines == null || lines.isEmpty()) {
            return;
        }

        // 这个事件本身也是个"注入点活着"的信号，报给 HUD 统计。
        FerryBridgeLive.markInjectorLive(INJECTOR_NAME);

        boolean changed = false;
        for (int i = 0; i < lines.size(); i++) {
            Component line = lines.get(i);
            if (line == null) {
                continue;
            }
            String plain = line.getString();
            if (plain == null || plain.isBlank()) {
                continue;
            }
            String mapped = store == null ? null : store.lookup(plain);
            if (mapped == null || mapped.equals(plain)) {
                // 未命中就记进采集清单，作为批量翻译的原料。
                // 这里会采进一些本来就不该翻的行（原版物品说明、数字行等），
                // 但采集器自己有「过短 / 无拉丁字母 / 含中文」三道过滤，
                // 下游翻译阶段还会再过一遍 —— 宁可多采，不要漏。
                FerryBridgeCollector.record(FerryBridgeCollector.BUCKET_TOOLTIP, plain);
                continue;
            }
            Component replaced = Component.literal(mapped).setStyle(line.getStyle());
            lines.set(i, replaced);
            changed = true;
            FerryBridgeLive.recordHit(plain, mapped);
        }

        // 一条都没命中时不计 miss：tooltip 里本来就有大量不该翻的内容
        //（数字、图标行、原版说明），全记成 miss 会让统计失去意义。
        if (!changed) {
            FerryBridgeLive.recordMiss();
        }
    }
}
