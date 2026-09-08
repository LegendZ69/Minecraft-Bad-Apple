package dev.legend.badapple.client;

import com.mojang.blaze3d.systems.RenderSystem;
import dev.legend.badapple.playback.PlaybackEngine;
import java.io.IOException;
import java.util.Iterator;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;
import net.fabricmc.fabric.api.client.rendering.v1.WorldRenderContext;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gl.ShaderProgram;
import net.minecraft.client.render.BufferBuilder;
import net.minecraft.client.render.BufferRenderer;
import net.minecraft.client.render.GameRenderer;
import net.minecraft.client.render.Tessellator;
import net.minecraft.client.render.VertexFormat;
import net.minecraft.client.render.VertexFormats;
import net.minecraft.client.texture.NativeImage;
import net.minecraft.client.texture.NativeImageBackedTexture;
import net.minecraft.client.util.math.MatrixStack;
import net.minecraft.client.world.ClientWorld;
import net.minecraft.util.math.Vec3d;
import org.joml.Matrix4f;
import org.lwjgl.opengl.GL11;
import org.lwjgl.opengl.GL13;
import org.lwjgl.opengl.GL14;
import org.lwjgl.opengl.GL20;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * A native-resolution video texture on a vertical plane in the world.
 *
 * <p>Call {@link #render} from WorldRenderEvents.LAST, on the render thread. This
 * is a client-side screen: its pixels are texture samples, not placed blocks.
 * PNG decoding happens on one daemon thread; all GPU work stays on the render
 * thread. Close the screen before closing its PlaybackEngine.
 */
public final class MovieScreen implements AutoCloseable {
    private static final Logger LOGGER = LoggerFactory.getLogger("badapple");
    private final PlaybackEngine engine;
    private final FrameDecoder decoder;
    private NativeImageBackedTexture texture;
    private ClientWorld placedWorld;
    private Vec3d center;
    private Vec3d right;
    private double width;
    private double height;
    private volatile boolean closed;
    private String renderError;

    public MovieScreen(PlaybackEngine engine) {
        this.engine = Objects.requireNonNull(engine, "engine");
        this.decoder = new FrameDecoder(engine);
    }

    /**
     * Places the screen ahead, with its bottom one block above the player's
     * feet, and aims the camera at its center without changing the yaw.
     */
    public void place(MinecraftClient client, double widthBlocks) {
        if (closed) {
            throw new IllegalStateException("The video screen is closed");
        }
        if (client.player == null || client.world == null) {
            throw new IllegalStateException("Join a world before placing the screen");
        }
        if (!Double.isFinite(widthBlocks) || widthBlocks <= 0) {
            throw new IllegalArgumentException("Screen width must be finite and greater than zero");
        }
        double yaw = Math.toRadians(client.player.getYaw());
        Vec3d forward = new Vec3d(-Math.sin(yaw), 0, Math.cos(yaw));
        right = new Vec3d(-forward.z, 0, forward.x);
        width = widthBlocks;
        height = widthBlocks * ((double) engine.metadata().height() / engine.metadata().width());
        double distance = widthBlocks * 0.9;
        center = new Vec3d(client.player.getX(), client.player.getY() + 1 + height * 0.5,
                client.player.getZ()).add(forward.multiply(distance));
        double rise = center.y - client.player.getEyePos().y;
        client.player.setPitch((float) -Math.toDegrees(Math.atan2(rise, distance)));
        placedWorld = client.world;
    }

    /** Returns a terminal decoding/rendering error, or null while healthy. */
    public String error() {
        return renderError != null ? renderError : decoder.error();
    }

    public void render(WorldRenderContext context) {
        if (closed || center == null || context.world() != placedWorld || renderError != null) {
            return;
        }
        RenderSystem.assertOnRenderThread();
        MatrixStack matrices = context.matrixStack();
        if (matrices == null) {
            return;
        }

        // Ask for the timeline's current frame, not the previous render's frame.
        // A ready newer frame replaces a late frame as soon as it becomes due.
        DecodedFrame frame = decoder.poll(engine.update());
        if (frame == null && texture == null) {
            return;
        }

        SavedRenderState state = new SavedRenderState();
        try {
            if (frame != null) {
                try (NativeImage pixels = frame.image()) {
                    if (texture == null) {
                        int limit = GL11.glGetInteger(GL11.GL_MAX_TEXTURE_SIZE);
                        if (pixels.getWidth() > limit || pixels.getHeight() > limit) {
                            throw new IllegalArgumentException("Video resolution exceeds this GPU's texture limit: " + limit);
                        }
                        texture = new NativeImageBackedTexture(pixels.getWidth(), pixels.getHeight(), false);
                    }
                    // Reuse one GPU allocation. The texture owns its own image,
                    // so the decoded frame can always be freed in this scope.
                    Objects.requireNonNull(texture.getImage()).copyFrom(pixels);
                    texture.upload();
                    texture.setFilter(false, false);
                }
            }

            RenderSystem.enableDepthTest();
            RenderSystem.depthFunc(GL11.GL_LEQUAL);
            RenderSystem.depthMask(true);
            RenderSystem.disableBlend();
            RenderSystem.disableCull();
            RenderSystem.setShader(GameRenderer::getPositionTexColorProgram);
            RenderSystem.setShaderTexture(0, texture.getGlId());
            RenderSystem.setShaderColor(1, 1, 1, 1);
            RenderSystem.setShaderFogStart(Float.MAX_VALUE);
            RenderSystem.setShaderFogEnd(Float.MAX_VALUE);

            Vec3d camera = context.camera().getPos();
            matrices.push();
            try {
                matrices.translate(center.x - camera.x, center.y - camera.y, center.z - camera.z);
                Matrix4f matrix = matrices.peek().getPositionMatrix();
                float rx = (float) (right.x * width * 0.5);
                float rz = (float) (right.z * width * 0.5);
                float halfHeight = (float) (height * 0.5);
                BufferBuilder vertices = Tessellator.getInstance().begin(
                        VertexFormat.DrawMode.QUADS, VertexFormats.POSITION_TEXTURE_COLOR);
                // PNG row zero is the top row; v=0 therefore belongs at the top.
                vertices.vertex(matrix, -rx, halfHeight, -rz).texture(0, 0).color(255, 255, 255, 255);
                vertices.vertex(matrix, -rx, -halfHeight, -rz).texture(0, 1).color(255, 255, 255, 255);
                vertices.vertex(matrix, rx, -halfHeight, rz).texture(1, 1).color(255, 255, 255, 255);
                vertices.vertex(matrix, rx, halfHeight, rz).texture(1, 0).color(255, 255, 255, 255);
                BufferRenderer.drawWithGlobalProgram(vertices.end());
            } finally {
                matrices.pop();
            }
        } catch (RuntimeException exception) {
            renderError = "Video screen: " + exception.getMessage();
            decoder.close();
        } finally {
            state.restore();
        }
    }

    @Override
    public void close() {
        if (closed) {
            return;
        }
        closed = true;
        decoder.close();
        placedWorld = null;
        NativeImageBackedTexture retired = texture;
        texture = null;
        if (retired != null) {
            Runnable release = () -> {
                retired.close();
                retired.clearGlId();
            };
            if (RenderSystem.isOnRenderThread()) {
                release.run();
            } else {
                RenderSystem.recordRenderCall(release::run);
            }
        }
    }

    private record DecodedFrame(int index, NativeImage image) {}

    /**
     * One worker and at most six ready images. There is no executor backlog:
     * after any seek the worker immediately selects from the new time window.
     */
    private static final class FrameDecoder implements AutoCloseable {
        private static final int READY_LIMIT = 6;
        private final PlaybackEngine engine;
        private final TreeMap<Integer, NativeImage> ready = new TreeMap<>();
        private final Thread worker;
        private final int frameCount;
        private int desired;
        private int delivered = -1;
        private long generation;
        private boolean closed;
        private String error;

        FrameDecoder(PlaybackEngine engine) {
            this.engine = engine;
            this.frameCount = engine.metadata().frameCount();
            worker = new Thread(this::decode, "badapple-frame-decoder");
            worker.setDaemon(true);
            worker.start();
        }

        synchronized String error() {
            if (!closed && error == null && !worker.isAlive()) {
                error = "Frame decoder stopped unexpectedly";
            }
            return error;
        }

        synchronized DecodedFrame poll(int requested) {
            if (closed) {
                return null;
            }
            int target = Math.max(0, Math.min(frameCount - 1, requested));
            if (target < desired) {
                // Only a rewind or loop invalidates in-flight work. On a
                // forward jump, retain the newest due image so a low render
                // rate cannot repeatedly discard every completed decode.
                generation++;
                clearReady();
                delivered = -1;
            }
            desired = target;
            Map.Entry<Integer, NativeImage> due = ready.floorEntry(target);
            DecodedFrame result = null;
            if (due != null && due.getKey() > delivered) {
                delivered = due.getKey();
                result = new DecodedFrame(delivered, ready.remove(delivered));
            }
            Iterator<Map.Entry<Integer, NativeImage>> iterator = ready.entrySet().iterator();
            while (iterator.hasNext()) {
                Map.Entry<Integer, NativeImage> entry = iterator.next();
                if (entry.getKey() <= delivered || entry.getKey() >= desired + READY_LIMIT) {
                    entry.getValue().close();
                    iterator.remove();
                }
            }
            notifyAll();
            return result;
        }

        private synchronized int nextFrame() throws InterruptedException {
            while (!closed && error == null) {
                int end = Math.min(frameCount, desired + READY_LIMIT);
                for (int candidate = desired; candidate < end; candidate++) {
                    if (candidate > delivered && !ready.containsKey(candidate)) {
                        return candidate;
                    }
                }
                wait();
            }
            return -1;
        }

        private void decode() {
            while (true) {
                int index;
                long jobGeneration;
                try {
                    synchronized (this) {
                        index = nextFrame();
                        if (index < 0) {
                            return;
                        }
                        jobGeneration = generation;
                    }
                } catch (InterruptedException exception) {
                    Thread.currentThread().interrupt();
                    return;
                }

                NativeImage image = null;
                try {
                    image = PngFrames.read(engine.archive().readFrame(index));
                    if (image.getWidth() != engine.metadata().width()
                            || image.getHeight() != engine.metadata().height()) {
                        throw new IOException("Frame " + index + " has unexpected dimensions");
                    }
                    synchronized (this) {
                        if (!closed && generation == jobGeneration && index > delivered
                                && index < desired + READY_LIMIT) {
                            // Keep a late completed frame usable: discarding every
                            // late decode would freeze forever on a slower machine.
                            NativeImage replaced = ready.put(index, image);
                            image = null;
                            if (replaced != null) {
                                replaced.close();
                            }
                            while (ready.size() > READY_LIMIT) {
                                ready.pollFirstEntry().getValue().close();
                            }
                        }
                    }
                } catch (IOException | RuntimeException exception) {
                    synchronized (this) {
                        if (!closed && generation == jobGeneration) {
                            error = "Could not decode frame " + index + ": " + exception.getMessage();
                            clearReady();
                            return;
                        }
                    }
                } catch (OutOfMemoryError | LinkageError fatal) {
                    // A dead daemon must not leave an apparently healthy frozen
                    // screen. Publish terminal failure once; never retry OOM.
                    synchronized (this) {
                        if (!closed) {
                            error = "Frame decoder terminated at frame " + index + ": "
                                    + fatal.getClass().getSimpleName() + ": " + fatal.getMessage();
                            clearReady();
                        }
                    }
                    LOGGER.error("Video frame decoder terminated at frame {}", index, fatal);
                    return;
                } finally {
                    // Includes seeks, close during decode, malformed images and
                    // canceled work. Ownership moves only after ready.put().
                    if (image != null) {
                        image.close();
                    }
                }
            }
        }

        private void clearReady() {
            ready.values().forEach(NativeImage::close);
            ready.clear();
        }

        @Override
        public synchronized void close() {
            if (!closed) {
                closed = true;
                generation++;
                clearReady();
                notifyAll();
                worker.interrupt();
            }
        }
    }

    /** Restore the caller's render state, including texture upload pixel stores. */
    private static final class SavedRenderState {
        private final ShaderProgram shader = RenderSystem.getShader();
        private final float[] color = RenderSystem.getShaderColor().clone();
        private final float fogStart = RenderSystem.getShaderFogStart();
        private final float fogEnd = RenderSystem.getShaderFogEnd();
        private final int shaderTexture = RenderSystem.getShaderTexture(0);
        private final boolean blend = GL11.glIsEnabled(GL11.GL_BLEND);
        private final int blendSourceRgb = GL11.glGetInteger(GL14.GL_BLEND_SRC_RGB);
        private final int blendDestRgb = GL11.glGetInteger(GL14.GL_BLEND_DST_RGB);
        private final int blendSourceAlpha = GL11.glGetInteger(GL14.GL_BLEND_SRC_ALPHA);
        private final int blendDestAlpha = GL11.glGetInteger(GL14.GL_BLEND_DST_ALPHA);
        private final int blendEquationRgb = GL11.glGetInteger(GL20.GL_BLEND_EQUATION_RGB);
        private final int blendEquationAlpha = GL11.glGetInteger(GL20.GL_BLEND_EQUATION_ALPHA);
        private final boolean cull = GL11.glIsEnabled(GL11.GL_CULL_FACE);
        private final boolean depth = GL11.glIsEnabled(GL11.GL_DEPTH_TEST);
        private final boolean depthMask = GL11.glGetBoolean(GL11.GL_DEPTH_WRITEMASK);
        private final int depthFunction = GL11.glGetInteger(GL11.GL_DEPTH_FUNC);
        private final int activeTexture = GL11.glGetInteger(GL13.GL_ACTIVE_TEXTURE);
        private final int unpackAlignment = GL11.glGetInteger(GL11.GL_UNPACK_ALIGNMENT);
        private final int unpackRowLength = GL11.glGetInteger(GL11.GL_UNPACK_ROW_LENGTH);
        private final int unpackSkipPixels = GL11.glGetInteger(GL11.GL_UNPACK_SKIP_PIXELS);
        private final int unpackSkipRows = GL11.glGetInteger(GL11.GL_UNPACK_SKIP_ROWS);
        private final int textureBinding;

        SavedRenderState() {
            RenderSystem.activeTexture(GL13.GL_TEXTURE0);
            textureBinding = GL11.glGetInteger(GL11.GL_TEXTURE_BINDING_2D);
        }

        void restore() {
            RenderSystem.setShader(() -> shader);
            RenderSystem.setShaderTexture(0, shaderTexture);
            RenderSystem.setShaderColor(color[0], color[1], color[2], color[3]);
            RenderSystem.setShaderFogStart(fogStart);
            RenderSystem.setShaderFogEnd(fogEnd);
            RenderSystem.blendFuncSeparate(blendSourceRgb, blendDestRgb, blendSourceAlpha, blendDestAlpha);
            GL20.glBlendEquationSeparate(blendEquationRgb, blendEquationAlpha);
            if (blend) RenderSystem.enableBlend(); else RenderSystem.disableBlend();
            if (cull) RenderSystem.enableCull(); else RenderSystem.disableCull();
            if (depth) RenderSystem.enableDepthTest(); else RenderSystem.disableDepthTest();
            RenderSystem.depthMask(depthMask);
            RenderSystem.depthFunc(depthFunction);
            RenderSystem.pixelStore(GL11.GL_UNPACK_ALIGNMENT, unpackAlignment);
            RenderSystem.pixelStore(GL11.GL_UNPACK_ROW_LENGTH, unpackRowLength);
            RenderSystem.pixelStore(GL11.GL_UNPACK_SKIP_PIXELS, unpackSkipPixels);
            RenderSystem.pixelStore(GL11.GL_UNPACK_SKIP_ROWS, unpackSkipRows);
            RenderSystem.activeTexture(GL13.GL_TEXTURE0);
            RenderSystem.bindTexture(textureBinding);
            RenderSystem.activeTexture(activeTexture);
        }
    }
}
