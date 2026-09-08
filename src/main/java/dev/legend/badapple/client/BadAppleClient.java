package dev.legend.badapple.client;

import com.mojang.brigadier.arguments.BoolArgumentType;
import com.mojang.brigadier.arguments.DoubleArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;
import dev.legend.badapple.playback.PlaybackEngine;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.command.v2.ClientCommandRegistrationCallback;
import net.fabricmc.fabric.api.client.command.v2.FabricClientCommandSource;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.fabricmc.fabric.api.client.rendering.v1.WorldRenderEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.world.ClientWorld;
import net.minecraft.text.Text;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Consumer;

import static net.fabricmc.fabric.api.client.command.v2.ClientCommandManager.argument;
import static net.fabricmc.fabric.api.client.command.v2.ClientCommandManager.literal;

public final class BadAppleClient implements ClientModInitializer {
    private static final Logger LOGGER = LoggerFactory.getLogger("badapple");
    private static final String DEFAULT_FILE = "bad_apple.bapple";
    private final ExecutorService loader = Executors.newSingleThreadExecutor(r -> {
        Thread thread = new Thread(r, "badapple-loader");
        thread.setDaemon(true);
        return thread;
    });
    private Path mediaDirectory;
    private PlaybackEngine engine;
    private MovieScreen screen;
    private ClientWorld screenWorld;
    private volatile int loadGeneration;
    private boolean loading;
    private boolean menuPaused;

    @Override
    public void onInitializeClient() {
        mediaDirectory = FabricLoader.getInstance().getGameDir().resolve("badapple");
        try {
            Files.createDirectories(mediaDirectory);
        } catch (IOException e) {
            LOGGER.error("Cannot create video directory {}", mediaDirectory, e);
        }
        registerCommands();
        WorldRenderEvents.LAST.register(context -> {
            if (screen != null && MinecraftClient.getInstance().world == screenWorld) {
                try {
                    screen.render(context);
                    if (screen.error() != null) throw new IllegalStateException(screen.error());
                } catch (RuntimeException e) {
                    LOGGER.error("Video rendering failed", e);
                    message("Playback failed: " + describe(e));
                    unload();
                }
            }
        });
        ClientTickEvents.END_CLIENT_TICK.register(client -> {
            if (screenWorld != null && client.world != screenWorld) {
                unload();
            }
            if (engine == null) return;
            if (client.isPaused() && engine.isPlaying()) {
                engine.pause();
                menuPaused = true;
            } else if (!client.isPaused() && menuPaused) {
                menuPaused = false;
                engine.resume();
            }
        });
        ClientPlayConnectionEvents.DISCONNECT.register((handler, client) -> unload());
        ClientLifecycleEvents.CLIENT_STOPPING.register(client -> {
            unload();
            loader.shutdownNow();
        });
    }

    private void registerCommands() {
        ClientCommandRegistrationCallback.EVENT.register((dispatcher, registryAccess) -> dispatcher.register(
            literal("badapple")
                .executes(context -> help(context.getSource()))
                .then(literal("help").executes(context -> help(context.getSource())))
                .then(literal("load")
                    .executes(context -> load(DEFAULT_FILE))
                    .then(argument("file", StringArgumentType.string())
                        .executes(context -> load(StringArgumentType.getString(context, "file")))))
                .then(literal("play").executes(context -> {
                    if (engine == null) return load(DEFAULT_FILE);
                    return control(PlaybackEngine::play, "Playing.");
                }))
                .then(literal("pause").executes(context -> control(PlaybackEngine::pause, "Paused.")))
                .then(literal("resume").executes(context -> control(PlaybackEngine::resume, "Resumed.")))
                .then(literal("restart").executes(context -> control(player -> {
                    player.seekSeconds(0);
                    player.play();
                }, "Restarted.")))
                .then(literal("stop").executes(context -> control(PlaybackEngine::stop, "Stopped; screen retained.")))
                .then(literal("unload").executes(context -> {
                    unload();
                    message("Screen removed.");
                    return 1;
                }))
                .then(literal("seek").then(argument("seconds", DoubleArgumentType.doubleArg(0))
                    .executes(context -> control(player -> player.seekSeconds(
                        DoubleArgumentType.getDouble(context, "seconds")), "Seek complete."))))
                .then(literal("loop").then(argument("enabled", BoolArgumentType.bool())
                    .executes(context -> control(player -> player.setLooping(
                        BoolArgumentType.getBool(context, "enabled")), "Loop setting updated."))))
                .then(literal("place")
                    .executes(context -> place(48))
                    .then(literal("native").executes(context -> engine == null ? missing() : place(engine.metadata().width())))
                    .then(argument("width", DoubleArgumentType.doubleArg(1, 4096))
                        .executes(context -> place(DoubleArgumentType.getDouble(context, "width")))))
                .then(literal("status").executes(context -> status()))
        ));
    }

