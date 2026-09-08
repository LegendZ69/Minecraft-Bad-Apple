package dev.legend.badapple.smoke;

import com.google.gson.GsonBuilder;
import dev.legend.badapple.client.BadAppleClient;
import dev.legend.badapple.client.MovieScreen;
import dev.legend.badapple.client.PngFrames;
import dev.legend.badapple.playback.PlaybackEngine;
import java.lang.reflect.Field;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.BitSet;
import java.util.HexFormat;
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
import net.minecraft.client.gui.screen.GameMenuScreen;
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
import org.lwjgl.system.MemoryStack;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Isolated, opt-in client test, excluded from the shipped mod. Production-mode
 * tests require an explicit opt-in and the exact expected release JAR digest.
 * Creates its own normal single-player world; never runs a dedicated server or
 * accepts an EULA, license, account, or multiplayer-security dialog.
 */
public final class CloudSmokeClient implements ClientModInitializer {
    private static final Logger LOGGER = LoggerFactory.getLogger("badapple-cloud-smoke");
    private final Map<String, Object> report = new LinkedHashMap<>();
    private final List<String> assertions = new ArrayList<>();
    private final List<Map<String, Object>> checkpoints = new ArrayList<>();
    private final List<Map<String, Object>> commands = new ArrayList<>();
    private final List<Map<String, Object>> audioTiming = new ArrayList<>();
    private final List<String> audioWarnings = new ArrayList<>();
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
    private boolean productionMode;
    private final Map<String, Object> fullPlayback = new LinkedHashMap<>();
    private final List<Map<String, Object>> fullSamples = new ArrayList<>();
    private final BitSet fullPresentedFrames = new BitSet();
    private boolean fullPreparing;
    private boolean fullPrepared;
    private boolean fullRunning;
    private boolean fullCompleted;
    private long fullStartedNanos;
    private long fullNextSampleNanos;
    private long fullRenderCallbacks;
    private long fullPreviousRenderNanos;
    private long fullLongestRenderGapNanos;
    private long fullPeakHeapUsedBytes;
    private int fullFirstUploadedFrame = -1;
    private int fullLastUploadedFrame = -1;
    private long syntheticRunStartedNanos;

