package com.redreality.ferrybridge.mixin;

import net.minecraft.network.chat.Component;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.ModifyVariable;

/**
 * <h2>⚠ 已停用 —— 本注入点当前无法工作，请勿放回 mixins.json</h2>
 *
 * <p><b>状态</b>：源码保留但<b>未在 {@code ferrybridge.mixins.json} 中启用</b>。
 * 强行启用会让游戏 FATAL 启动失败（2026-10-10 三次实测确认）。
 *
 * <p><b>根因</b>：目标类 {@code Component} 是 interface，而 Mixin 0.8.5 下
 * 向 interface 注入 {@code @ModifyVariable} 无解 —— 三条约束互相锁死：
 *
 * <ol>
 *   <li>interface mixin 的方法必须 public（{@code MixinPreProcessorInterface.prepareMethod}），
 *       违反 → 注入点被<b>静默丢弃</b>，游戏照常启动；</li>
 *   <li>注入方法必须 private（{@code MixinApplicatorStandard.checkMethodVisibility}），
 *       违反 → <b>FATAL</b>；与第 1 条互斥；</li>
 *   <li>Mixin 类形态必须与目标类一致（{@code MixinInfo$SubType.validateTarget}），
 *       违反 → <b>FATAL</b>。目标类是 interface，故Mixin 类只能是 interface。</li>
 * </ol>
 *
 * Mixin 文档提到的 {@code INJECTORS_IN_INTERFACE_MIXINS} 开关在0.8.5
 * <b>不存在</b>（{@code featureset} 包为空），那是 Fabric fork 0.8.9+ 才引入的。
 *
 * <p><b>下一步</b>：换目标类 —— {@code MutableComponent} 或 {@code LiteralContents}
 * 都是具体类，上述三条约束全不适用。见 README『已知限制』。
 *
 * <p><b>为什么留着这份文件</b>：下面完整记录了三个方向的坑，
 * 换目标类时这些结论可以直接复用，不必重新踩。
 *
 * <hr>
 *
 * <h2>以下为原始设计（目标类 Component 走不通，仅作设计存档）</h2>
 *
 * <p>在 {@code Component.literal(String)} 的入口做精确替换。
 *
 * <h2>为什么拦这里（而不是 getString / visit）</h2>
 *
 * <p>{@code Component.literal(String)} 是<strong>所有硬编码文本的唯一入口</strong>。
 * boh 那些 {@code §7"One cut is all it takes."} 全部经由它变成
 * {@code LiteralContents}。在入口替换，等于从源头改，后续所有渲染路径
 * （tooltip / 聊天栏 / GUI / 按 T 显示物品名）自动都拿到中文。
 *
 * <h2>踩过的三个坑，逐一说明为什么它们不行</h2>
 *
 * <ol>
 *   <li><b>拦 {@code Component.getString()}</b> —— {@code Component} 是接口，
 *       该方法是 default 方法，Mixin 直接抛
 *       {@code InvalidInterfaceMixinException}，游戏 FATAL 崩溃。
 *       {@code require = 0} 拦不住（属"注入点非法"而非"方法缺失"）。</li>
 *   <li><b>拦 {@code LiteralContents.getString()}</b> —— 方法存在且能注入，
 *       但 tooltip 渲染走的是 {@code visit()}，根本不调它。
 *       表现为「日志显示已加载 58 条映射，tooltip 仍是英文」。</li>
 *   <li><b>拦 {@code LiteralContents.visit()} 的局部变量</b> ——
 *       反编译（javap）后确认方法体是：
 *       <pre>
 *       0: aload_1// consumer
 *       1: aload_0
 *       2: getfield b:String           ← 文本是【字段】，不是局部变量
 *       5: invokeinterface accept(String)
 *       </pre>
 *       局部变量表里<b>根本没有 String</b>（只有 slot0=this、slot1=consumer），
 *       所以 {@code @ModifyVariable(index=1)} 改的是 consumer 对象本身，
 *       类型不匹配被<b>静默忽略</b> —— 不报错，也不生效。</li>
 * </ol>
 *
 * <h2>与资源包的分工</h2>
 *
 * <p>lang 文件里的文本走 {@code TranslatableContents}（即
 * {@code Component.translatable("item.boh.xxx")}），由资源包解决，桥不碰。
 * 两者天然不重叠，不会重复翻译，也就不会覆盖汉化组的成果。
 *
 * <h2>重入守卫</h2>
 *
 * <p>若查表返回的中文恰好也在映射表键里（玩家自建包），没有守卫会死循环。
 */
