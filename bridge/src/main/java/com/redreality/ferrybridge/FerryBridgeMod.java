package com.redreality.ferrybridge;

import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.loading.FMLEnvironment;

@Mod(FerryBridgeMod.MODID)
public class FerryBridgeMod {
    public static final String MODID = "ferrybridge";

    public FerryBridgeMod() {
        if (FMLEnvironment.dist == Dist.CLIENT) {
            ClientHandler.init();
        }
    }
}
