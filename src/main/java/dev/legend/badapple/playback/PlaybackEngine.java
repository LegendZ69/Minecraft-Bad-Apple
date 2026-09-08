package dev.legend.badapple.playback;

import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.AudioInputStream;
import javax.sound.sampled.AudioSystem;
import javax.sound.sampled.Clip;
import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.function.LongSupplier;

/** Audio-led playback with a monotonic silent fallback. All public methods are thread safe. */
public final class PlaybackEngine implements Closeable {
    private static final long AUDIO_CLOCK_TOLERANCE_MICROS = 100_000;
    private static final long AUDIO_SEEK_TOLERANCE_MICROS = 1000;
    private static final long AUDIO_STALL_TIMEOUT_NANOS = 2_000_000_000L;
    private final VideoArchive archive;
    private final Timeline timeline;
    private final LongSupplier nanoClock;
    private Clip audio;
    private boolean audioDriving;
    private boolean looping;
    private boolean playRequested;
    private boolean closed;
    private String warning;
    private long lastAudioPosition;
    private long lastAudioAdvanceNanos;
    private long audioStartPositionMicros;
    private long audioStartNanos;
    private boolean audioSeekPending;

    public static PlaybackEngine open(Path path) throws IOException {
        return new PlaybackEngine(VideoArchive.open(path), System::nanoTime, true);
    }

    PlaybackEngine(VideoArchive archive, LongSupplier nanoClock, boolean initializeAudio) {
        this.archive = archive;
        this.nanoClock = nanoClock;
        this.timeline = new Timeline(archive.metadata().durationMicros(),
                archive.metadata().frameTimestampsMicros(), nanoClock);
        if (initializeAudio && archive.metadata().audio() != null) loadAudio();
    }

    /** Device injection keeps clock regressions deterministic without a physical audio device. */
    PlaybackEngine(VideoArchive archive, LongSupplier nanoClock, Clip audio) {
        this(archive, nanoClock, false);
        this.audio = audio;
    }

    public VideoArchive archive() {
        return archive;
    }

    public VideoArchive.Metadata metadata() {
        return archive.metadata();
    }

    public synchronized void play() {
        requireOpen();
        update();
        if (playRequested) return;
        timeline.play();
        playRequested = true;
        startAudio(timeline.positionMicros());
    }

    public synchronized void resume() {
        play();
    }

    public synchronized void pause() {
        requireOpen();
        update();
        timeline.pause();
        playRequested = false;
        stopAudio();
    }

    public synchronized void stop() {
        requireOpen();
        stopAudio();
        timeline.stop();
        playRequested = false;
    }

    public synchronized void seekSeconds(double seconds) {
        requireOpen();
        if (!Double.isFinite(seconds)) throw new IllegalArgumentException("Seek position must be finite");
        boolean wasPlaying = playRequested;
        long micros = (long) (Math.max(0, Math.min(durationSeconds(), seconds)) * 1_000_000);
        stopAudio();
        timeline.seekMicros(micros);
        if (wasPlaying && micros < timeline.durationMicros()) startAudio(micros);
    }

    /** Advances playback and returns the frame to display; late rendering skips directly to its timestamp. */
    public synchronized int update() {
        requireOpen();
        if (audioDriving && audio != null) {
            try {
                long now = nanoClock.getAsLong();
                long rawPosition = audio.getMicrosecondPosition();
                long elapsedMicros = Math.max(0, now - audioStartNanos) / 1000;
                // DirectClip may expose the old device position while a seek is being applied.
                // Never latch it as a new clock origin, or a backward seek can remain stuck at
                // the previous timestamp. The elapsed-time bound also rejects buffered jumps.
                long tolerance = audioSeekPending ? AUDIO_SEEK_TOLERANCE_MICROS : AUDIO_CLOCK_TOLERANCE_MICROS;
                boolean plausible = rawPosition >= Math.max(0, audioStartPositionMicros - AUDIO_SEEK_TOLERANCE_MICROS)
                        && rawPosition <= audioStartPositionMicros + elapsedMicros + tolerance;
                if (plausible) {
                    audioSeekPending = false;
                    if (rawPosition > lastAudioPosition) {
                        lastAudioPosition = rawPosition;
                        lastAudioAdvanceNanos = now;
                    }
                }
                timeline.seekMicros(Math.min(lastAudioPosition, timeline.durationMicros()));
                if (!audioSeekPending && plausible && lastAudioPosition >= audio.getMicrosecondLength()) {
                    // The WAV may finish slightly before the final video frame; continue on the wall clock.
                    // Do not mix in a separately read frame position, which can belong to an old seek.
                    audioDriving = false;
                } else if (now - lastAudioAdvanceNanos > AUDIO_STALL_TIMEOUT_NANOS) {
                    // Recover elapsed time rather than silently losing the entire stall interval.
                    timeline.seekMicros(lastAudioPosition + (now - lastAudioAdvanceNanos) / 1000);
                    disableAudio(new IOException("audio device stopped advancing"));
                }
            } catch (RuntimeException error) {
                disableAudio(error);
            }
        }
        long position = timeline.positionMicros();
        if (position >= timeline.durationMicros()) {
            stopAudio();
            if (looping && playRequested) {
                timeline.stop();
                timeline.play();
                startAudio(0);
                position = 0;
            } else {
                playRequested = false;
            }
        }
        return timeline.frameAtMicros(position);
    }

