package dev.legend.badapple.smoke;

import com.google.gson.GsonBuilder;
import dev.legend.badapple.client.BadAppleClient;
import dev.legend.badapple.client.MovieScreen;
import dev.legend.badapple.playback.PlaybackEngine;
import java.lang.reflect.Field;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import javax.sound.sampled.AudioSystem;
import javax.sound.sampled.Clip;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.command.v2.ClientCommandManager;
import net.fabricmc.fabric.api.client.command.v2.FabricClientCommandSource;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.rendering.v1.WorldRenderEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gui.Element;
import net.minecraft.client.gui.ParentElement;
import net.minecraft.client.gui.screen.TitleScreen;
import net.minecraft.client.gui.screen.world.CreateWorldScreen;
import net.minecraft.client.gui.screen.world.WorldCreator;
import net.minecraft.client.gui.widget.ButtonWidget;
import net.minecraft.client.texture.NativeImage;
import net.minecraft.client.texture.NativeImageBackedTexture;
import net.minecraft.client.util.ScreenshotRecorder;
import net.minecraft.text.Text;
import net.minecraft.world.Difficulty;
import net.minecraft.world.gen.WorldPresets;
import org.lwjgl.opengl.GL11;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Isolated, opt-in development client test, excluded from the shipped mod.
 * Creates its own normal single-player world; never runs a dedicated server or
 * accepts an EULA, license, account, or multiplayer-security dialog.
 */
public final class CloudSmokeClient implements ClientModInitializer {
    private static final Logger LOGGER = LoggerFactory.getLogger("badapple-cloud-smoke");
    private final Map<String, Object> report = new LinkedHashMap<>();
    private final List<String> assertions = new ArrayList<>();
    private final List<Map<String, Object>> checkpoints = new ArrayList<>();
    private final List<Map<String, Object>> commands = new ArrayList<>();
    private Path output;
    private BadAppleClient mod;
    private long started;
    private long readyAfter;
    private int stage;
    private boolean finished;
    private String capturePending;
    private double pausedPosition;
    private long audioPosition;
    private Clip initialClip;
    private String latestWarning;
    private boolean referenceMode;

    @Override
    public void onInitializeClient() {
        if (!Boolean.getBoolean("badapple.smoke")) return;
        if (!FabricLoader.getInstance().isDevelopmentEnvironment()) {
            throw new IllegalStateException("Cloud smoke tests require an isolated development instance");
        }
        output = Path.of(Objects.requireNonNull(System.getProperty("badapple.smoke.output"),
                "Set badapple.smoke.output to an isolated evidence directory")).toAbsolutePath();
        started = System.nanoTime();
        referenceMode = Boolean.getBoolean("badapple.smoke.reference");
        report.put("schemaVersion", 1);
        report.put("startedUtc", Instant.now().toString());
        report.put("referenceMode", referenceMode);
        report.put("media", referenceMode ? "User-selected reference archive; provenance is documented separately"
                : "Synthetic deterministic test fixture; NOT Bad Apple reference media");
        report.put("minecraft", "1.21.1");
        report.put("assertions", assertions);
        report.put("checkpoints", checkpoints);
        report.put("commands", commands);
        report.put("audioMixers", Arrays.stream(AudioSystem.getMixerInfo())
                .map(info -> info.getName() + " / " + info.getDescription()).toList());
        ClientTickEvents.END_CLIENT_TICK.register(client -> {
            if (finished) return;
            try {
                if ((System.nanoTime() - started) / 1_000_000_000L > 300) {
                    throw new AssertionError("Smoke timeout at stage " + stage + "; currentScreen="
                            + (client.currentScreen == null ? "none" : client.currentScreen.getClass().getName()));
                }
                if (System.nanoTime() < readyAfter) return;
                tick(client);
            } catch (Throwable error) {
                finish(client, error);
            }
        });
        // END runs after the production mod's LAST callback has drawn its quad.
        WorldRenderEvents.END.register(context -> {
            if (finished || capturePending == null) return;
            try {
                capture(MinecraftClient.getInstance());
            } catch (Throwable error) {
                finish(MinecraftClient.getInstance(), error);
            }
        });
    }

