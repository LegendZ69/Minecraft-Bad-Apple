package dev.legend.badapple.playback;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.AudioInputStream;
import javax.sound.sampled.LineEvent;
import javax.sound.sampled.SourceDataLine;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;

import static org.junit.jupiter.api.Assertions.*;

/** Public Java Sound behavior, exercised without a physical mixer or timing-sensitive sleeps. */
@Timeout(10)
class StreamingAudioClipTest {
    private static final AudioFormat MONO = new AudioFormat(8_000, 16, 1, true, false);
    private static final AudioFormat STEREO = new AudioFormat(48_000, 16, 2, true, false);

    @Test
    void forwardsExactPcmBytesWithTwentyMillisecondDeviceBuffer() throws Exception {
        SimulatedLine device = new SimulatedLine();
        device.autoRender = true;
        byte[] expected = pcm(1_280);
        try (StreamingAudioClip clip = new StreamingAudioClip(device.line)) {
            clip.open(MONO, expected, 0, expected.length);
            assertEquals(320, device.requestedBufferBytes);
            clip.start();
            device.awaitCaptured(expected.length);
            assertArrayEquals(expected, device.capturedBytes());
            assertTrue(device.writeSizes().stream().allMatch(size -> size > 0 && size <= 320 && size % 2 == 0));
        }
    }

    @Test
    void audioInputStreamAndByteArrayRangesPreserveTheirExactSamples() throws Exception {
        byte[] source = pcm(960);
        byte[] expected = Arrays.copyOfRange(source, 160, 800);
        SimulatedLine ranged = new SimulatedLine();
        ranged.autoRender = true;
        try (StreamingAudioClip clip = new StreamingAudioClip(ranged.line)) {
            clip.open(MONO, source, 160, expected.length);
            clip.start();
            ranged.awaitCaptured(expected.length);
            assertArrayEquals(expected, ranged.capturedBytes());
        }

        SimulatedLine streamed = new SimulatedLine();
        streamed.autoRender = true;
        try (StreamingAudioClip clip = new StreamingAudioClip(streamed.line);
             AudioInputStream input = new AudioInputStream(new ByteArrayInputStream(expected),
                     MONO, expected.length / MONO.getFrameSize())) {
            clip.open(input);
            clip.start();
            streamed.awaitCaptured(expected.length);
            assertArrayEquals(expected, streamed.capturedBytes());
        }
    }

    @Test
    void seekUsesRenderedClockRelativeToTargetAndPauseIgnoresNativeFlushJump() throws Exception {
        SimulatedLine device = new SimulatedLine();
        device.nativeFrames = 24_000;
        device.flushJumpFrames = 8_000;
        try (StreamingAudioClip clip = new StreamingAudioClip(device.line)) {
            clip.open(MONO, pcm(4_000), 0, 4_000);
            clip.start();
            device.awaitQueued(320);
            device.renderFrames(160);
            assertEquals(160, clip.getLongFramePosition());
            assertEquals(20_000, clip.getMicrosecondPosition());
            clip.stop();
            assertEquals(160, clip.getLongFramePosition());
            assertEquals(20_000, clip.getMicrosecondPosition());
            clip.setFramePosition(400);
            assertEquals(400, clip.getLongFramePosition());
            device.advanceNativeClock(500);
            assertEquals(400, clip.getLongFramePosition());
            clip.start();
            device.awaitQueued(320);
            device.renderFrames(80);
            assertEquals(480, clip.getLongFramePosition());
            assertEquals(60_000, clip.getMicrosecondPosition());
        }
    }

    @Test
    void writesStayFrameAlignedWithinBothAvailableCapacityAndTwentyMilliseconds() throws Exception {
        SimulatedLine device = new SimulatedLine();
        device.autoRender = true;
        device.availableLimit = 12;
        byte[] expected = pcm(8_000);
        try (StreamingAudioClip clip = new StreamingAudioClip(device.line)) {
            clip.open(STEREO, expected, 0, expected.length);
            assertEquals(3_840, device.requestedBufferBytes);
            clip.start();
            device.awaitCaptured(expected.length);
            assertArrayEquals(expected, device.capturedBytes());
            assertTrue(device.writeSizes().stream().allMatch(size -> size > 0 && size <= 12 && size % 4 == 0));
        }
    }

