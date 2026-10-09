package com.redreality.ferrybridge.mixin;

import com.redreality.ferrybridge.core.TranslationStore;

/**
 * Mixin 注入点的中转层。
 *
 * <p>Mixin 类是接口/抽象形态，不能持有实例字段；而查表状态在 core 里是单例。
 * 这里只做一次桥接，并放一个重入守卫。
 *
 * <p>重入守卫的必要性：注入点拿到的是"已经渲染过一次"的字符串。若某个玩家的
 * 社区包把中文原文又写进了映射表的英文键里（自己造的包、或二次加工的包），
 * 查表会命中并返回另一个字符串，而那个字符串又可能再次命中。没有守卫就会栈溢出。
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