    private void tick(MinecraftClient client) throws Exception {
        switch (stage) {
            case 0 -> {
                if (!(client.currentScreen instanceof TitleScreen)) return;
                Files.createDirectories(output);
                mod = FabricLoader.getInstance().getEntrypoints("client", ClientModInitializer.class).stream()
                        .filter(BadAppleClient.class::isInstance).map(BadAppleClient.class::cast)
                        .findFirst().orElseThrow();
                require(Files.isRegularFile(FabricLoader.getInstance().getGameDir()
                        .resolve("badapple/" + archiveName())), "Selected smoke archive exists");
                report.put("renderer", GL11.glGetString(GL11.GL_RENDERER));
                report.put("openGlVersion", GL11.glGetString(GL11.GL_VERSION));
                CreateWorldScreen.create(client, client.currentScreen);
                advance(1, 0);
            }
            case 1 -> {
                if (!(client.currentScreen instanceof CreateWorldScreen create)) return;
                WorldCreator creator = create.getWorldCreator();
                creator.setWorldName("Bad Apple Cloud Smoke " + System.currentTimeMillis());
                creator.setSeed("48036030");
                creator.setGameMode(WorldCreator.Mode.CREATIVE);
                creator.setDifficulty(Difficulty.PEACEFUL);
                creator.setCheatsEnabled(true);
                creator.setGenerateStructures(false);
                creator.getNormalWorldTypes().stream()
                        .filter(type -> type.preset() != null && type.preset().matchesKey(WorldPresets.FLAT))
                        .findFirst().ifPresent(creator::setWorldType);
                ButtonWidget button = findCreateButton(create);
                require(button != null && button.active, "Normal single-player Create World button available");
                button.onPress();
                advance(2, 500);
            }
            case 2 -> {
                if (client.world == null || client.player == null || client.getServer() == null
                        || client.currentScreen != null || ClientCommandManager.getActiveDispatcher() == null) return;
                client.options.hudHidden = true;
                var server = client.getServer();
                server.execute(() -> {
                    for (String command : List.of("gamemode spectator @a", "tp @a 0 200 0 0 0",
                            "time set day", "weather clear", "gamerule doDaylightCycle false")) {
                        server.getCommandManager().executeWithPrefix(server.getCommandSource(), command);
                    }
                });
                advance(3, 2000);
            }
            case 3 -> {
                require(client.player != null && Math.abs(client.player.getY() - 200) < 1,
                        "Dedicated synthetic-world spectator camera positioned above terrain");
                command(client, "badapple load " + archiveName());
                advance(4, 0);
            }
            case 4 -> {
                if (engine() == null || screen() == null) return;
                report.put("sourceWidth", engine().width());
                report.put("sourceHeight", engine().height());
                report.put("sourceFrameCount", engine().metadata().frameCount());
                report.put("sourceDurationSeconds", engine().durationSeconds());
                if (referenceMode) {
                    require(engine().durationSeconds() > 121, "Reference duration covers 30, 60 and 120 second checkpoints");
                } else {
                    require(engine().metadata().width() == 480 && engine().metadata().height() == 360,
                            "Native synthetic texture dimensions are 480x360");
                    require(engine().metadata().frameCount() == 180, "Synthetic archive contains all 180 frames");
                }
                initialClip = field(engine(), "audio", Clip.class);
                report.put("javaSoundClipOpened", initialClip != null && initialClip.isOpen());
                latestWarning = engine().warning();
                command(client, "badapple pause");
                pausedPosition = engine().positionSeconds();
                audioPosition = initialClip == null ? -1 : initialClip.getMicrosecondPosition();
                advance(5, 650);
            }
            case 5 -> {
                require(!engine().isPlaying() && Math.abs(engine().positionSeconds() - pausedPosition) < 0.002,
                        "Pause freezes playback timeline for at least 650ms");
                if (initialClip != null) {
                    require(!initialClip.isRunning() && Math.abs(initialClip.getMicrosecondPosition() - audioPosition) < 50000,
                            "Pause stops Java Sound clip and freezes audio position");
                }
                command(client, "badapple seek " + (referenceMode ? "30" : "0"));
                requestCapture(referenceMode ? "01-reference-30-seconds" : "01-first-frame");
                advance(6, 0);
            }
            case 6 -> {
                if (capturePending != null) return;
                command(client, "badapple resume");
                audioPosition = initialClip == null ? -1 : initialClip.getMicrosecondPosition();
                advance(7, 1100);
            }
            case 7 -> {
                require(engine().isPlaying() && engine().positionSeconds() > (referenceMode ? 30.5 : 0.5),
                        "Resume advances playback clock and frame selection");
                report.put("audioClockAdvanced", initialClip != null
                        && initialClip.getMicrosecondPosition() > audioPosition + 200000);
                if (Boolean.getBoolean("badapple.smoke.requireAudio")) {
                    require(Boolean.TRUE.equals(report.get("audioClockAdvanced")) && engine().warning() == null,
                            "Real Java Sound output clock advances without fallback");
                }
                command(client, "badapple pause");
                command(client, "badapple seek " + (referenceMode ? "60" : "3"));
                require(Math.abs(engine().positionSeconds() - (referenceMode ? 60 : 3)) < 0.002,
                        "Seek selects exact requested source timestamp");
                if (!referenceMode) require(engine().currentFrameIndex() == 90, "Seek to 3 seconds selects source frame 90");
                requestCapture(referenceMode ? "02-reference-60-seconds" : "02-seek-frame-90");
                advance(8, 0);
            }
            case 8 -> {
                if (capturePending != null) return;
                command(client, "badapple loop true");
                command(client, "badapple seek " + (engine().durationSeconds() - 0.4));
                command(client, "badapple resume");
                advance(9, 1200);
            }
            case 9 -> {
                require(engine().isLooping() && engine().isPlaying() && engine().positionSeconds() < 4,
                        "Loop crosses duration and restarts video/audio timeline");
                command(client, "badapple pause");
                command(client, "badapple seek " + (referenceMode ? "120" : "1"));
                command(client, "badapple place native");
                require(field(screen(), "width", Double.class) == (double) engine().width()
                                && field(screen(), "height", Double.class) == (double) engine().height(),
                        "Native placement uses exactly one world block per source pixel");
                requestCapture(referenceMode ? "03-reference-native-120-seconds" : "03-native-one-pixel-per-block");
                advance(10, 0);
            }
            case 10 -> {
                if (capturePending != null) return;
                command(client, "badapple stop");
                require(!engine().isPlaying() && engine().positionSeconds() == 0 && screen() != null,
                        "Stop resets timeline and retains screen");
                command(client, "badapple restart");
                require(engine().isPlaying(), "Restart resumes from beginning");
                command(client, "badapple status");
                command(client, "badapple unload");
                require(engine() == null && screen() == null, "Unload releases movie and screen");
                require(initialClip == null || !initialClip.isOpen(), "Unload closes existing Java Sound clip");
                command(client, "badapple load " + archiveName());
                advance(11, 0);
            }
            case 11 -> {
                if (engine() == null || screen() == null) return;
                command(client, "badapple pause");
                command(client, "badapple seek " + (referenceMode ? "60" : "2"));
                requestCapture(referenceMode ? "04-reference-reloaded-60-seconds" : "04-reloaded-frame-60");
                advance(12, 0);
            }
            case 12 -> {
                if (capturePending != null) return;
                if (referenceMode) {
                    require(checkpoints.stream().anyMatch(item -> {
                        Map<?, ?> view = (Map<?, ?>) item.get("framebuffer");
                        return ((Number) view.get("luminanceRange")).intValue() >= 128
                                && ((Number) view.get("darkPixels")).intValue() >= 100
                                && ((Number) view.get("lightPixels")).intValue() >= 100;
                    }), "At least one reference checkpoint visibly renders non-uniform grayscale detail");
                }
                require(referenceMode ? Math.abs(engine().positionSeconds() - 60) < 0.002
                        : engine().currentFrameIndex() == 60, "Reload creates working decoder after cleanup");
                latestWarning = engine().warning();
                command(client, "badapple unload");
                finish(client, null);
            }
            default -> throw new AssertionError("Unknown smoke stage " + stage);
        }
    }