    @Test
    void stopWaitsForOldWriterBeforeSeekAndRestart() throws Exception {
        SimulatedLine device = new SimulatedLine();
        device.blockFirstWrite = true;
        var commands = Executors.newSingleThreadExecutor();
        try (StreamingAudioClip clip = new StreamingAudioClip(device.line)) {
            clip.open(MONO, pcm(1_280), 0, 1_280);
            clip.start();
            assertTrue(device.writeEntered.await(2, TimeUnit.SECONDS), "writer did not enter its first write");
            Thread firstWriter = device.lastWriter;
            var stopped = commands.submit(clip::stop);
            assertTrue(device.stopEntered.await(2, TimeUnit.SECONDS), "native stop was not invoked");
            assertFalse(stopped.isDone(), "stop returned while the old writer was still inside write");
            device.releaseWrite.countDown();
            stopped.get(2, TimeUnit.SECONDS);
            assertFalse(firstWriter.isAlive(), "old worker must be joined before stop returns");
            assertEquals(0, device.activeWrites.get());
            assertEquals(0, device.queuedBytes, "an in-flight write completing after the first flush must also be discarded");
            int previousBytes = device.capturedBytes().length;
            clip.setFramePosition(40);
            clip.start();
            device.awaitCaptured(previousBytes + 1);
            assertEquals(1, device.maximumConcurrentWrites.get());
        } finally {
            device.releaseWrite.countDown();
            commands.shutdownNow();
            assertTrue(commands.awaitTermination(2, TimeUnit.SECONDS));
        }
    }

    @Test
    void closeJoinsWorkerAndClosesNativeLineIdempotently() throws Exception {
        SimulatedLine device = new SimulatedLine();
        StreamingAudioClip clip = new StreamingAudioClip(device.line);
        try {
            clip.open(MONO, pcm(4_000), 0, 4_000);
            clip.start();
            device.awaitQueued(320);
            Thread writer = device.lastWriter;
            clip.close();
            assertTrue(device.closed);
            assertFalse(device.running);
            assertFalse(writer.isAlive());
            assertFalse(clip.isOpen());
            clip.close();
        } finally {
            clip.close();
        }
    }

    @Test
    void stopUnblocksNativeDrainAndDoesNotReplaceRenderedPositionWithEndOfPcm() throws Exception {
        SimulatedLine device = new SimulatedLine();
        device.blockDrain = true;
        var commands = Executors.newSingleThreadExecutor();
        try (StreamingAudioClip clip = new StreamingAudioClip(device.line)) {
            // All bytes fit in the device queue, but none have rendered when drain begins.
            clip.open(MONO, pcm(160), 0, 160);
            clip.start();
            assertTrue(device.drainEntered.await(2, TimeUnit.SECONDS), "writer did not reach native drain");
            Thread firstWriter = device.lastWriter;
            var stopped = commands.submit(clip::stop);
            stopped.get(1, TimeUnit.SECONDS);
            assertFalse(firstWriter.isAlive(), "draining producer was not joined");
            assertEquals(0, clip.getLongFramePosition(), "queued samples must not become rendered samples on stop");
            clip.setFramePosition(40);
            assertEquals(40, clip.getLongFramePosition(), "old drain completion must not overwrite the seek target");
        } finally {
            device.releaseDrain.countDown();
            commands.shutdownNow();
            assertTrue(commands.awaitTermination(2, TimeUnit.SECONDS));
        }
    }

    @Test
    void stoppedListenerCanStopAgainAndOtherListenerFailuresDoNotPoisonClock() throws Exception {
        SimulatedLine device = new SimulatedLine();
        device.autoRender = true;
        CountDownLatch callbackFinished = new CountDownLatch(1);
        AtomicReference<Throwable> callbackFailure = new AtomicReference<>();
        try (StreamingAudioClip clip = new StreamingAudioClip(device.line)) {
            clip.addLineListener(event -> {
                if (event.getType() == LineEvent.Type.STOP) {
                    throw new IllegalStateException("simulated application listener failure");
                }
            });
            clip.addLineListener(event -> {
                if (event.getType() == LineEvent.Type.STOP) {
                    try {
                        clip.stop();
                    } catch (Throwable error) {
                        callbackFailure.set(error);
                    } finally {
                        callbackFinished.countDown();
                    }
                }
            });
            clip.open(MONO, pcm(160), 0, 160);
            clip.start();
            assertTrue(callbackFinished.await(2, TimeUnit.SECONDS), "STOP listener deadlocked or was not notified");
            assertNull(callbackFailure.get());
            assertEquals(80, clip.getLongFramePosition());
            assertEquals(10_000, clip.getMicrosecondPosition());
        }
    }

