package dev.legend.badapple.playback;

import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicLong;

import static org.junit.jupiter.api.Assertions.*;

class TimelineTest {
    @Test
    void choosesMostRecentTimestampIncludingVariableFrameIntervals() {
        Timeline timeline = new Timeline(250_000, new long[]{0, 33_367, 70_000, 150_000});
        assertEquals(0, timeline.frameAtMicros(-1));
        assertEquals(0, timeline.frameAtMicros(33_366));
        assertEquals(1, timeline.frameAtMicros(33_367));
        assertEquals(2, timeline.frameAtMicros(149_999));
        assertEquals(3, timeline.frameAtMicros(150_000));
        assertEquals(3, timeline.frameAtMicros(Long.MAX_VALUE));
    }

    @Test
    void pauseResumeAndSeekPreservePositionWithoutCountingPausedTime() {
        AtomicLong nanos = new AtomicLong(1_000_000_000L);
        Timeline timeline = new Timeline(5_000_000, new long[]{0, 1_000_000, 3_000_000}, nanos::get);
        timeline.play();
        nanos.addAndGet(1_500_000_000L);
        assertEquals(1_500_000, timeline.positionMicros());
        timeline.pause();
        nanos.addAndGet(10_000_000_000L);
        assertEquals(1_500_000, timeline.positionMicros());
        assertFalse(timeline.isPlaying());
        timeline.seekMicros(2_000_000);
        assertEquals(2_000_000, timeline.positionMicros());
        assertFalse(timeline.isPlaying());
        timeline.play();
        nanos.addAndGet(1_100_000_000L);
        assertEquals(3_100_000, timeline.positionMicros());
        assertEquals(2, timeline.currentFrameIndex());
    }

    @Test
    void clampsSeeksAndEndsThenRestartsFromBeginning() {
        AtomicLong nanos = new AtomicLong();
        Timeline timeline = new Timeline(1_000_000, new long[]{0, 500_000}, nanos::get);
        timeline.seekMicros(-50);
        assertEquals(0, timeline.positionMicros());
        timeline.play();
        nanos.set(2_000_000_000L);
        assertEquals(1_000_000, timeline.positionMicros());
        assertFalse(timeline.isPlaying());
        assertEquals(1, timeline.currentFrameIndex());
        timeline.play();
        assertEquals(0, timeline.positionMicros());
        assertTrue(timeline.isPlaying());
        timeline.stop();
        assertEquals(0, timeline.positionMicros());
        assertFalse(timeline.isPlaying());
        timeline.seekMicros(Long.MAX_VALUE);
        assertEquals(1_000_000, timeline.positionMicros());
    }

    @Test
    void copiesTimestampArrayAndRejectsMalformedTimeline() {
        long[] timestamps = {0, 50};
        Timeline timeline = new Timeline(100, timestamps);
        timestamps[1] = 1;
        assertEquals(0, timeline.frameAtMicros(25));
        assertThrows(IllegalArgumentException.class, () -> new Timeline(100, new long[]{1}));
        assertThrows(IllegalArgumentException.class, () -> new Timeline(100, new long[]{0, 0}));
        assertThrows(IllegalArgumentException.class, () -> new Timeline(100, new long[]{0, 100}));
    }
}