    private static ButtonWidget findCreateButton(ParentElement parent) {
        String label = Text.translatable("selectWorld.create").getString();
        for (Element element : parent.children()) {
            if (element instanceof ButtonWidget button && button.getMessage().getString().equals(label)) return button;
            if (element instanceof ParentElement child) {
                ButtonWidget result = findCreateButton(child);
                if (result != null) return result;
            }
        }
        return null;
    }

    private void command(MinecraftClient client, String command) throws Exception {
        int result = Objects.requireNonNull(ClientCommandManager.getActiveDispatcher()).execute(command,
                (FabricClientCommandSource) Objects.requireNonNull(client.getNetworkHandler()).getCommandSource());
        Map<String, Object> observation = new LinkedHashMap<>();
        observation.put("command", "/" + command);
        observation.put("result", result);
        observation.put("elapsedSeconds", (System.nanoTime() - started) / 1e9);
        commands.add(observation);
        require(result == 1, "Command accepted: /" + command);
    }

    private void requestCapture(String name) {
        capturePending = name;
    }

    private void capture(MinecraftClient client) throws Exception {
        MovieScreen screen = screen();
        PlaybackEngine engine = engine();
        if (screen == null || engine == null) return;
        require(screen.error() == null, "No movie rendering/decoding error");
        NativeImageBackedTexture texture = field(screen, "texture", NativeImageBackedTexture.class);
        if (texture == null) return;
        Object decoder = field(screen, "decoder", Object.class);
        int delivered;
        synchronized (decoder) {
            delivered = field(decoder, "delivered", Integer.class);
        }
        int expectedFrame = engine.currentFrameIndex();
        if (delivered != expectedFrame) return;
        Map<String, Object> checkpoint = new LinkedHashMap<>();
        checkpoint.put("name", capturePending);
        checkpoint.put("frame", delivered);
        checkpoint.put("seconds", engine.positionSeconds());
        checkpoint.put("worldWidth", field(screen, "width", Double.class));
        checkpoint.put("worldHeight", field(screen, "height", Double.class));
        int previousTexture = GL11.glGetInteger(GL11.GL_TEXTURE_BINDING_2D);
        try (NativeImage source = NativeImage.read(engine.archive().readFrame(delivered));
                NativeImage gpu = new NativeImage(engine.width(), engine.height(), false)) {
            GL11.glBindTexture(GL11.GL_TEXTURE_2D, texture.getGlId());
            gpu.loadFromTextureImage(0, false);
            require(GL11.glGetTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_MIN_FILTER) == GL11.GL_NEAREST
                            && GL11.glGetTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_MAG_FILTER) == GL11.GL_NEAREST,
                    capturePending + ": nearest-neighbor GPU sampling");
            for (int y = 0; y < source.getHeight(); y++) {
                for (int x = 0; x < source.getWidth(); x++) {
                    if (source.getColor(x, y) != gpu.getColor(x, y)) {
                        throw new AssertionError(capturePending + ": GPU differs from source at " + x + "," + y);
                    }
                }
            }
            checkpoint.put("gpuPixelsCompared", source.getWidth() * source.getHeight());
            checkpoint.put("gpuExactMatch", true);
            gpu.writeTo(output.resolve(capturePending + "-gpu.png"));
        } finally {
            GL11.glBindTexture(GL11.GL_TEXTURE_2D, previousTexture);
        }
        try (NativeImage framebuffer = ScreenshotRecorder.takeScreenshot(client.getFramebuffer())) {
            framebuffer.writeTo(output.resolve(capturePending + "-world.png"));
            checkpoint.put("framebuffer", referenceMode ? verifyReferenceFrame(framebuffer) : verifyCorners(framebuffer));
        }
        checkpoints.add(checkpoint);
        LOGGER.info("BADAPPLE_SMOKE_CHECKPOINT {}", capturePending);
        capturePending = null;
    }

    /** Finds actual rendered corner patches, proving quad visibility and orientation. */
    private Map<String, Object> verifyCorners(NativeImage framebuffer) {
        int[][] colors = {{255, 0, 0}, {0, 255, 0}, {0, 0, 255}, {255, 255, 0}};
        String[] names = {"topLeftRed", "topRightGreen", "bottomLeftBlue", "bottomRightYellow"};
        long[] counts = new long[4];
        long[] sumX = new long[4];
        long[] sumY = new long[4];
        for (int y = 0; y < framebuffer.getHeight(); y++) {
            for (int x = 0; x < framebuffer.getWidth(); x++) {
                int pixel = framebuffer.getColor(x, y);
                int r = pixel & 255;
                int g = (pixel >>> 8) & 255;
                int b = (pixel >>> 16) & 255;
                for (int c = 0; c < colors.length; c++) {
                    if (Math.abs(r - colors[c][0]) <= 3 && Math.abs(g - colors[c][1]) <= 3
                            && Math.abs(b - colors[c][2]) <= 3) {
                        counts[c]++;
                        sumX[c] += x;
                        sumY[c] += y;
                    }
                }
            }
        }
        Map<String, Object> result = new LinkedHashMap<>();
        double[] x = new double[4];
        double[] y = new double[4];
        for (int c = 0; c < colors.length; c++) {
            require(counts[c] >= 200, capturePending + ": visible framebuffer corner " + names[c]);
            x[c] = (double) sumX[c] / counts[c];
            y[c] = (double) sumY[c] / counts[c];
            result.put(names[c], Map.of("pixels", counts[c], "centerX", x[c], "centerY", y[c]));
        }
        require(x[0] + 50 < x[1] && x[2] + 50 < x[3]
                        && y[0] + 50 < y[2] && y[1] + 50 < y[3],
                capturePending + ": framebuffer proves correct top/bottom and left/right orientation");
        result.put("width", framebuffer.getWidth());
        result.put("height", framebuffer.getHeight());
        return result;
    }

    /** The centered interior must visibly contain a non-uniform grayscale movie. */
    private Map<String, Object> verifyReferenceFrame(NativeImage framebuffer) {
        int left = framebuffer.getWidth() * 35 / 100;
        int right = framebuffer.getWidth() * 65 / 100;
        int top = framebuffer.getHeight() * 35 / 100;
        int bottom = framebuffer.getHeight() * 65 / 100;
        int gray = 0;
        int minimum = 255;
        int maximum = 0;
        int dark = 0;
        int light = 0;
        for (int y = top; y < bottom; y++) {
            for (int x = left; x < right; x++) {
                int color = framebuffer.getColor(x, y);
                int r = color & 255;
                int g = (color >>> 8) & 255;
                int b = (color >>> 16) & 255;
                if (Math.max(r, Math.max(g, b)) - Math.min(r, Math.min(g, b)) <= 4) {
                    gray++;
                    minimum = Math.min(minimum, r);
                    maximum = Math.max(maximum, r);
                    if (r < 64) dark++;
                    if (r > 192) light++;
                }
            }
        }
        int total = (right - left) * (bottom - top);
        double fraction = (double) gray / total;
        require(fraction >= 0.95, capturePending + ": rendered central movie area is grayscale");
        // A faithful source frame may legitimately be entirely black or white.
        // Record contrast here and require it across the checkpoint set, not in
        // every image. Synthetic corner tests independently prove orientation.
        return Map.of("width", framebuffer.getWidth(), "height", framebuffer.getHeight(),
                "roi", Map.of("left", left, "right", right, "top", top, "bottom", bottom),
                "grayscaleFraction", fraction, "luminanceRange", maximum - minimum,
                "darkPixels", dark, "lightPixels", light);
    }

    private String archiveName() {
        return referenceMode ? "reference.bapple" : "smoke.bapple";
    }

    private void advance(int nextStage, long delayMillis) {
        stage = nextStage;
        readyAfter = System.nanoTime() + delayMillis * 1_000_000L;
        LOGGER.info("BADAPPLE_SMOKE_STAGE {}", stage);
    }

    private void require(boolean condition, String label) {
        if (!condition) throw new AssertionError(label);
        if (!assertions.contains(label)) assertions.add(label);
    }

    private PlaybackEngine engine() throws ReflectiveOperationException {
        return field(mod, "engine", PlaybackEngine.class);
    }

    private MovieScreen screen() throws ReflectiveOperationException {
        return field(mod, "screen", MovieScreen.class);
    }

    // Reflection is restricted to this project's fields, never Minecraft internals.
    private static <T> T field(Object owner, String name, Class<T> type) throws ReflectiveOperationException {
        Field field = owner.getClass().getDeclaredField(name);
        field.setAccessible(true);
        return type.cast(field.get(owner));
    }

    private void finish(MinecraftClient client, Throwable failure) {
        if (finished) return;
        finished = true;
        report.put("status", failure == null ? "passed" : "failed");
        report.put("finishedUtc", Instant.now().toString());
        report.put("elapsedSeconds", (System.nanoTime() - started) / 1e9);
        report.put("finalStage", stage);
        report.put("audioWarning", latestWarning);
        report.put("physicalAudioVerified", false);
        if (failure != null) {
            report.put("failure", failure.toString());
            LOGGER.error("BADAPPLE_SMOKE_FAILED", failure);
        }
        try {
            Files.createDirectories(output);
            Files.writeString(output.resolve("smoke-report.json"), new GsonBuilder().setPrettyPrinting()
                    .serializeNulls().create().toJson(report) + "\n");
            if (failure != null) {
                try (NativeImage framebuffer = ScreenshotRecorder.takeScreenshot(client.getFramebuffer())) {
                    framebuffer.writeTo(output.resolve("failure-world.png"));
                }
            }
        } catch (Exception writeFailure) {
            LOGGER.error("Could not save smoke evidence", writeFailure);
        }
        if (failure == null) LOGGER.info("BADAPPLE_SMOKE_PASSED {} assertions", assertions.size());
        // A separate verifier requires an explicit passed report: graceful exit
        // alone never counts as success, including errors before this entrypoint.
        client.scheduleStop();
    }
}