    @Override
    public void onInitializeClient() {
        if (!Boolean.getBoolean("badapple.smoke")) return;
        productionMode = Boolean.getBoolean("badapple.smoke.production");
        if (!FabricLoader.getInstance().isDevelopmentEnvironment() && !productionMode) {
            throw new IllegalStateException("Cloud smoke tests require an isolated development instance");
        }
        output = Path.of(Objects.requireNonNull(System.getProperty("badapple.smoke.output"),
                "Set badapple.smoke.output to an isolated evidence directory")).toAbsolutePath();
        started = System.nanoTime();
        referenceMode = Boolean.getBoolean("badapple.smoke.reference");
        report.put("schemaVersion", 1);
        report.put("startedUtc", Instant.now().toString());
        report.put("referenceMode", referenceMode);
        report.put("runtimeMode", productionMode ? "production" : "development");
        report.put("developmentEnvironment", FabricLoader.getInstance().isDevelopmentEnvironment());
        report.put("runtimeNamespace", FabricLoader.getInstance().getMappingResolver().getCurrentRuntimeNamespace());
        report.put("media", referenceMode ? "User-selected reference archive; provenance is documented separately"
                : "Synthetic deterministic test fixture; NOT Bad Apple reference media");
        report.put("minecraft", "1.21.1");
        report.put("assertions", assertions);
        report.put("checkpoints", checkpoints);
        report.put("commands", commands);
        report.put("audioTiming", audioTiming);
        report.put("audioWarnings", audioWarnings);
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
            if (finished) return;
            try {
                if (fullPreparing || fullRunning) observeFullPlayback(MinecraftClient.getInstance());
                if (capturePending != null) capture(MinecraftClient.getInstance());
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
                if (productionMode) verifyProductionRuntime();
                mod = FabricLoader.getInstance().getEntrypoints("client", ClientModInitializer.class).stream()
                        .filter(BadAppleClient.class::isInstance).map(BadAppleClient.class::cast)
                        .findFirst().orElseThrow();
                Path selectedArchive = FabricLoader.getInstance().getGameDir().resolve("badapple/" + archiveName());
                require(Files.isRegularFile(selectedArchive), "Selected smoke archive exists");
                report.put("archiveSha256", sha256(selectedArchive));
                report.put("archiveSizeBytes", Files.size(selectedArchive));
                report.put("renderer", GL11.glGetString(GL11.GL_RENDERER));
                report.put("openGlVersion", GL11.glGetString(GL11.GL_VERSION));
                verifyLargePngDecoder();
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
                audioSnapshot("initial-archive-opened");
                if (!referenceMode) {
                    command(client, "badapple loop false");
                    syntheticRunStartedNanos = System.nanoTime();
                    command(client, "badapple restart");
                    advance(19, 0);
                    return;
                }
                command(client, "badapple pause");
                pausedPosition = engine().positionSeconds();
                audioPosition = initialClip == null ? -1 : initialClip.getMicrosecondPosition();
                advance(5, 650);
            }
            case 19 -> {
                require(engine().warning() == null && initialClip != null && initialClip.isOpen(),
                        "Uninterrupted six-second synthetic preflight retains real audio without fallback");
                if (engine().isPlaying()) return;
                double elapsed = (System.nanoTime() - syntheticRunStartedNanos) / 1e9;
                require(Math.abs(engine().positionSeconds() - engine().durationSeconds()) <= 0.05
                                && elapsed >= engine().durationSeconds() - 0.2
                                && elapsed <= engine().durationSeconds() + 5,
                        "Synthetic preflight plays its entire six seconds and stops naturally");
                report.put("syntheticUninterruptedPlaybackSeconds", elapsed);
                report.put("syntheticUninterruptedFinalPositionSeconds", engine().positionSeconds());
                audioSnapshot("synthetic-uninterrupted-natural-end");
                command(client, "badapple pause");
                pausedPosition = engine().positionSeconds();
                audioPosition = initialClip.getMicrosecondPosition();
                advance(5, 650);
            }
            case 5 -> {
                audioSnapshot("pause-settled-650ms");
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
                // ALSA may report the old queued-buffer position immediately
                // after a backward seek. Compare against the requested paused
                // timeline target, not that stale device reading.
                audioPosition = (long) (engine().positionSeconds() * 1_000_000);
                report.put("audioResumeTargetMicros", audioPosition);
                command(client, "badapple resume");
                advance(7, 1100);
            }
            case 7 -> {
                audioSnapshot("resume-settled-1100ms");
                require(engine().isPlaying() && engine().positionSeconds() > (referenceMode ? 30.5 : 0.5),
                        "Resume advances playback clock and frame selection");
                report.put("audioClockAdvanced", initialClip != null
                        && initialClip.getMicrosecondPosition() > audioPosition + 200000 && engine().warning() == null);
                if (Boolean.getBoolean("badapple.smoke.requireAudio")) {
                    require(Boolean.TRUE.equals(report.get("audioClockAdvanced")) && engine().warning() == null,
                            "Real Java Sound output clock advances without fallback");
                }
                client.setScreen(new GameMenuScreen(true));
                advance(13, 650);
            }
            case 13 -> {
                require(client.isPaused() && !engine().isPlaying(),
                        "Opening the single-player menu automatically pauses playback");
                if (initialClip != null) require(!initialClip.isRunning(), "Single-player menu pauses Java Sound output");
                pausedPosition = engine().positionSeconds();
                advance(14, 650);
            }
            case 14 -> {
                require(Math.abs(engine().positionSeconds() - pausedPosition) < 0.002,
                        "Single-player menu retains a frozen playback timestamp");
                client.setScreen(null);
                advance(15, 650);
            }
            case 15 -> {
                require(!client.isPaused() && engine().isPlaying(),
                        "Closing the single-player menu automatically resumes playback");
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
                advance(16, 150);
            }
            case 16 -> {
                audioSnapshot("restart-settled-150ms");
                require(engine().isPlaying() && engine().positionSeconds() < 0.4,
                        "Restart returns to the beginning after a 150ms device settling window");
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
                if (referenceMode) {
                    command(client, "badapple loop false");
                    command(client, "badapple seek 0");
                    command(client, "badapple place native");
                    fullPreparing = true;
                    advance(17, 0);
                    return;
                }
                latestWarning = engine().warning();
                command(client, "badapple unload");
                finish(client, null);
            }
            case 17 -> {
                if (!fullPrepared) return;
                PlaybackEngine player = engine();
                require(player != null && field(player, "audio", Clip.class) != null,
                        "Full reference playback has a real Java Sound clip");
                fullStartedNanos = System.nanoTime();
                command(client, "badapple restart");
                fullPlayback.put("status", "running");
                fullPlayback.put("uninterrupted", true);
                fullPlayback.put("durationSeconds", player.durationSeconds());
                fullPlayback.put("frameCount", player.metadata().frameCount());
                fullPlayback.put("nativeWidth", player.width());
                fullPlayback.put("nativeHeight", player.height());
                fullPlayback.put("worldWidth", field(screen(), "width", Double.class));
                fullPlayback.put("worldHeight", field(screen(), "height", Double.class));
                fullPlayback.put("sampleIntervalSeconds", 5);
                fullPlayback.put("startedAtPositionSeconds", player.positionSeconds());
                fullPlayback.put("startingCommandIndex", commands.size());
                fullPlayback.put("initialFramePreloadedAtStart", true);
                fullPlayback.put("samples", fullSamples);
                fullPlayback.put("notes", "No seek, pause, or other command during the complete playthrough. "
                        + "Distinct uploaded frames are counted only from actual world-render callbacks after restart; "
                        + "the initially preloaded frame zero is verified separately. Software rendering may skip source frames. "
                        + "GPU comparisons verify the actually uploaded frame, which may lag timeline selection. "
                        + "Initial raw audio position may still reflect the previous seek while the device settles. "
                        + "Physical speakers and every original frame being displayed are not assumed.");
                report.put("fullPlayback", fullPlayback);
                fullRunning = true;
                fullNextSampleNanos = fullStartedNanos + 5_000_000_000L;
                sampleFullPlayback(0, false);
                advance(18, 0);
            }
            case 18 -> {
                require(client.world != null && client.player != null && !client.isPaused(),
                        "Full reference playback remains connected and unpaused");
                PlaybackEngine player = engine();
                require(player != null && screen() != null && screen().error() == null,
                        "Full reference playback retains a healthy movie and renderer");
                require(player.warning() == null && field(player, "audio", Clip.class) != null,
                        "Full reference playback never falls back to silent timing");
                fullPeakHeapUsedBytes = Math.max(fullPeakHeapUsedBytes, heapUsedBytes());
                if (!fullCompleted) return;
                require(fullSamples.size() >= (int) Math.floor(player.durationSeconds() / 5) + 1,
                        "Full reference playback contains bounded five-second evidence samples through the end");
                require(!player.isPlaying() && Math.abs(player.positionSeconds() - player.durationSeconds()) <= 0.05
                                && fullLastUploadedFrame == player.metadata().frameCount() - 1,
                        "Full reference playback naturally reaches the exact final source frame and stopped state");
                latestWarning = player.warning();
                command(client, "badapple unload");
                stage = 12;
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

    private void observeFullPlayback(MinecraftClient client) throws Exception {
        PlaybackEngine player = engine();
        MovieScreen movie = screen();
        if (player == null || movie == null) return;
        require(movie.error() == null, "Full reference playback renderer has no terminal error");
        NativeImageBackedTexture texture = field(movie, "texture", NativeImageBackedTexture.class);
        if (texture == null) return;
        int uploaded = uploadedFrame(movie);
        if (fullPreparing) {
            if (uploaded != 0 || player.currentFrameIndex() != 0) return;
            // Frame zero has actually been drawn at native world scale before
            // the uninterrupted run starts, avoiding an asynchronous decode race.
            fullPreparing = false;
            fullPrepared = true;
            return;
        }
        if (uploaded < 0) return;
        long now = System.nanoTime();
        fullRenderCallbacks++;
        if (fullPreviousRenderNanos != 0) {
            fullLongestRenderGapNanos = Math.max(fullLongestRenderGapNanos, now - fullPreviousRenderNanos);
        }
        fullPreviousRenderNanos = now;
        require(uploaded >= fullLastUploadedFrame, "Uninterrupted reference playback never rewinds uploaded frames");
        fullPresentedFrames.set(uploaded);
        if (fullFirstUploadedFrame < 0) fullFirstUploadedFrame = uploaded;
        fullLastUploadedFrame = uploaded;
        boolean ended = !player.isPlaying() && player.positionSeconds() >= player.durationSeconds();
        if (ended && uploaded == player.metadata().frameCount() - 1) {
            sampleFullPlayback(uploaded, true);
            fullRunning = false;
            fullCompleted = true;
            completeFullPlayback(player);
        } else if (now >= fullNextSampleNanos) {
            sampleFullPlayback(uploaded, false);
            // Keep a bounded sample list and preserve a real long gap in the
            // evidence rather than fabricating catch-up observations.
            do {
                fullNextSampleNanos += 5_000_000_000L;
            } while (fullNextSampleNanos <= now);
        }
    }

    private void sampleFullPlayback(int uploaded, boolean ended) throws Exception {
        PlaybackEngine player = engine();
        MovieScreen movie = screen();
        Clip clip = field(player, "audio", Clip.class);
        require(clip != null, "Full reference sample retains its Java Sound device");
        Map<String, Object> sample = new LinkedHashMap<>();
        long beforeEngineRead = System.nanoTime();
        double position;
        synchronized (player) {
            position = player.positionSeconds();
            long afterEngineRead = System.nanoTime();
            // Use the device position accepted by this exact engine update for
            // synchronized A/V evidence. A second ALSA query can itself block,
            // so record that independent raw reading with its own timestamps.
            sample.put("elapsedSeconds", (afterEngineRead - fullStartedNanos) / 1e9);
            sample.put("positionSeconds", position);
            sample.put("audioPositionSeconds", field(player, "lastAudioPosition", Long.class) / 1_000_000.0);
            sample.put("engineClockReadMillis", (afterEngineRead - beforeEngineRead) / 1e6);
            sample.put("playing", field(player, "playRequested", Boolean.class));
            sample.put("audioDriving", field(player, "audioDriving", Boolean.class));
            sample.put("warning", player.warning());
            long beforeRawRead = System.nanoTime();
            sample.put("rawAudioPositionSeconds", clip.getMicrosecondPosition() / 1_000_000.0);
            long afterRawRead = System.nanoTime();
            sample.put("rawAudioReadMillis", (afterRawRead - beforeRawRead) / 1e6);
            sample.put("rawAudioObservedElapsedSeconds", (afterRawRead - fullStartedNanos) / 1e9);
            sample.put("elapsedAfterClockReadsSeconds", (afterRawRead - fullStartedNanos) / 1e9);
        }
        long[] sourceTimestamps = player.metadata().frameTimestampsMicros();
        int requested = Arrays.binarySearch(sourceTimestamps, (long) (position * 1_000_000));
        if (requested < 0) requested = -requested - 2;
        sample.put("requestedFrame", Math.max(0, Math.min(sourceTimestamps.length - 1, requested)));
        sample.put("uploadedFrame", uploaded);
        double uploadedPosition = sourceTimestamps[uploaded] / 1_000_000.0;
        sample.put("uploadedPositionSeconds", uploadedPosition);
        sample.put("clipRunning", clip.isRunning());
        sample.put("heapUsedBytes", heapUsedBytes());
        sample.put("nativeWidth", player.width());
        sample.put("nativeHeight", player.height());
        NativeImageBackedTexture texture = field(movie, "texture", NativeImageBackedTexture.class);
        int previousTexture = GL11.glGetInteger(GL11.GL_TEXTURE_BINDING_2D);
        try (NativeImage source = PngFrames.read(player.archive().readFrame(uploaded));
                NativeImage gpu = new NativeImage(player.width(), player.height(), false)) {
            GL11.glBindTexture(GL11.GL_TEXTURE_2D, texture.getGlId());
            gpu.loadFromTextureImage(0, false);
            for (int y = 0; y < source.getHeight(); y++) {
                for (int x = 0; x < source.getWidth(); x++) {
                    if (source.getColor(x, y) != gpu.getColor(x, y)) {
                        throw new AssertionError("Full reference GPU sample differs at source frame "
                                + uploaded + " pixel " + x + "," + y);
                    }
                }
            }
            sample.put("gpuExactMatch", true);
            sample.put("gpuPixelsCompared", player.width() * player.height());
        } finally {
            GL11.glBindTexture(GL11.GL_TEXTURE_2D, previousTexture);
        }
        fullPeakHeapUsedBytes = Math.max(fullPeakHeapUsedBytes, ((Number) sample.get("heapUsedBytes")).longValue());
        fullSamples.add(sample);
        require(uploadedPosition <= position + 0.000001 && position - uploadedPosition <= 1,
                "Full reference uploaded frame remains within one second of the actual playback clock");
        if (fullSamples.size() > 1) {
            Map<String, Object> previous = fullSamples.get(fullSamples.size() - 2);
            double wallGap = ((Number) sample.get("elapsedSeconds")).doubleValue()
                    - ((Number) previous.get("elapsedSeconds")).doubleValue();
            double sourceAdvance = uploadedPosition - ((Number) previous.get("uploadedPositionSeconds")).doubleValue();
            require(sourceAdvance + (ended ? 1 : 0.2) >= wallGap * 0.5,
                    "Full reference uploaded source frames continue advancing between evidence samples");
        }
        LOGGER.info("BADAPPLE_FULL_REFERENCE_SAMPLE elapsed={} position={} uploadedFrame={} ended={}",
                sample.get("elapsedSeconds"), sample.get("positionSeconds"), uploaded, ended);
    }

    private void completeFullPlayback(PlaybackEngine player) throws Exception {
        Clip clip = field(player, "audio", Clip.class);
        int frameCount = player.metadata().frameCount();
        int observed = fullPresentedFrames.cardinality();
        fullPlayback.put("status", "passed");
        fullPlayback.put("elapsedSeconds", (System.nanoTime() - fullStartedNanos) / 1e9);
        fullPlayback.put("finalPositionSeconds", player.positionSeconds());
        fullPlayback.put("finalFrame", player.currentFrameIndex());
        fullPlayback.put("finalPlaying", player.isPlaying());
        fullPlayback.put("finalClipRunning", clip != null && clip.isRunning());
        fullPlayback.put("audioFallback", player.warning() != null || clip == null);
        fullPlayback.put("endingCommandIndex", commands.size());
        fullPlayback.put("renderCallbacks", fullRenderCallbacks);
        fullPlayback.put("uniqueUploadedFrames", observed);
        fullPlayback.put("frameCoverageFraction", (double) observed / frameCount);
        fullPlayback.put("skippedSourceFrames", frameCount - observed);
        fullPlayback.put("firstUploadedFrame", fullFirstUploadedFrame);
        fullPlayback.put("lastUploadedFrame", fullLastUploadedFrame);
        fullPlayback.put("meanRenderCallbacksPerSecond", fullRenderCallbacks / ((System.nanoTime() - fullStartedNanos) / 1e9));
        fullPlayback.put("longestRenderGapSeconds", fullLongestRenderGapNanos / 1e9);
        fullPlayback.put("gpuComparisons", fullSamples.size());
        fullPlayback.put("gpuPixelsCompared", (long) fullSamples.size() * player.width() * player.height());
        fullPlayback.put("peakHeapUsedBytes", fullPeakHeapUsedBytes);
        fullPlayback.put("maxObservedHeapBytes", Runtime.getRuntime().maxMemory());
        LOGGER.info("BADAPPLE_FULL_REFERENCE_COMPLETED uniqueUploadedFrames={}/{} callbacks={} samples={}",
                observed, frameCount, fullRenderCallbacks, fullSamples.size());
    }

    private static int uploadedFrame(MovieScreen movie) throws ReflectiveOperationException {
        Object decoder = field(movie, "decoder", Object.class);
        synchronized (decoder) {
            return field(decoder, "delivered", Integer.class);
        }
    }

    private static long heapUsedBytes() {
        Runtime runtime = Runtime.getRuntime();
        return runtime.totalMemory() - runtime.freeMemory();
    }

    private static String sha256(Path path) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream stream = Files.newInputStream(path)) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = stream.read(buffer)) >= 0) {
                if (count > 0) digest.update(buffer, 0, count);
            }
        }
        return HexFormat.of().formatHex(digest.digest());
    }

    private void verifyLargePngDecoder() throws Exception {
        int pointerBefore = MemoryStack.stackGet().getPointer();
        byte[] encoded;
        int[] expected;
        try (NativeImage noise = new NativeImage(512, 384, false)) {
            int state = 0x4bad1234;
            for (int y = 0; y < noise.getHeight(); y++) {
                for (int x = 0; x < noise.getWidth(); x++) {
                    state ^= state << 13;
                    state ^= state >>> 17;
                    state ^= state << 5;
                    noise.setColor(x, y, 0xff000000 | (state & 0x00ffffff));
                }
            }
            encoded = noise.getBytes();
            expected = noise.copyPixelsRgba();
        }
        require(encoded.length > 64 * 1024, "Large-PNG regression fixture exceeds the default 64KiB native stack");
        for (int iteration = 0; iteration < 16; iteration++) {
            try (NativeImage decoded = PngFrames.read(encoded)) {
                require(decoded.getWidth() == 512 && decoded.getHeight() == 384
                                && Arrays.equals(expected, decoded.copyPixelsRgba()),
                        "Large-PNG stream decoding repeatedly preserves every native pixel");
            }
            require(MemoryStack.stackGet().getPointer() == pointerBefore,
                    "PNG decoding restores native scratch-stack scope after every iteration");
        }
        report.put("largePngRegression", Map.of("status", "passed", "encodedBytes", encoded.length,
                "decodeIterations", 16, "pixelsCompared", 16L * 512 * 384,
                "nativeStackPointerPreserved", true));
    }

    private void command(MinecraftClient client, String command) throws Exception {
        audioSnapshot("before /" + command);
        int result = Objects.requireNonNull(ClientCommandManager.getActiveDispatcher()).execute(command,
                (FabricClientCommandSource) Objects.requireNonNull(client.getNetworkHandler()).getCommandSource());
        Map<String, Object> observation = new LinkedHashMap<>();
        observation.put("command", "/" + command);
        observation.put("result", result);
        observation.put("elapsedSeconds", (System.nanoTime() - started) / 1e9);
        commands.add(observation);
        require(result == 1, "Command accepted: /" + command);
        audioSnapshot("after /" + command);
    }

    private void audioSnapshot(String label) throws ReflectiveOperationException {
        Map<String, Object> sample = new LinkedHashMap<>();
        sample.put("label", label);
        sample.put("elapsedSeconds", (System.nanoTime() - started) / 1e9);
        PlaybackEngine player = engine();
        sample.put("engineLoaded", player != null);
        if (player != null) {
            synchronized (player) {
                Clip clip = field(player, "audio", Clip.class);
                sample.put("clipPresent", clip != null);
                if (clip != null) {
                    sample.put("clipMicrosecondsBeforeEngineUpdate", clip.getMicrosecondPosition());
                    sample.put("clipFramePositionBeforeEngineUpdate", clip.getLongFramePosition());
                    sample.put("clipOpen", clip.isOpen());
                    sample.put("clipRunning", clip.isRunning());
                    sample.put("clipActive", clip.isActive());
                    sample.put("clipFrameLength", clip.getFrameLength());
                    sample.put("clipMicrosecondLength", clip.getMicrosecondLength());
                    sample.put("clipBufferBytes", clip.getBufferSize());
                    sample.put("clipFormat", clip.getFormat().toString());
                }
                sample.put("enginePositionSeconds", player.positionSeconds());
                sample.put("enginePlaying", player.isPlaying());
                sample.put("engineWarning", player.warning());
                if (player.warning() != null && !audioWarnings.contains(player.warning())) audioWarnings.add(player.warning());
                Map<String, Object> clockState = new LinkedHashMap<>();
                for (String name : List.of("audioDriving", "audioSeekPending", "audioStartPositionMicros",
                        "lastAudioPosition", "audioStartNanos", "lastAudioAdvanceNanos")) {
                    try {
                        clockState.put(name, field(player, name, Object.class));
                    } catch (NoSuchFieldException ignored) {
                        // Keep diagnostics compatible while the clock is refactored.
                    }
                }
                sample.put("engineAudioState", clockState);
                if (clip != null) {
                    sample.put("clipMicrosecondsAfterEngineUpdate", clip.getMicrosecondPosition());
                    sample.put("clipFramePositionAfterEngineUpdate", clip.getLongFramePosition());
                }
            }
        }
        audioTiming.add(sample);
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
        try (NativeImage source = PngFrames.read(engine.archive().readFrame(delivered));
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

    private void verifyProductionRuntime() throws Exception {
        FabricLoader loader = FabricLoader.getInstance();
        require(!loader.isDevelopmentEnvironment(), "production: development environment is disabled");
        require("intermediary".equals(loader.getMappingResolver().getCurrentRuntimeNamespace()),
                "production: Minecraft runtime namespace is intermediary");
        require(!referenceMode, "production: isolated synthetic fixture only");
        Path expectedGame = Path.of(Objects.requireNonNull(System.getProperty("badapple.smoke.expectedGameDir")))
                .toRealPath();
        Path game = loader.getGameDir().toRealPath();
        require(game.equals(expectedGame) && game.endsWith(Path.of("build", "production-game")),
                "production: designated isolated game directory");
        require(output.toRealPath().equals(game.getParent().resolve("production-verification").toRealPath()),
                "production: designated isolated evidence directory");

        Path expectedJar = Path.of(Objects.requireNonNull(System.getProperty("badapple.smoke.expectedJar")))
                .toRealPath();
        String expectedDigest = Objects.requireNonNull(System.getProperty("badapple.smoke.expectedJarSha256"));
        require(expectedDigest.matches("[0-9a-f]{64}"), "production: expected release digest is SHA-256");
        Path actualJar = classSource(BadAppleClient.class);
        require(Files.isRegularFile(actualJar) && actualJar.getFileName().toString().endsWith(".jar"),
                "production: main entrypoint loaded from an actual JAR");
        require(actualJar.equals(expectedJar), "production: actual code source is the selected release JAR");
        String actualDigest = sha256(actualJar);
        report.put("expectedModJarSha256", expectedDigest);
        report.put("loadedModJarSha256", actualDigest);
        report.put("loadedModJarPath", actualJar.toString());
        require(actualDigest.equals(expectedDigest), "production: loaded release JAR SHA-256 matches expected artifact");
        for (Class<?> type : List.of(MovieScreen.class, PngFrames.class, PlaybackEngine.class)) {
            require(classSource(type).equals(actualJar), "production: " + type.getSimpleName() + " loads from same release JAR");
        }
        var container = loader.getModContainer("badapple").orElseThrow();
        report.put("loadedModId", container.getMetadata().getId());
        report.put("loadedModVersion", container.getMetadata().getVersion().getFriendlyString());
        report.put("loadedModOriginKind", container.getOrigin().getKind().name());
        report.put("loadedModOriginPaths", container.getOrigin().getPaths().stream().map(Path::toString).toList());
        require(container.getOrigin().getPaths().size() == 1
                        && container.getOrigin().getPaths().getFirst().toRealPath().equals(actualJar),
                "production: Fabric mod origin agrees with actual loaded code source");
        require(!classSource(CloudSmokeClient.class).equals(actualJar),
                "production: smoke harness is a separate non-release artifact");
    }

    private static Path classSource(Class<?> type) throws Exception {
        var source = Objects.requireNonNull(type.getProtectionDomain().getCodeSource(),
                "Class has no verifiable code source: " + type.getName());
        requireFileUrl(source.getLocation().getProtocol());
        return Path.of(source.getLocation().toURI()).toRealPath();
    }

    private static void requireFileUrl(String protocol) {
        if (!"file".equals(protocol)) throw new IllegalStateException("Expected an on-disk JAR code source, got " + protocol);
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
        if (failure != null && mod != null) {
            try {
                PlaybackEngine live = engine();
                if (live != null && live.warning() != null) {
                    latestWarning = live.warning();
                    if (!audioWarnings.contains(latestWarning)) audioWarnings.add(latestWarning);
                }
                MovieScreen movie = screen();
                if (movie != null) report.put("failureDecoderError", movie.error());
            } catch (ReflectiveOperationException diagnosticFailure) {
                report.put("failureDiagnosticError", diagnosticFailure.toString());
            }
        }
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