    public synchronized int currentFrameIndex() {
        return update();
    }

    public synchronized double positionSeconds() {
        update();
        return timeline.positionMicros() / 1_000_000.0;
    }

    public double durationSeconds() {
        return timeline.durationMicros() / 1_000_000.0;
    }

    public int width() {
        return metadata().width();
    }

    public int height() {
        return metadata().height();
    }

    public synchronized boolean isPlaying() {
        update();
        return timeline.isPlaying();
    }

    public synchronized void setLooping(boolean looping) {
        this.looping = looping;
    }

    public synchronized boolean isLooping() {
        return looping;
    }

    /** Null when no warning exists; intentionally silent archives do not produce a warning. */
    public synchronized String warning() {
        return warning;
    }

    private void loadAudio() {
        Clip candidate = null;
        try (InputStream raw = archive.openAudioStream()) {
            if (raw == null) return;
            raw.mark(12);
            byte[] header = raw.readNBytes(12);
            raw.reset();
            if (header.length != 12 || !new String(header, 0, 4, StandardCharsets.US_ASCII).equals("RIFF")
                    || !new String(header, 8, 4, StandardCharsets.US_ASCII).equals("WAVE")) {
                throw new IOException("audio.wav must be a PCM WAV file");
            }
            try (AudioInputStream decoded = AudioSystem.getAudioInputStream(raw)) {
                AudioFormat format = decoded.getFormat();
                boolean pcm = AudioFormat.Encoding.PCM_SIGNED.equals(format.getEncoding())
                        || AudioFormat.Encoding.PCM_UNSIGNED.equals(format.getEncoding());
                long frameLength = decoded.getFrameLength();
                int frameSize = format.getFrameSize();
                if (!pcm || frameSize <= 0 || frameSize > 32 || frameLength <= 0
                        || frameLength > VideoArchive.MAX_AUDIO_BYTES / frameSize) {
                    throw new IOException("Audio must be bounded PCM WAV data under 256 MiB");
                }
                candidate = new StreamingAudioClip();
                candidate.open(decoded);
                audio = candidate;
            }
        } catch (Exception unavailable) {
            if (candidate != null) {
                try {
                    candidate.close();
                } catch (RuntimeException ignored) {
                    // Cleanup must not hide the original audio-device failure.
                }
            }
            warning = audioWarning(unavailable);
        }
    }

    private void startAudio(long positionMicros) {
        if (audio == null) return;
        try {
            audio.stop();
            audio.flush();
            if (positionMicros >= audio.getMicrosecondLength()) {
                audioDriving = false;
                return;
            }
            audio.setMicrosecondPosition(positionMicros);
            // setMicrosecondPosition need not be reflected in the native position immediately.
            // The requested target, not an immediate read-back, is the only safe seek origin.
            lastAudioPosition = positionMicros;
            audioStartPositionMicros = positionMicros;
            audioStartNanos = nanoClock.getAsLong();
            lastAudioAdvanceNanos = audioStartNanos;
            audioSeekPending = true;
            audioDriving = true;
            audio.start();
        } catch (RuntimeException error) {
            disableAudio(error);
        }
    }

    private void stopAudio() {
        audioDriving = false;
        audioSeekPending = false;
        if (audio == null) return;
        try {
            audio.stop();
        } catch (RuntimeException error) {
            disableAudio(error);
        }
    }

    private void disableAudio(Exception error) {
        audioDriving = false;
        audioSeekPending = false;
        warning = audioWarning(error);
        if (audio != null) {
            try {
                audio.close();
            } catch (RuntimeException ignored) {
                // Preserve the original device failure and retain the silent timeline.
            }
            audio = null;
        }
    }

    private static String audioWarning(Exception error) {
        String reason = error.getMessage();
        if (reason == null || reason.isBlank()) reason = error.getClass().getSimpleName();
        return "Audio unavailable; playing silently: " + reason;
    }

    private void requireOpen() {
        if (closed) throw new IllegalStateException("Playback engine is closed");
    }

    @Override
    public synchronized void close() throws IOException {
        if (closed) return;
        closed = true;
        playRequested = false;
        timeline.stop();
        try {
            if (audio != null) audio.close();
        } finally {
            audio = null;
            archive.close();
        }
    }
}
