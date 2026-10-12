package com.redreality.ferrybridge.core;

/**
 * Mixin 注入点的中转层。
 *
 * <p><b>【2026-10-12 历史备注】</b>本模组 L1 之后已零 Mixin，替换路径全走
 * Forge 官方事件，因此本类在**运行代码里已经没有调用方**了。保留它有两个原因：
 * 一是 {@code src/reference} 里的 ComponentMixin 仍引用它；二是下面这段
 * 「mixin 包隔离」的坑值得原样留着。新代码不要往这里加东西。
 *
 * <p>Mixin 类是接口/抽象形态，不能持有实例字段；而查表状态在 core 里是单例。
 * 这里只做一次桥接，并放一个重入守卫。
 *
 * <p><b>为什么这个类必须待在 core 包、绝不能挪回 mixin 包</b>
 *（2026-10-10 实机踩坑，游戏直接崩溃）：
 *
 * <p>{@code ferrybridge.mixins.json} 把整个 {@code com.redreality.ferrybridge.mixin}
 * 声明成了 mixin 包。Mixin 框架对 mixin 包有**硬性隔离**：该包下的类只能由
 * 框架自己加载，任何"普通代码"直接引用它们会被拒绝，抛
 * {@link org.spongepowered.asm.mixin.transformer.throwables.IllegalClassLoadError}：
 * <pre>
 *   com.redreality.ferrybridge.mixin.FerryBridgeAccess is in a defined mixin package
 *   com.redreality.ferrybridge.mixin.* owned by ferrybridge.mixins.json
 *   and cannot be referenced directly
 * </pre>
 * 症状极具迷惑性：注入点**其实完全正常**（refmap 修好后 {@code translateSimpleChat}
 * 确实被调用了），但一执行到引用 {@code FerryBridgeAccess} 那行就崩，
 * 玩家看到的只是"进服务器刚收到消息就崩"，看不出跟 mixin 包隔离有关。
 *
 * <p>放在 core 包则完全合法：mixin 类引用外部普通类是允许的（反向才禁止）。
 * 而且 core 是零 MC 依赖层，跨加载器复用也更顺。
 */
public final class FerryBridgeAccess {

    /** 重入守卫。用 ThreadLocal 避免渲染线程与网络线程互相干扰。 */
    public static final ThreadLocal<Boolean> IN_TRANSLATION = ThreadLocal.withInitial(() -> Boolean.FALSE);

    private FerryBridgeAccess() {
    }

    /**
     * 查表。未初始化或未命中返回 null。
     *
     * <p>热路径：映射表为空时 TranslationStore.lookup 立即返回 null，
     * 未命中路径零分配。
     */
    public static String lookup(String plain) {
        TranslationStore store = TranslationStore.get();
        if (store == null) {
            return null;
        }
        return store.lookup(plain);
    }
}