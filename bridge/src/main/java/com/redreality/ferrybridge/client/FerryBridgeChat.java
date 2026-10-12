package com.redreality.ferrybridge.client;

import com.redreality.ferrybridge.core.FerryBridgeCollector;
import com.redreality.ferrybridge.core.FerryBridgeLive;
import com.redreality.ferrybridge.core.TranslationStore;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.MutableComponent;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.client.event.ClientChatReceivedEvent;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.eventbus.api.SubscribeEvent;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.event.lifecycle.FMLClientSetupEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 聊天栏文本替换（Forge 官方事件版）。
 *
 * <h2>为什么从 Mixin 换成事件（2026-10-12 L1）</h2>
 *
 * <p>原先这一条路径是 {@code mixin/ChatComponentMixin}，用 {@code @Inject} 拦
 * {@code ChatComponent.addMessage(...)} 的三个重载。它能工作，但代价是全套 Mixin 负债：
 * refmap 必须靠注解处理器生成（生成失败时因为 {@code require = 0} 会<b>静默失效</b>——
 * 游戏正常启动、日志零 ERROR、功能就是不动）、混淆映射要用 tsrg2 转 SRG、
 * 方法签名的描述符写错就报自相矛盾的错、构建缓存还会让映射改动静默不生效。
 *
 * <p>而 {@link ClientChatReceivedEvent} 是 Forge 给的现成官方事件：
 * 它把「即将显示的这条消息」直接交给我们，可以 {@code setMessage()} 换掉。
 * 于是：
 * <ul>
 *   <li><b>不需要 Mixin</b> → 不需要 refmap、不需要混淆映射、没有静默失效；</li>
 *   <li><b>不需要碰 {@code ChatComponent} 的方法签名</b> → 跨版本改的是事件 API 签名，
 *       而不是字节码注入点；</li>
 *   <li>与 tooltip 那条路径（{@link FerryBridgeTooltip}）用的是同一套思路，
 *       两条路只有「事件名」不同。</li>
 * </ul>
 *
 * <h2>覆盖范围（实机需比对，别当成等价替换）</h2>
 *
 * <p>{@code ClientChatReceivedEvent} 在客户端<b>收到聊天包</b>时触发，覆盖：
 * <ul>
 *   <li>玩家聊天（{@code ClientChatReceivedEvent.Player}）；</li>
 *   <li>系统消息与动作栏（{@code ClientChatReceivedEvent.System}，
 *       {@code isOverlay()} 区分是否走动作栏）。</li>
 * </ul>
 *
 * <p>它与 {@code ChatComponent.addMessage} 的差别在于：<b>别的模组在客户端直接往聊天栏
 * 塞的消息</b>（不走网络、自己调 {@code addMessage}）不会经过这个事件。
 * 这部分若实机发现漏翻，再单独想办法，不要为此回退到 Mixin。
 *
 * <p><b>不取消事件</b>：只把要显示的那条消息换成中文，其余流程原样放行。
 * 取消会让消息整条不显示，那不是我们要的。
 */
@Mod.EventBusSubscriber(modid = "ferrybridge", bus = Mod.EventBusSubscriber.Bus.MOD, value = Dist.CLIENT)
public final class FerryBridgeChat {

    private static final Logger LOGGER = LoggerFactory.getLogger("FerryBridge");

    /** 本注入点在对外的名字里出现一次即可，统计用的标识。 */
    private static final String INJECTOR_NAME = "FerryBridgeChat#ClientChatReceivedEvent";

    private FerryBridgeChat() {
    }

    @SubscribeEvent
    public static void onClientSetup(FMLClientSetupEvent event) {
        event.enqueueWork(() -> {
            MinecraftForge.EVENT_BUS.addListener(FerryBridgeChat::onChatReceived);
            LOGGER.info("FerryBridge chat hook ready (ClientChatReceivedEvent)");
        });
    }

    /**
     * 收到聊天消息时调用。
     *
     * <p>{@code event.getMessage()} 是稍后会显示在聊天栏的那条 Component；
     * 命中映射表就用 {@code setMessage()} 换掉，未命中就交给采集器记一笔。
     */
    private static void onChatReceived(ClientChatReceivedEvent event) {
        if (event == null) {
            return;
        }
        // 与 tooltip 路径共用同一个开关：HUD 上按 F10 关掉注入时，两条路一起停，
        // 玩家只需要记一个开关。
        if (!FerryBridgeLive.isInjectionOn()) {
            return;
        }
        Component original = event.getMessage();
        if (original == null) {
            return;
        }

        // 事件真的被派发到 == 这条路径活着。HUD 与诊断报告的「生效注入点」靠它统计。
        // （按名字去重，所以这里每条消息都调也无所谓。）
        FerryBridgeLive.markInjectorLive(INJECTOR_NAME);

        String plain = original.getString();
        if (plain == null || plain.isBlank()) {
            return;
        }

        // 刻意不检查映射表是否为空：表为空时查表必然未命中，
        // 而「未命中」正是采集器要的东西 —— 空表的第一轮启动，恰恰最需要采集。
        TranslationStore store = TranslationStore.get();
        String mapped = store == null ? null : store.lookup(plain);
        if (mapped == null || mapped.equals(plain)) {
            // 未命中：命中 0 而 miss 巨大，说明映射表没覆盖到实际内容，
            // 这是排查覆盖率问题的第一手实时数据（看 HUD 即可）。
            FerryBridgeLive.recordMiss();
            // 同时记进采集清单 —— 这就是「大批量翻译」的原料。
            FerryBridgeCollector.record(FerryBridgeCollector.BUCKET_CHAT, plain);
            return;
        }

        // 保留原样式（颜色、斜体等），只换文本。
        MutableComponent translated = Component.literal(mapped);
        translated.setStyle(original.getStyle());
        // 记录一次成功替换，HUD 会实时显示次数与最近命中的文本。
        FerryBridgeLive.recordHit(plain, mapped);
        event.setMessage(translated);
    }
}