    private int load(String filename) {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client.world == null || client.player == null) {
            message("Join a world first.");
            return 0;
        }
        Path path;
        try {
            path = mediaDirectory.resolve(filename).normalize();
            if (!path.startsWith(mediaDirectory) || !filename.endsWith(".bapple") || !Files.isRegularFile(path)) {
                message("Put " + filename + " in " + mediaDirectory + ". Convert your video with tools/prepare_video.py first.");
                return 0;
            }
        } catch (RuntimeException e) {
            message("Invalid video filename.");
            return 0;
        }
        int generation = ++loadGeneration;
        ClientWorld requestedWorld = client.world;
        loading = true;
        message("Loading " + filename + "...");
        loader.execute(() -> {
            PlaybackEngine opened;
            try {
                opened = PlaybackEngine.open(path);
            } catch (Exception e) {
                LOGGER.warn("Could not open video {}", path, e);
                client.execute(() -> {
                    if (generation == loadGeneration) {
                        loading = false;
                        message("Cannot load video: " + describe(e));
                    }
                });
                return;
            }
            client.execute(() -> {
                if (generation != loadGeneration || client.world != requestedWorld || client.player == null) {
                    if (generation == loadGeneration) loading = false;
                    closeEngine(opened);
                    return;
                }
                loading = false;
                clearCurrent();
                engine = opened;
                screenWorld = requestedWorld;
                try {
                    screen = new MovieScreen(opened);
                    screen.place(client, 48);
                    opened.play();
                    message("Loaded " + opened.metadata().width() + "×" + opened.metadata().height()
                        + ", " + opened.metadata().frameCount() + " frames. /badapple pause | seek <seconds> | place native");
                    if (opened.warning() != null) message(opened.warning());
                } catch (Exception e) {
                    message("Cannot create screen: " + describe(e));
                    LOGGER.error("Cannot create screen", e);
                    clearCurrent();
                }
            });
        });
        return 1;
    }

    private int place(double width) {
        if (screen == null) return missing();
        screen.place(MinecraftClient.getInstance(), width);
        message("Screen placed ahead: " + width + " blocks wide.");
        return 1;
    }

    private int control(Consumer<PlaybackEngine> action, String feedback) {
        if (engine == null) return missing();
        menuPaused = false;
        try {
            action.accept(engine);
            message(feedback);
            return 1;
        } catch (RuntimeException e) {
            LOGGER.warn("Playback control failed", e);
            message("Playback control failed: " + describe(e));
            return 0;
        }
    }

    private int status() {
        if (engine == null) return missing();
        message(String.format(Locale.ROOT, "%s %.2f / %.2f s · frame %d / %d · %d×%d",
            engine.isPlaying() ? "Playing" : "Paused", engine.positionSeconds(), engine.durationSeconds(),
            engine.currentFrameIndex() + 1, engine.metadata().frameCount(), engine.metadata().width(), engine.metadata().height()));
        if (engine.warning() != null) message(engine.warning());
        return 1;
    }

    private int help(FabricClientCommandSource source) {
        source.sendFeedback(Text.literal("Bad Apple · /badapple load [file.bapple] | play | pause | resume | restart | stop | unload | seek <seconds> | loop <true/false> | place [width/native] | status"));
        source.sendFeedback(Text.literal("Videos: " + mediaDirectory));
        return 1;
    }

    private int missing() {
        message(loading ? "Video is still loading." : "Load a video first: /badapple load");
        return 0;
    }

    private void unload() {
        ++loadGeneration;
        loading = false;
        clearCurrent();
    }

    private void clearCurrent() {
        if (screen != null) {
            screen.close();
            screen = null;
        }
        if (engine != null) {
            closeEngine(engine);
            engine = null;
        }
        screenWorld = null;
        menuPaused = false;
    }

    private static void closeEngine(PlaybackEngine player) {
        try {
            player.close();
        } catch (Exception e) {
            LOGGER.warn("Could not close video", e);
        }
    }

    private static void message(String text) {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client.player != null) client.player.sendMessage(Text.literal("[Bad Apple] " + text), false);
        else LOGGER.info(text);
    }

    private static String describe(Throwable error) {
        return error.getMessage() == null ? error.getClass().getSimpleName() : error.getMessage();
    }
}
