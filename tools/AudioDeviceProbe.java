import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.AudioSystem;
import javax.sound.sampled.Clip;
import javax.sound.sampled.DataLine;
import javax.sound.sampled.SourceDataLine;

/**
 * Standalone Java Sound clock probe; no Minecraft classes or external libraries.
 *
 * <pre>
 * javac -d /tmp/audio-device-probe tools/AudioDeviceProbe.java
 * java -cp /tmp/audio-device-probe AudioDeviceProbe mode=clip
 * java -cp /tmp/audio-device-probe AudioDeviceProbe mode=line
 * </pre>
 *
 * Writes strict JSON Lines to stdout. The sound is an explicitly synthetic,
 * six-second 440 Hz stereo tone, not audio from the Bad Apple reference.
 * SourceDataLine has no seek API: its seek phases reset the PCM source cursor
 * after stopping/flushing, while retaining the device's cumulative raw clock.
 */
public final class AudioDeviceProbe {
    private static final int SAMPLE_RATE = 48_000;
    private static final int FRAME_BYTES = 4;
    private static final int DURATION_SECONDS = 6;
    private static final int REQUESTED_LINE_BUFFER_BYTES = 3_840; // 20 ms.
    private static final int WRITE_BLOCK_BYTES = 3_840;
    private static final int INITIAL_SAMPLE_COUNT = 60;
    private static final int SEEK_SAMPLE_COUNT = 15;
    private static final long SAMPLE_INTERVAL_NANOS = 100_000_000L;
    private static final AudioFormat FORMAT =
            new AudioFormat(SAMPLE_RATE, 16, 2, true, false);
    private static final long PROCESS_START_NANOS = System.nanoTime();

