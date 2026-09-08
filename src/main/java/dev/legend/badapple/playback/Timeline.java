package dev.legend.badapple.playback;

import java.util.Arrays;
import java.util.Objects;
import java.util.function.LongSupplier;

/** Monotonic fallback clock and variable-frame-rate lookup, independent of Minecraft and audio. */
public final class Timeline {
    private final long durationMicros;
    private final long[] timestamps;
    private final LongSupplier nanoClock;
    private long anchorNanos;
    private long anchorPositionMicros;
    private boolean playing;

    public Timeline(long durationMicros, long[] timestamps) {
        this(durationMicros, timestamps, System::nanoTime);
    }

    public Timeline(long durationMicros, long[] timestamps, LongSupplier nanoClock) {
        if (durationMicros <= 0 || timestamps == null || timestamps.length == 0 || timestamps[0] != 0) {
            throw new IllegalArgumentException("Timeline requires a positive duration and timestamps starting at zero");
        }
        for (int i = 0; i < timestamps.length; i++) {
            if (timestamps[i] < 0 || timestamps[i] >= durationMicros || (i > 0 && timestamps[i] <= timestamps[i - 1])) {
                throw new IllegalArgumentException("Timeline timestamps must strictly increase before duration");
            }
        }
        this.durationMicros = durationMicros;
        this.timestamps = timestamps.clone();
        this.nanoClock = Objects.requireNonNull(nanoClock);
    }

    public void play() {
        if (playing) return;
        if (anchorPositionMicros >= durationMicros) anchorPositionMicros = 0;
        anchorNanos = nanoClock.getAsLong();
        playing = true;
    }

    public void pause() {
        anchorPositionMicros = positionMicros();
        playing = false;
    }

    public void stop() {
        playing = false;
        anchorPositionMicros = 0;
    }

    public void seekMicros(long positionMicros) {
        anchorPositionMicros = Math.max(0, Math.min(durationMicros, positionMicros));
        anchorNanos = nanoClock.getAsLong();
    }

    public long positionMicros() {
        if (!playing) return anchorPositionMicros;
        long elapsedMicros = Math.max(0, nanoClock.getAsLong() - anchorNanos) / 1000;
        if (elapsedMicros >= durationMicros - anchorPositionMicros) {
            anchorPositionMicros = durationMicros;
            playing = false;
            return durationMicros;
        }
        return anchorPositionMicros + elapsedMicros;
    }

    public int currentFrameIndex() {
        return frameAtMicros(positionMicros());
    }

    public int frameAtMicros(long positionMicros) {
        int result = Arrays.binarySearch(timestamps, positionMicros);
        return result >= 0 ? result : Math.max(0, -result - 2);
    }

    public boolean isPlaying() {
        positionMicros();
        return playing;
    }

    public long durationMicros() {
        return durationMicros;
    }
}
