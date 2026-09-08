package dev.legend.badapple.playback;

import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.AudioInputStream;
import javax.sound.sampled.AudioSystem;
import javax.sound.sampled.Clip;
import javax.sound.sampled.Control;
import javax.sound.sampled.DataLine;
import javax.sound.sampled.Line;
import javax.sound.sampled.LineEvent;
import javax.sound.sampled.LineListener;
import javax.sound.sampled.LineUnavailableException;
import javax.sound.sampled.SourceDataLine;
import java.io.IOException;
import java.util.Arrays;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Executor;
import java.util.concurrent.Executors;
import java.util.concurrent.locks.LockSupport;

/**
 * Seekable PCM playback using a small, explicitly sized public Java Sound line.
 * OpenJDK DirectClip hard-codes a one-second device buffer; on ALSA/Pulse its
 * available-buffer clock can consequently begin almost one second ahead.
 * This adapter bounds that buffering to 20 ms without JDK internals/reflection.
 * Control calls serialize, and a previous producer must exit before a seek or
 * restart can launch another producer. Audio remains bounded to 256 MiB.
 */
final class StreamingAudioClip implements Clip {
    private static final int MAX_PCM_BYTES = 256 * 1024 * 1024;
    private static final int BUFFER_MILLIS = 20;
    private static final Executor EVENTS = Executors.newSingleThreadExecutor(task -> {
        Thread dispatcher = new Thread(task, "Bad Apple audio events");
        dispatcher.setDaemon(true);
        return dispatcher;
    });
    private final Object state = new Object();
    private final CopyOnWriteArrayList<LineListener> listeners = new CopyOnWriteArrayList<>();
    private SourceDataLine line;
    private AudioFormat format;
    private byte[] pcm;
    private int frameLength;
    private int writeBlockBytes;
    private volatile boolean opened;
    private volatile boolean running;
    private volatile long generation;
    private volatile RuntimeException failure;
    private volatile Thread producer;
    private long frozenFrame;
    private long startFrame;
    private long deviceFrameOrigin;

    StreamingAudioClip() {}

    StreamingAudioClip(SourceDataLine line) {
        this.line = line;
    }

    @Override
    public synchronized void open(AudioFormat requested, byte[] data, int offset, int length)
            throws LineUnavailableException {
        if (opened) throw new IllegalStateException("Audio clip is already open");
        validateFormat(requested);
        if (data == null || offset < 0 || length <= 0 || length > MAX_PCM_BYTES
                || (long) offset + length > data.length || length % requested.getFrameSize() != 0) {
            throw new IllegalArgumentException("PCM data must be bounded, nonempty, whole sample frames");
        }
        byte[] owned = Arrays.copyOfRange(data, offset, offset + length);
        if (line == null) line = AudioSystem.getSourceDataLine(requested);
        int bufferFrames = Math.max(1, (int) Math.ceil(requested.getFrameRate() * BUFFER_MILLIS / 1000.0));
        int bufferBytes = Math.multiplyExact(bufferFrames, requested.getFrameSize());
        try {
            line.open(requested, bufferBytes);
        } catch (LineUnavailableException | RuntimeException error) {
            line.close();
            throw error;
        }
        synchronized (state) {
            format = requested;
            pcm = owned;
            frameLength = length / requested.getFrameSize();
            writeBlockBytes = bufferBytes;
            frozenFrame = 0;
            failure = null;
            running = false;
            opened = true;
        }
        emit(LineEvent.Type.OPEN, 0);
    }

    @Override
    public synchronized void open(AudioInputStream stream) throws LineUnavailableException, IOException {
        if (opened) throw new IllegalStateException("Audio clip is already open");
        AudioFormat requested = stream.getFormat();
        validateFormat(requested);
        long frames = stream.getFrameLength();
        if (frames <= 0 || frames > MAX_PCM_BYTES / requested.getFrameSize()) {
            throw new IOException("PCM audio must declare a bounded positive frame count");
        }
        int length = (int) (frames * requested.getFrameSize());
        byte[] data = stream.readNBytes(length);
        if (data.length != length) throw new IOException("PCM audio ended before its declared frame count");
        open(requested, data, 0, length);
    }

