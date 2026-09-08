package dev.legend.badapple.playback;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

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

    private PlaybackEngine engine(AtomicLong nanos) throws Exception {
        VideoArchiveTest helper = new VideoArchiveTest();
        helper.directory = directory;
        byte[] png = VideoArchiveTest.png(1, 1);
        Path path = helper.archive(VideoArchiveTest.manifest(),
                Map.of("frames/000000.png", png, "frames/000001.png", png));
        return new PlaybackEngine(VideoArchive.open(path), nanos::get, false);
    }
}
