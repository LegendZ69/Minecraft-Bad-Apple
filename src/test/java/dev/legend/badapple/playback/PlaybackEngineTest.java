package dev.legend.badapple.playback;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.Clip;
import java.lang.reflect.Proxy;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

import static org.junit.jupiter.api.Assertions.*;

class PlaybackEngineTest {
    @TempDir Path directory;

    @Test
    void silentPlaybackUsesElapsedTimeAndPausesSeeksAndLoops() throws Exception {
        AtomicLong nanos = new AtomicLong();
        try (PlaybackEngine engine = engine(nanos)) {
            assertNull(engine.warning());
            assertFalse(engine.isPlaying());
            engine.play();
            nanos.addAndGet(750_000_000L);
            assertEquals(1, engine.update());
            engine.pause();
            nanos.addAndGet(5_000_000_000L);
            assertEquals(0.75, engine.positionSeconds(), 0.000001);
            engine.seekSeconds(0.25);
            assertEquals(0, engine.currentFrameIndex());
            assertFalse(engine.isPlaying());
            engine.resume();
            nanos.addAndGet(500_000_000L);
            assertEquals(0.75, engine.positionSeconds(), 0.000001);
            engine.setLooping(true);
            nanos.addAndGet(500_000_000L);
            assertEquals(0, engine.update());
            assertTrue(engine.isPlaying());
            assertEquals(0, engine.positionSeconds());
            engine.stop();
            nanos.addAndGet(10_000_000_000L);
            assertFalse(engine.isPlaying());
            assertEquals(0, engine.positionSeconds());
        }
    }

    @Test
    void seekingToEndWhilePausedDoesNotStartLoopingPlayback() throws Exception {
        AtomicLong nanos = new AtomicLong();
        try (PlaybackEngine engine = engine(nanos)) {
            engine.setLooping(true);
            engine.seekSeconds(1);
            assertFalse(engine.isPlaying());
            assertEquals(1, engine.positionSeconds());
            assertEquals(1, engine.currentFrameIndex());
            assertThrows(IllegalArgumentException.class, () -> engine.seekSeconds(Double.NaN));
            assertThrows(IllegalArgumentException.class, () -> engine.seekSeconds(Double.POSITIVE_INFINITY));
            engine.play();
            assertTrue(engine.isPlaying());
            assertEquals(0, engine.positionSeconds());
        }
    }

    @Test
    void stoppingAtEndKeepsFinalFrameUntilReplay() throws Exception {
        AtomicLong nanos = new AtomicLong();
        try (PlaybackEngine engine = engine(nanos)) {
            engine.play();
            nanos.addAndGet(2_000_000_000L);
            assertEquals(1, engine.update());
            assertFalse(engine.isPlaying());
            assertEquals(1, engine.positionSeconds());
            engine.resume();
            assertEquals(0, engine.positionSeconds());
            assertTrue(engine.isPlaying());
        }
    }

