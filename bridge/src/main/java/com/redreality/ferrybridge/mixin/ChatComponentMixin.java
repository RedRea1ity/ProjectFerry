package com.redreality.ferrybridge.mixin;

import com.redreality.ferrybridge.core.TranslationStore;
import net.minecraft.client.gui.components.ChatComponent;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.MutableComponent;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * 聊天栏文本替换。
 *
 * <p>拦截 addMessage 的原因：聊天栏的 Component 在存入前就会被取一次
 * getString()，所以 ComponentMixin 已能覆盖大部分情形。这里额外拦一次是为了
 * 处理两类 getString() 覆盖不到的情况：
 *
 * <ul>
 *   <li>消息里含 {@code translatable} 组件时，纯文本查表拿不到最终串，
 *       需要在这里展开后再查表；</li>
 *   <li>签名校验消息（1.19.1+ 的 MessageSignature）会把消息体序列化后才渲染，
 *       替换点必须在序列化之前。</li>
 * </ul>
 *
 * <p>require = 0：签名随版本变动明显，缺失时静默跳过聊天栏，其余功能不受影响。
 */
@Mixin(ChatComponent.class)
public abstract class ChatComponentMixin {

    @Inject(method = "addMessage(Lnet/minecraft/network/chat/Component;)V", at = @At("HEAD"), cancellable = true, require = 0)
    private void ferrybridge$translateSimpleChat(Component message, CallbackInfo info) {
        Component translated = ferrybridge$replace(message);
        if (translated != null) {
            // 直接走另一条重载，等价于"把翻译结果当作原始消息"。
            ((ChatComponent) (Object) this).addMessage(translated);
            info.cancel();
        }
    }

    @Inject(
            method = "addMessage(Lnet/minecraft/network/chat/Component;"
                    + "Lnet/minecraft/network/chat/MessageSignature;"
                    + "Lnet/minecraft/client/GuiMessageTag;)V",
            at = @At("HEAD"),
            cancellable = true,
            require = 0)
    private void ferrybridge$translateSignedChat(
            Component message,
            net.minecraft.network.chat.MessageSignature signature,
            net.minecraft.client.GuiMessageTag tag,
            CallbackInfo info) {
        Component translated = ferrybridge$replace(message);
        if (translated != null) {
            // 签名必须原样透传：它是服务端对消息体的校验，替换文本会导致签名失配。
            ((ChatComponent) (Object) this).addMessage(translated, signature, tag);
            info.cancel();
        }
    }

    /**
     * 第三个重载（1.19.2+ 内部用，带 onlyLast）**故意不拦**。
     *
     * <p>它不接收玩家可见的新文本 —— 只是前两个重载之间转发的内部入口，
     * 玩家可见的入口已被上面的两个 @Inject 覆盖。拦它只有纯风险：
     *
     * <ul>
     *   <li>描述符含 {@code GuiMessageTag}，而该类在 1.20.1 位于
     *       {@code net.minecraft.network.chat.GuiMessageTag}
     *       （不在 net.minecraft.client，也不在 gui.components）。
     *       包名写错时报的错自相矛盾："需要 A,B,C / 找到 A,B,C"，
     *       看不出真实原因，极易误判成"方法不存在"。</li>
     *   <li>每多一个注入点，就多一处可能因版本差异失效的签名。</li>
     * </ul>
     *
     * <p>需要覆盖这条路径时，靠 {@code ComponentMixin} 的 {@code getString()}
     * 就够了 —— 它对所有渲染路径生效，包括这个内部转发。
     */

    /**
     * 尝试把一条聊天消息替换为中文。
     *
     * @return 命中则返回新的 Component，未命中返回 null（调用方不改动原流程）
     */
    private static Component ferrybridge$replace(Component message) {
        if (message == null || FerryBridgeAccess.IN_TRANSLATION.get()) {
            return null;
        }
        TranslationStore store = TranslationStore.get();
        if (store == null || store.size() == 0) {
            return null;
        }
        FerryBridgeAccess.IN_TRANSLATION.set(Boolean.TRUE);
        try {
            String plain = message.getString();
            if (plain == null || plain.isBlank()) {
                return null;
            }
            String mapped = store.lookup(plain);
            if (mapped == null || mapped.equals(plain)) {
                return null;
            }
            // 保留原样式（颜色、斜体等），只换文本。
            MutableComponent result = Component.literal(mapped);
            result.setStyle(message.getStyle());
            return result;
        } finally {
            FerryBridgeAccess.IN_TRANSLATION.set(Boolean.FALSE);
        }
    }
}