    private static void validateFormat(AudioFormat format) {
        boolean pcm = AudioFormat.Encoding.PCM_SIGNED.equals(format.getEncoding())
                || AudioFormat.Encoding.PCM_UNSIGNED.equals(format.getEncoding());
        if (!pcm || !Float.isFinite(format.getFrameRate()) || format.getFrameRate() <= 0
                || format.getFrameRate() > 384_000 || format.getFrameSize() <= 0
                || format.getFrameSize() > 32 || format.getChannels() <= 0
                || !Float.isFinite(format.getSampleRate()) || format.getSampleRate() <= 0
                || format.getSampleSizeInBits() <= 0) {
            throw new IllegalArgumentException("Audio must use a fully specified bounded PCM format");
        }
    }

    @Override
    public synchronized void start() {
        requireOpen();
        checkFailure();
        if (running || frozenFrame >= frameLength) return;
        awaitProducer();
        final long epoch;
        final int offset;
        synchronized (state) {
            startFrame = frozenFrame;
            deviceFrameOrigin = line.getLongFramePosition();
            offset = Math.toIntExact(startFrame * format.getFrameSize());
            epoch = ++generation;
            running = true;
        }
        try {
            line.start();
            Thread worker = new Thread(() -> produce(epoch, offset), "Bad Apple PCM playback");
            worker.setDaemon(true);
            producer = worker;
            worker.start();
            emit(LineEvent.Type.START, startFrame);
        } catch (RuntimeException error) {
            synchronized (state) { running = false; generation++; }
            throw error;
        }
    }

    private void produce(long epoch, int initialOffset) {
        int offset = initialOffset;
        try {
            while (running && generation == epoch && offset < pcm.length) {
                int count = Math.min(writeBlockBytes, Math.min(line.available(), pcm.length - offset));
                count -= count % format.getFrameSize();
                if (count <= 0) {
                    LockSupport.parkNanos(1_000_000L);
                    continue;
                }
                int written = line.write(pcm, offset, count);
                if (written < 0 || written > count || written % format.getFrameSize() != 0) {
                    throw new IllegalStateException("Audio device returned an invalid PCM write count");
                }
                offset += written;
                if (written == 0) LockSupport.parkNanos(1_000_000L);
            }
            if (!running || generation != epoch) return;
            line.drain();
            boolean ended;
            synchronized (state) {
                ended = running && generation == epoch;
                if (ended) {
                    frozenFrame = frameLength;
                    running = false;
                }
            }
            if (ended) {
                line.stop();
                emit(LineEvent.Type.STOP, frameLength);
            }
        } catch (RuntimeException error) {
            synchronized (state) {
                if (generation == epoch) {
                    failure = new IllegalStateException("PCM audio producer failed", error);
                    running = false;
                }
            }
        }
    }

    @Override
    public synchronized void stop() {
        if (!opened) return;
        boolean stopped;
        long position;
        synchronized (state) {
            stopped = running;
            // Capture rendered time before flush turns queued bytes into native position.
            position = currentFrameLocked();
            frozenFrame = position;
            running = false;
            generation++;
        }
        line.stop();
        line.flush();
        awaitProducer();
        // A write already past its cancellation check may finish after the first
        // flush. Once joined, flush again so no old-generation PCM can survive.
        line.flush();
        if (stopped) emit(LineEvent.Type.STOP, position);
    }

    private void awaitProducer() {
        Thread worker = producer;
        if (worker == null || worker == Thread.currentThread()) return;
        LockSupport.unpark(worker);
        try {
            worker.join(1000);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            line.close();
            throw new IllegalStateException("Interrupted while stopping PCM producer", interrupted);
        }
        if (worker.isAlive()) {
            line.close();
            throw new IllegalStateException("PCM producer did not stop; audio device was closed");
        }
        producer = null;
    }

    @Override
    public synchronized void setFramePosition(int frames) {
        requireOpen();
        boolean resume = running;
        stop();
        synchronized (state) { frozenFrame = Math.max(0, Math.min(frameLength, frames)); }
        if (resume) start();
    }