    private AudioDeviceProbe() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            throw new IllegalArgumentException("Usage: AudioDeviceProbe mode=clip|line");
        }
        String mode = args[0].startsWith("mode=") ? args[0].substring(5) : args[0];
        if (!mode.equals("clip") && !mode.equals("line")) {
            throw new IllegalArgumentException("Usage: AudioDeviceProbe mode=clip|line");
        }
        System.out.printf(Locale.ROOT,
                "{\"type\":\"configuration\",\"mode\":%s,\"javaVersion\":%s,"
                + "\"javaVendor\":%s,\"osName\":%s,\"audio\":\"synthetic_tone\","
                + "\"frequencyHz\":440,\"durationSeconds\":6,\"sampleRate\":48000,"
                + "\"channels\":2,\"encoding\":\"PCM_SIGNED_s16le\","
                + "\"peakAmplitude\":4096,\"requestedLineBufferBytes\":3840,"
                + "\"writeBlockBytes\":3840,\"sampleIntervalMs\":100,"
                + "\"initialObservationMs\":6000,\"seekObservationMs\":1500}%n",
                quote(mode), quote(System.getProperty("java.version")),
                quote(System.getProperty("java.vendor")), quote(System.getProperty("os.name")));
        byte[] pcm = syntheticTone();
        if (mode.equals("clip")) {
            runClip(pcm);
        } else {
            runLine(pcm);
        }
    }

    private static byte[] syntheticTone() {
        byte[] pcm = new byte[SAMPLE_RATE * DURATION_SECONDS * FRAME_BYTES];
        for (int frame = 0; frame < SAMPLE_RATE * DURATION_SECONDS; frame++) {
            short sample = (short) Math.round(4096.0
                    * Math.sin(2.0 * Math.PI * 440.0 * frame / SAMPLE_RATE));
            int offset = frame * FRAME_BYTES;
            pcm[offset] = (byte) sample;
            pcm[offset + 1] = (byte) (sample >> 8);
            pcm[offset + 2] = (byte) sample;
            pcm[offset + 3] = (byte) (sample >> 8);
        }
        return pcm;
    }

    private static void runClip(byte[] pcm) throws Exception {
        Clip clip = AudioSystem.getClip();
        try {
            clip.open(FORMAT, pcm, 0, pcm.length);
            clipPhase(clip, "initial", 0L);
            clipPhase(clip, "rewind_zero", 0L);
            clipPhase(clip, "seek_three_seconds", 3_000_000L);
        } finally {
            if (clip.isOpen()) {
                snapshot("clip", "close", "before_close", clip, 0L, 0L, -1L);
            }
            clip.close();
            closed("clip", clip.isOpen());
        }
    }

    private static void clipPhase(Clip clip, String phase, long targetUs)
            throws InterruptedException {
        snapshot("clip", phase, "before_stop", clip, 0L, targetUs, -1L);
        clip.stop();
        snapshot("clip", phase, "after_stop", clip, 0L, targetUs, -1L);
        clip.flush();
        snapshot("clip", phase, "after_flush", clip, 0L, targetUs, -1L);
        clip.setMicrosecondPosition(targetUs);
        snapshot("clip", phase, "after_seek", clip, 0L, targetUs, -1L);
        snapshot("clip", phase, "before_start", clip, 0L, targetUs, -1L);
        long start = System.nanoTime();
        clip.start();
        snapshot("clip", phase, "after_start", clip, start, targetUs, -1L);
        int sampleCount = phase.equals("initial") ? INITIAL_SAMPLE_COUNT : SEEK_SAMPLE_COUNT;
        for (int sample = 1; sample <= sampleCount; sample++) {
            sleepUntil(start + sample * SAMPLE_INTERVAL_NANOS);
            snapshot("clip", phase, "sample_" + sample, clip, start, targetUs, -1L);
        }
    }

    private static void runLine(byte[] pcm) throws Exception {
        SourceDataLine line = AudioSystem.getSourceDataLine(FORMAT);
        try {
            line.open(FORMAT, REQUESTED_LINE_BUFFER_BYTES);
            linePhase(line, pcm, "initial", 0L);
            linePhase(line, pcm, "rewind_zero", 0L);
            linePhase(line, pcm, "seek_three_seconds", 3_000_000L);
        } finally {
            if (line.isOpen()) {
                snapshot("line", "close", "before_close", line, 0L, 0L, -1L);
            }
            line.close();
            closed("line", line.isOpen());
        }
    }

    private static void linePhase(SourceDataLine line, byte[] pcm, String phase,
                                  long targetUs) throws Exception {
        snapshot("line", phase, "before_stop", line, 0L, targetUs, 0L);
        line.stop();
        snapshot("line", phase, "after_stop", line, 0L, targetUs, 0L);
        line.flush();
        snapshot("line", phase, "after_flush", line, 0L, targetUs, 0L);
        int sourceOffset = Math.toIntExact(targetUs * SAMPLE_RATE / 1_000_000L) * FRAME_BYTES;
        AtomicLong writtenBytes = new AtomicLong();
        AtomicBoolean writing = new AtomicBoolean(true);
        AtomicReference<Throwable> writerFailure = new AtomicReference<>();
        Thread writer = new Thread(() -> {
            int offset = sourceOffset;
            try {
                while (writing.get() && offset < pcm.length) {
                    // One producer writes no more than the reported free space,
                    // so stopping the producer does not depend on a blocked write.
                    int count = Math.min(WRITE_BLOCK_BYTES,
                            Math.min(line.available(), pcm.length - offset));
                    count -= count % FRAME_BYTES;
                    if (count == 0) {
                        Thread.sleep(1L);
                        continue;
                    }
                    int accepted = line.write(pcm, offset, count);
                    if (accepted < 0 || accepted % FRAME_BYTES != 0) {
                        throw new IllegalStateException("Invalid write size: " + accepted);
                    }
                    offset += accepted;
                    writtenBytes.addAndGet(accepted);
                    if (accepted == 0) {
                        Thread.sleep(1L);
                    }
                }
            } catch (Throwable failure) {
                writerFailure.set(failure);
            }
        }, "synthetic-tone-writer-" + phase);
        writer.setDaemon(true);
        snapshot("line", phase, "source_cursor_reset", line, 0L, targetUs, 0L);
        snapshot("line", phase, "before_start", line, 0L, targetUs, 0L);
        long start = System.nanoTime();
        line.start();
        snapshot("line", phase, "after_start", line, start, targetUs, 0L);
        writer.start();
        try {
            int sampleCount = phase.equals("initial") ? INITIAL_SAMPLE_COUNT : SEEK_SAMPLE_COUNT;
            for (int sample = 1; sample <= sampleCount; sample++) {
                sleepUntil(start + sample * SAMPLE_INTERVAL_NANOS);
                snapshot("line", phase, "sample_" + sample, line, start, targetUs,
                        writtenBytes.get());
            }
        } finally {
            writing.set(false);
            writer.join(1_000L);
            if (writer.isAlive()) {
                // A broken/blocked implementation must not hang a diagnostic CI job.
                line.close();
                writer.join(1_000L);
                throw new IllegalStateException("Audio writer did not stop promptly");
            }
        }
        if (writerFailure.get() != null) {
            throw new IllegalStateException("Audio writer failed", writerFailure.get());
        }
        snapshot("line", phase, "writer_stopped", line, start, targetUs, writtenBytes.get());
    }

    private static void snapshot(String mode, String phase, String event, DataLine line,
                                 long phaseStart, long targetUs, long writtenBytes) {
        long now = System.nanoTime();
        long phaseElapsedUs = phaseStart == 0L ? -1L : (now - phaseStart) / 1_000L;
        long rawUs = line.getMicrosecondPosition();
        long rawFrames = line.getLongFramePosition();
        long clipLengthUs = line instanceof Clip clip ? clip.getMicrosecondLength() : -1L;
        int clipFrames = line instanceof Clip clip ? clip.getFrameLength() : -1;
        System.out.printf(Locale.ROOT,
                "{\"type\":\"sample\",\"mode\":%s,\"phase\":%s,\"event\":%s,"
                + "\"processElapsedUs\":%d,\"phaseElapsedUs\":%d,\"targetMediaUs\":%d,"
                + "\"rawClockUs\":%d,\"longFramePosition\":%d,\"running\":%s,"
                + "\"active\":%s,\"open\":%s,\"bufferBytes\":%d,\"availableBytes\":%d,"
                + "\"writtenBytes\":%d,\"clipLengthUs\":%d,\"clipLengthFrames\":%d}%n",
                quote(mode), quote(phase), quote(event), (now - PROCESS_START_NANOS) / 1_000L,
                phaseElapsedUs, targetUs, rawUs, rawFrames, line.isRunning(), line.isActive(),
                line.isOpen(), line.getBufferSize(), line.available(), writtenBytes,
                clipLengthUs, clipFrames);
    }

    private static void closed(String mode, boolean open) {
        System.out.printf(Locale.ROOT,
                "{\"type\":\"closed\",\"mode\":%s,\"open\":%s}%n", quote(mode), open);
    }

    private static void sleepUntil(long deadlineNanos) throws InterruptedException {
        long remaining;
        while ((remaining = deadlineNanos - System.nanoTime()) > 0L) {
            Thread.sleep(remaining / 1_000_000L, (int) (remaining % 1_000_000L));
        }
    }

    private static String quote(String value) {
        if (value == null) {
            return "null";
        }
        StringBuilder escaped = new StringBuilder("\"");
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '\\' -> escaped.append("\\\\");
                case '"' -> escaped.append("\\\"");
                case '\n' -> escaped.append("\\n");
                case '\r' -> escaped.append("\\r");
                case '\t' -> escaped.append("\\t");
                default -> {
                    if (character < 0x20) {
                        escaped.append(String.format(Locale.ROOT, "\\u%04x", (int) character));
                    } else {
                        escaped.append(character);
                    }
                }
            }
        }
        return escaped.append('"').toString();
    }
}