    @Test
    void audioClockDrivesVideoAndPauseResumeFlushesOldDeviceBuffers() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.play();
            nanos.addAndGet(250_000_000L);
            device.positionMicros = 250_000;
            assertEquals(0.25, engine.positionSeconds(), 0.000001);
            engine.pause();
            assertFalse(device.running);
            nanos.addAndGet(5_000_000_000L);
            assertEquals(0.25, engine.positionSeconds(), 0.000001);
            engine.resume();
            assertEquals(250_000, device.requestedPositionMicros);
            assertEquals(2, device.flushes);
            assertTrue(device.running);
            nanos.addAndGet(100_000_000L);
            device.positionMicros = 350_000;
            assertEquals(0.35, engine.positionSeconds(), 0.000001);
        }
        assertTrue(device.closed);
    }

    @Test
    void backwardSeekDoesNotLatchTheAsynchronousOldDevicePosition() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.seekSeconds(3);
            engine.play();
            assertEquals(3, engine.positionSeconds());
            device.applySeekImmediately = false;
            engine.seekSeconds(0);
            assertEquals(0, engine.positionSeconds());
            nanos.addAndGet(50_000_000L);
            assertEquals(0, engine.positionSeconds());
            device.positionMicros = 50_000;
            assertEquals(0.05, engine.positionSeconds(), 0.000001);
            nanos.addAndGet(100_000_000L);
            device.positionMicros = 150_000;
            assertEquals(0.15, engine.positionSeconds(), 0.000001);
            assertNull(engine.warning());
        }
    }

    @Test
    void forwardSeekDoesNotMoveBackToTheAsynchronousOldDevicePosition() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.seekSeconds(1);
            engine.play();
            device.applySeekImmediately = false;
            engine.seekSeconds(4);
            assertEquals(4, engine.positionSeconds());
            nanos.addAndGet(100_000_000L);
            assertEquals(4, engine.positionSeconds());
            device.positionMicros = 4_100_000;
            assertEquals(4.1, engine.positionSeconds(), 0.000001);
            assertTrue(engine.isPlaying());
        }
    }

    @Test
    void restartIgnoresStaleEndPositionUntilDeviceSeekSettles() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.seekSeconds(5);
            engine.play();
            nanos.addAndGet(1_000_000_000L);
            device.positionMicros = 6_000_000;
            assertEquals(6, engine.positionSeconds());
            assertFalse(engine.isPlaying());
            engine.stop();
            device.applySeekImmediately = false;
            engine.play();
            assertEquals(0, engine.positionSeconds());
            assertTrue(engine.isPlaying());
            nanos.addAndGet(50_000_000L);
            assertEquals(0, engine.positionSeconds());
            device.positionMicros = 50_000;
            assertEquals(0.05, engine.positionSeconds(), 0.000001);
            assertTrue(engine.isPlaying());
        }
    }

    @Test
    void loopingDoesNotRepeatedlyRestartFromAStaleAudioEndPosition() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.setLooping(true);
            engine.seekSeconds(5);
            engine.play();
            device.applySeekImmediately = false;
            nanos.addAndGet(1_000_000_000L);
            device.positionMicros = 6_000_000;
            assertEquals(0, engine.positionSeconds());
            assertEquals(0, device.requestedPositionMicros);
            assertEquals(2, device.flushes);
            nanos.addAndGet(50_000_000L);
            assertEquals(0, engine.positionSeconds());
            assertEquals(2, device.flushes);
            device.positionMicros = 50_000;
            assertEquals(0.05, engine.positionSeconds(), 0.000001);
            assertTrue(engine.isPlaying());
        }
    }

    @Test
    void shortBackwardSeekNearEndDoesNotAcceptStaleEndWithinClockTolerance() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.seekSeconds(5);
            engine.play();
            nanos.addAndGet(1_000_000_000L);
            device.positionMicros = 6_000_000;
            assertEquals(6, engine.positionSeconds());
            device.applySeekImmediately = false;
            engine.seekSeconds(5.95);
            engine.play();
            assertEquals(5.95, engine.positionSeconds(), 0.000001);
            assertTrue(engine.isPlaying());
            nanos.addAndGet(20_000_000L);
            assertEquals(5.95, engine.positionSeconds(), 0.000001);
            device.positionMicros = 5_970_000;
            assertEquals(5.97, engine.positionSeconds(), 0.000001);
            assertTrue(engine.isPlaying());
        }
    }

    @Test
    void separatelyReadStaleFramePositionDoesNotEndAudioClock() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.play();
            nanos.addAndGet(100_000_000L);
            device.positionMicros = 100_000;
            device.framePositionOverride = device.frameLength();
            assertEquals(0.1, engine.positionSeconds(), 0.000001);
            nanos.addAndGet(500_000_000L);
            // The audio time, not the stale frame count or free-running wall time, still leads.
            assertEquals(0.1, engine.positionSeconds(), 0.000001);
            device.positionMicros = 600_000;
            assertEquals(0.6, engine.positionSeconds(), 0.000001);
        }
    }

    @Test
    void impossibleForwardDeviceJumpIsRejectedEvenAfterSeekHasSettled() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.play();
            nanos.addAndGet(100_000_000L);
            device.positionMicros = 100_000;
            assertEquals(0.1, engine.positionSeconds(), 0.000001);
            nanos.addAndGet(50_000_000L);
            device.positionMicros = 5_000_000;
            assertEquals(0.1, engine.positionSeconds(), 0.000001);
            device.positionMicros = 150_000;
            assertEquals(0.15, engine.positionSeconds(), 0.000001);
            assertNull(engine.warning());
        }
    }

    @Test
    void transientBackwardAudioClockReadNeverMovesVideoBackward() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.play();
            nanos.addAndGet(400_000_000L);
            device.positionMicros = 400_000;
            assertEquals(0.4, engine.positionSeconds(), 0.000001);
            nanos.addAndGet(100_000_000L);
            device.positionMicros = 300_000;
            assertEquals(0.4, engine.positionSeconds(), 0.000001);
            device.positionMicros = 500_000;
            assertEquals(0.5, engine.positionSeconds(), 0.000001);
        }
    }

    @Test
    void stalledDeviceFallsBackWithoutLosingTheStallInterval() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.play();
            nanos.addAndGet(2_100_000_000L);
            assertEquals(2.1, engine.positionSeconds(), 0.000001);
            assertTrue(device.closed);
            assertTrue(engine.warning().contains("audio device stopped advancing"));
            nanos.addAndGet(200_000_000L);
            assertEquals(2.3, engine.positionSeconds(), 0.000001);
            assertTrue(engine.isPlaying());
        }
    }

    @Test
    void shorterAudioHandsOffToWallClockForFinalVideoFrames() throws Exception {
        AtomicLong nanos = new AtomicLong();
        SimulatedClip device = new SimulatedClip();
        device.lengthMicros = 1_000_000;
        try (PlaybackEngine engine = engine(nanos, device)) {
            engine.play();
            nanos.addAndGet(1_000_000_000L);
            device.positionMicros = 1_000_000;
            assertEquals(1, engine.positionSeconds());
            nanos.addAndGet(500_000_000L);
            assertEquals(1.5, engine.positionSeconds(), 0.000001);
            assertTrue(engine.isPlaying());
            assertNull(engine.warning());
        }
    }

    private PlaybackEngine engine(AtomicLong nanos) throws Exception {
        return new PlaybackEngine(archive(1_000_000), nanos::get, false);
    }

    private PlaybackEngine engine(AtomicLong nanos, SimulatedClip device) throws Exception {
        return new PlaybackEngine(archive(6_000_000), nanos::get, device.clip);
    }

    private VideoArchive archive(long durationMicros) throws Exception {
        VideoArchiveTest helper = new VideoArchiveTest();
        helper.directory = directory;
        byte[] png = VideoArchiveTest.png(1, 1);
        var manifest = VideoArchiveTest.manifest();
        manifest.addProperty("durationMicros", durationMicros);
        Path path = helper.archive(manifest,
                Map.of("frames/000000.png", png, "frames/000001.png", png));
        return VideoArchive.open(path);
    }

    /** Scripted native clock: seek acknowledgement and device advancement are independent. */
    private static final class SimulatedClip {
        private long positionMicros;
        private long requestedPositionMicros;
        private long lengthMicros = 6_000_000;
        private Integer framePositionOverride;
        private boolean applySeekImmediately = true;
        private boolean running;
        private boolean closed;
        private int flushes;

        private final Clip clip = (Clip) Proxy.newProxyInstance(Clip.class.getClassLoader(),
                new Class<?>[]{Clip.class}, (proxy, method, args) -> switch (method.getName()) {
                    case "getMicrosecondPosition" -> positionMicros;
                    case "getMicrosecondLength" -> lengthMicros;
                    case "getFramePosition" -> framePositionOverride != null
                            ? framePositionOverride : (int) (positionMicros * 48_000 / 1_000_000);
                    case "getLongFramePosition" -> positionMicros * 48_000 / 1_000_000;
                    case "getFrameLength" -> frameLength();
                    case "getFormat" -> new AudioFormat(48_000, 16, 2, true, false);
                    case "isOpen" -> !closed;
                    case "isRunning", "isActive" -> running;
                    case "setMicrosecondPosition" -> {
                        requestedPositionMicros = (long) args[0];
                        if (applySeekImmediately) positionMicros = requestedPositionMicros;
                        yield null;
                    }
                    case "start" -> { running = true; yield null; }
                    case "stop" -> { running = false; yield null; }
                    case "flush" -> { flushes++; yield null; }
                    case "close" -> { closed = true; running = false; yield null; }
                    case "toString" -> "SimulatedClip";
                    case "hashCode" -> System.identityHashCode(proxy);
                    case "equals" -> proxy == args[0];
                    default -> throw new UnsupportedOperationException(method.getName());
                });

        private int frameLength() {
            return (int) (lengthMicros * 48_000 / 1_000_000);
        }
    }
}