    @Test
    void asynchronousWriteFailureIsVisibleToPlaybackClock() throws Exception {
        SimulatedLine device = new SimulatedLine();
        device.writeFailure = new IllegalStateException("simulated native write failure");
        try (StreamingAudioClip clip = new StreamingAudioClip(device.line)) {
            clip.open(MONO, pcm(640), 0, 640);
            clip.start();
            assertTrue(device.writeEntered.await(2, TimeUnit.SECONDS));
            awaitCondition(() -> {
                try {
                    clip.getLongFramePosition();
                    return false;
                } catch (RuntimeException expected) {
                    return true;
                }
            }, "writer failure did not reach the clock");
            assertThrows(RuntimeException.class, clip::getMicrosecondPosition);
        }
    }

    @Test
    void rejectsIncompleteFramesAndNonPcmDataBeforeStartingAWriter() throws Exception {
        SimulatedLine device = new SimulatedLine();
        try (StreamingAudioClip clip = new StreamingAudioClip(device.line)) {
            assertThrows(IllegalArgumentException.class, () -> clip.open(STEREO, new byte[3], 0, 3));
            assertThrows(IllegalArgumentException.class, () -> clip.open(MONO, new byte[0], 0, 0));
            assertThrows(IllegalArgumentException.class, () -> clip.open(MONO, new byte[8], -1, 4));
            assertThrows(IllegalArgumentException.class, () -> clip.open(MONO, new byte[8], 6, 4));
            AudioFormat compressed = new AudioFormat(AudioFormat.Encoding.ULAW,
                    8_000, 8, 1, 1, 8_000, false);
            assertThrows(IllegalArgumentException.class, () -> clip.open(compressed, new byte[32], 0, 32));
            assertNull(device.lastWriter);
        }
    }

    @Test
    void rejectsTruncatedAndUnboundedAudioStreamsBeforeOpeningNativeLine() throws Exception {
        SimulatedLine device = new SimulatedLine();
        try (StreamingAudioClip clip = new StreamingAudioClip(device.line);
             AudioInputStream truncated = new AudioInputStream(new ByteArrayInputStream(new byte[16]), MONO, 16);
             AudioInputStream unbounded = new AudioInputStream(new ByteArrayInputStream(new byte[16]), MONO, -1)) {
            assertThrows(IOException.class, () -> clip.open(truncated));
            assertThrows(IOException.class, () -> clip.open(unbounded));
            assertFalse(device.open);
            assertNull(device.lastWriter);
        }
    }

    private static byte[] pcm(int length) {
        byte[] bytes = new byte[length];
        for (int index = 0; index < length; index++) bytes[index] = (byte) (index * 31 + 7);
        return bytes;
    }

