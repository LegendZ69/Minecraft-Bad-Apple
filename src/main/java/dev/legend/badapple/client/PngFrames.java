package dev.legend.badapple.client;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.util.Objects;
import net.minecraft.client.texture.NativeImage;
import org.lwjgl.system.MemoryStack;

/** Bounded PNG decoding without copying encoded frames onto LWJGL's small stack. */
public final class PngFrames {
    private PngFrames() {}

    public static NativeImage read(byte[] png) throws IOException {
        Objects.requireNonNull(png, "png");
        // The byte[] convenience overload can place the entire encoded image
        // on a fixed-size MemoryStack. Complex source PNGs exceed that capacity.
        // The InputStream overload uses the expandable native-buffer path;
        // an explicit stack scope also bounds any decoder scratch allocations.
        try (MemoryStack ignored = MemoryStack.stackPush();
                ByteArrayInputStream stream = new ByteArrayInputStream(png)) {
            return NativeImage.read(stream);
        }
    }
}