@Mixin(Component.class)
public interface ComponentMixin {

    /**
     * {@code Component.literal(String)}，obf = {@code a(Ljava/lang/String;)...}。
     *
     * <p>注意 {@code Component} 上有两个 {@code String} 入参的静态方法：
     * <ul>
     *   <li>{@code static Component a(String)}—— {@code literal}，我们要拦的</li>
     *   <li>{@code static MutableComponent b(String)} —— {@code literalMutable}，名字不同不冲突</li>
     * </ul>
     * 因此 {@code method = "a"} 唯一匹配。写完整描述符更保险，
     * 但 obf 化后 {@code Component} 的类型描述符可能变，故只写方法名。
     *
     * <p>{@code @ModifyVariable} 而非 {@code @Inject + cancellable}：
     * 只换字符串、让原方法继续正常构造 {@code LiteralContents}，
     * 对原方法零侵入，也不必 {@code setReturnValue}。
     *
     * <p>{@code index = 0}：静态方法没有 {@code this}，slot 0 就是 String 参数。
     * <p><b>本类必须声明为 abstract class，绝不能是 interface。</b>
     *
     * <p>目标{@code Component} 确实是接口，但「Mixin 类自身是什么形态」是另一件事，
     * 两者无关。已踩过两个方向的坑：
     *
     * <ol>
     *   <li><b>{@code interface} + {@code private static}</b> ——
     *       {@code MixinPreProcessorInterface.prepareMethod} 要求 interface mixin的
     *       所有方法必须 public，于是抛 {@code InvalidInterfaceMixinException}
     *       （"Interface mixin contains a non-public method"），
     *       <b>整个注入点在类加载阶段被丢弃</b>。日志只有一条 ERROR，游戏照常启动，
     *       症状是「聊天框能汉化、tooltip 完全不汉化」，极具迷惑性 ——
     *       因为 {@code ChatComponentMixin} 是 {@code abstract class}，不受该校验影响。</li>
     *   <li><b>改成 {@code public static} 硬碰</b> —— 上一条修好后立刻反向炸：
     *       {@code MixinApplicatorStandard.checkMethodVisibility} 又要求注入方法
     *       必须 private，抛 {@code InvalidMixinException}
     *       （"contains non-private static method"），
     *       这次是 <b>FATAL，游戏直接启动失败</b>。</li>
     * </ol>
     *
     * <p>结论：public/private 这对互斥要求<b>只作用于 interface 形态的 mixin 类</b>。
     * 声明为 {@code abstract class} 后两条校验都不再适用，{@code private static} 可正常注入。
     */
    @ModifyVariable(method = "a", at = @At("HEAD"), index = 0, ordinal = 0, require = 0)
    private static String ferrybridge$translateLiteral(String text) {
        if (text == null || text.isEmpty() || FerryBridgeAccess.IN_TRANSLATION.get()) {
            return text;
        }
        String mapped = FerryBridgeAccess.lookup(text);
        if (mapped == null || mapped.equals(text)) {
            return text;
        }
        FerryBridgeAccess.IN_TRANSLATION.set(Boolean.TRUE);
        try {
            return mapped;
        } finally {
            FerryBridgeAccess.IN_TRANSLATION.set(Boolean.FALSE);
        }
    }
}