    @Override
    public void setMicrosecondPosition(long micros) {
        long frames = (long) (Math.max(0, micros) * (double) getFormat().getFrameRate() / 1_000_000.0);
        setFramePosition((int) Math.min(frameLength, frames));
    }

    private long currentFrameLocked() {
        if (!running) return frozenFrame;
        long elapsedFrames = Math.max(0, line.getLongFramePosition() - deviceFrameOrigin);
        return Math.min(frameLength, startFrame + elapsedFrames);
    }

    @Override
    public long getLongFramePosition() {
        checkFailure();
        synchronized (state) { return currentFrameLocked(); }
    }

    @Override public int getFramePosition() { return (int) getLongFramePosition(); }
    @Override public long getMicrosecondPosition() { return framesToMicros(getLongFramePosition()); }
    @Override public long getMicrosecondLength() { return framesToMicros(frameLength); }
    @Override public int getFrameLength() { return frameLength; }
    @Override public AudioFormat getFormat() { return format; }
    @Override public int getBufferSize() { return line == null ? 0 : line.getBufferSize(); }
    @Override public int available() { return line == null ? 0 : line.available(); }
    @Override public boolean isRunning() { return running; }
    @Override public boolean isActive() { return running && line.isActive(); }
    @Override public boolean isOpen() { return opened; }
    @Override public float getLevel() { return line == null ? AudioSystem.NOT_SPECIFIED : line.getLevel(); }
    @Override public Line.Info getLineInfo() { return new DataLine.Info(Clip.class, format); }
    @Override public Control[] getControls() { return line == null ? new Control[0] : line.getControls(); }
    @Override public boolean isControlSupported(Control.Type type) { return line != null && line.isControlSupported(type); }
    @Override public Control getControl(Control.Type type) {
        if (line == null) throw new IllegalArgumentException("Audio line is not open");
        return line.getControl(type);
    }
    @Override public void addLineListener(LineListener listener) { listeners.addIfAbsent(listener); }
    @Override public void removeLineListener(LineListener listener) { listeners.remove(listener); }
    @Override public void drain() { if (line != null) line.drain(); }

    @Override
    public synchronized void flush() {
        boolean resume = running;
        stop();
        if (resume) start();
    }

    @Override
    public void open() throws LineUnavailableException {
        if (!opened) throw new LineUnavailableException("Open this clip with PCM audio data first");
    }

    @Override
    public void setLoopPoints(int start, int end) {
        throw new UnsupportedOperationException("Looping is controlled by the playback timeline");
    }

    @Override
    public void loop(int count) {
        if (count != 0) throw new UnsupportedOperationException("Looping is controlled by the playback timeline");
        start();
    }

    @Override
    public synchronized void close() {
        if (!opened) return;
        RuntimeException problem = null;
        try {
            stop();
        } catch (RuntimeException error) {
            problem = error;
        } finally {
            synchronized (state) { running = false; generation++; opened = false; }
            line.close();
            // Only release the array once the producer has really exited.
            if (producer == null || !producer.isAlive()) pcm = null;
        }
        emit(LineEvent.Type.CLOSE, frozenFrame);
        if (problem != null) throw problem;
    }

    private long framesToMicros(long frames) {
        return format == null ? 0 : (long) (frames * 1_000_000.0 / format.getFrameRate());
    }

    private void requireOpen() {
        if (!opened) throw new IllegalStateException("Audio clip is closed");
    }

    private void checkFailure() {
        RuntimeException problem = failure;
        if (problem != null) throw problem;
    }

    private void emit(LineEvent.Type type, long frames) {
        if (listeners.isEmpty()) return;
        LineEvent event = new LineEvent(this, type, frames);
        LineListener[] recipients = listeners.toArray(LineListener[]::new);
        // Never run application callbacks on a producer which stop() must join.
        // Like Java Sound's dispatcher, listener delivery is asynchronous.
        EVENTS.execute(() -> {
            for (LineListener listener : recipients) {
                try {
                    listener.update(event);
                } catch (RuntimeException ignored) {
                    // A client listener must not turn valid PCM playback into an audio failure.
                }
            }
        });
    }
}