    private static void awaitCondition(BooleanSupplier condition, String failure) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
        CountDownLatch delay = new CountDownLatch(1);
        while (!condition.getAsBoolean()) {
            assertTrue(System.nanoTime() < deadline, failure);
            delay.await(5, TimeUnit.MILLISECONDS);
        }
    }

    /** A bounded byte queue and an independently advanced, cumulative hardware frame clock. */
    private static final class SimulatedLine {
        private final ByteArrayOutputStream captured = new ByteArrayOutputStream();
        private final List<Integer> writes = new ArrayList<>();
        private final AtomicInteger activeWrites = new AtomicInteger();
        private final AtomicInteger maximumConcurrentWrites = new AtomicInteger();
        private final CountDownLatch writeEntered = new CountDownLatch(1);
        private final CountDownLatch releaseWrite = new CountDownLatch(1);
        private final CountDownLatch stopEntered = new CountDownLatch(1);
        private final CountDownLatch drainEntered = new CountDownLatch(1);
        private final CountDownLatch releaseDrain = new CountDownLatch(1);
        private volatile AudioFormat format;
        private volatile int requestedBufferBytes;
        private volatile int availableLimit = Integer.MAX_VALUE;
        private volatile int queuedBytes;
        private volatile long nativeFrames;
        private volatile long flushJumpFrames;
        private volatile boolean autoRender;
        private volatile boolean blockFirstWrite;
        private volatile boolean blockDrain;
        private volatile boolean running;
        private volatile boolean closed;
        private volatile boolean open;
        private volatile RuntimeException writeFailure;
        private volatile Thread lastWriter;

        private final SourceDataLine line = (SourceDataLine) Proxy.newProxyInstance(
                SourceDataLine.class.getClassLoader(), new Class<?>[]{SourceDataLine.class},
                (proxy, method, args) -> switch (method.getName()) {
                    case "open" -> {
                        format = (AudioFormat) args[0];
                        requestedBufferBytes = args.length > 1 ? (int) args[1] : 65_536;
                        open = true;
                        yield null;
                    }
                    case "getFormat" -> format;
                    case "getBufferSize" -> requestedBufferBytes;
                    case "available" -> Math.min(availableLimit, Math.max(0, requestedBufferBytes - queuedBytes));
                    case "write" -> write((byte[]) args[0], (int) args[1], (int) args[2]);
                    case "getLongFramePosition" -> nativeFrames;
                    case "getFramePosition" -> (int) nativeFrames;
                    case "getMicrosecondPosition" -> (long) (nativeFrames * 1_000_000 / format.getFrameRate());
                    case "isOpen" -> open;
                    case "isRunning", "isActive" -> running;
                    case "getLevel" -> 0.0f;
                    case "getControls" -> new javax.sound.sampled.Control[0];
                    case "isControlSupported" -> false;
                    case "start" -> { running = true; yield null; }
                    case "stop" -> {
                        running = false;
                        stopEntered.countDown();
                        releaseDrain.countDown();
                        yield null;
                    }
                    case "flush" -> { flush(); yield null; }
                    case "drain" -> { drain(); yield null; }
                    case "addLineListener", "removeLineListener" -> null;
                    case "close" -> {
                        closed = true;
                        open = false;
                        running = false;
                        releaseDrain.countDown();
                        yield null;
                    }
                    case "toString" -> "SimulatedSourceDataLine";
                    case "hashCode" -> System.identityHashCode(proxy);
                    case "equals" -> proxy == args[0];
                    default -> throw new UnsupportedOperationException(method.getName());
                });

        private int write(byte[] data, int offset, int length) throws InterruptedException {
            lastWriter = Thread.currentThread();
            int concurrency = activeWrites.incrementAndGet();
            maximumConcurrentWrites.accumulateAndGet(concurrency, Math::max);
            boolean firstWrite = writeEntered.getCount() != 0;
            writeEntered.countDown();
            try {
                if (firstWrite && blockFirstWrite) {
                    // Real native writes need not react to Java interruption. Native stop/flush
                    // and generation cancellation must still finish before a replacement writer.
                    long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
                    boolean released = false;
                    while (!released && System.nanoTime() < deadline) {
                        try {
                            released = releaseWrite.await(20, TimeUnit.MILLISECONDS);
                        } catch (InterruptedException ignored) {
                            // Simulate an uninterruptible but bounded native call.
                        }
                    }
                    if (!released) throw new IllegalStateException("test native write was never released");
                }
                if (writeFailure != null) throw writeFailure;
                synchronized (this) {
                    if (closed) throw new IllegalStateException("write after close");
                    if (length <= 0 || length % format.getFrameSize() != 0
                            || length > Math.min(availableLimit, requestedBufferBytes - queuedBytes)) {
                        throw new IllegalArgumentException("write exceeded frame-aligned device capacity");
                    }
                    captured.write(data, offset, length);
                    writes.add(length);
                    if (autoRender) nativeFrames += length / format.getFrameSize();
                    else queuedBytes += length;
                    notifyAll();
                }
                return length;
            } finally {
                activeWrites.decrementAndGet();
            }
        }

        private synchronized void renderFrames(int frames) {
            int bytes = frames * format.getFrameSize();
            if (bytes > queuedBytes) throw new IllegalArgumentException("not enough queued frames");
            queuedBytes -= bytes;
            nativeFrames += frames;
            notifyAll();
        }

        private synchronized void advanceNativeClock(long frames) {
            nativeFrames += frames;
        }

        private synchronized void flush() {
            queuedBytes = 0;
            nativeFrames += flushJumpFrames;
            releaseDrain.countDown();
            notifyAll();
        }

        private void drain() throws InterruptedException {
            drainEntered.countDown();
            if (blockDrain && !releaseDrain.await(2, TimeUnit.SECONDS)) {
                throw new IllegalStateException("test native drain was never stopped or flushed");
            }
        }

        private synchronized byte[] capturedBytes() {
            return captured.toByteArray();
        }

        private synchronized List<Integer> writeSizes() {
            return List.copyOf(writes);
        }

        private synchronized void awaitCaptured(int minimumBytes) throws InterruptedException {
            awaitState(() -> captured.size() >= minimumBytes, "PCM writer did not produce expected bytes");
        }

        private synchronized void awaitQueued(int minimumBytes) throws InterruptedException {
            awaitState(() -> queuedBytes >= minimumBytes, "PCM writer did not fill the expected queue");
        }

        private void awaitState(BooleanSupplier ready, String failure) throws InterruptedException {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
            while (!ready.getAsBoolean()) {
                long remaining = deadline - System.nanoTime();
                assertTrue(remaining > 0, failure);
                TimeUnit.NANOSECONDS.timedWait(this, remaining);
            }
        }
    }
}
