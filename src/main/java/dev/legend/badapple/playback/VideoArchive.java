package dev.legend.badapple.playback;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/** A bounded, random-access reader for the portable .bapple container. */
public final class VideoArchive implements Closeable {
    public static final int MAX_MANIFEST_BYTES = 8 * 1024 * 1024;
    public static final int MAX_FRAME_BYTES = 64 * 1024 * 1024;
    public static final int MAX_AUDIO_BYTES = 256 * 1024 * 1024;
    public static final int MAX_FRAMES = 500_000;
    public static final int MAX_DIMENSION = 4096;
    public static final long MAX_DURATION_MICROS = 6L * 60 * 60 * 1_000_000;
    private static final byte[] PNG_SIGNATURE = {(byte) 137, 80, 78, 71, 13, 10, 26, 10};

    private final ZipFile zip;
    private final Metadata metadata;

    private VideoArchive(ZipFile zip, Metadata metadata) {
        this.zip = zip;
        this.metadata = metadata;
    }

    public static VideoArchive open(Path path) throws IOException {
        ZipFile zip = new ZipFile(path.toFile());
        try {
            Set<String> names = new HashSet<>();
            var entries = zip.entries();
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                if (!names.add(entry.getName())) {
                    throw new IOException("Archive contains duplicate entry: " + entry.getName());
                }
                if (names.size() > MAX_FRAMES + 32) {
                    throw new IOException("Archive has too many entries");
                }
            }
            ZipEntry manifest = requireEntry(zip, "manifest.json", MAX_MANIFEST_BYTES);
            Metadata metadata = parseMetadata(readEntry(zip, manifest, MAX_MANIFEST_BYTES));
            for (int i = 0; i < metadata.frameCount(); i++) {
                requireEntry(zip, frameName(i), MAX_FRAME_BYTES);
            }
            if (metadata.audio() != null) {
                requireEntry(zip, metadata.audio(), MAX_AUDIO_BYTES);
            }
            return new VideoArchive(zip, metadata);
        } catch (IOException | RuntimeException failure) {
            try {
                zip.close();
            } catch (IOException closeFailure) {
                failure.addSuppressed(closeFailure);
            }
            if (failure instanceof IOException io) throw io;
            throw new IOException("Invalid .bapple archive: " + failure.getMessage(), failure);
        }
    }

    public Metadata metadata() {
        return metadata;
    }

    public byte[] readFrame(int index) throws IOException {
        if (index < 0 || index >= metadata.frameCount()) {
            throw new IndexOutOfBoundsException("Frame index: " + index);
        }
        byte[] bytes = readEntry(zip, requireEntry(zip, frameName(index), MAX_FRAME_BYTES), MAX_FRAME_BYTES);
        // Check IHDR before a decoder can allocate memory from untrusted dimensions.
        if (bytes.length < 33 || !Arrays.equals(PNG_SIGNATURE, Arrays.copyOf(bytes, 8))
                || ByteBuffer.wrap(bytes, 8, 4).getInt() != 13
                || bytes[12] != 'I' || bytes[13] != 'H' || bytes[14] != 'D' || bytes[15] != 'R') {
            throw new IOException("Frame " + index + " is not a valid PNG");
        }
        int width = ByteBuffer.wrap(bytes, 16, 4).getInt();
        int height = ByteBuffer.wrap(bytes, 20, 4).getInt();
        if (width != metadata.width() || height != metadata.height()) {
            throw new IOException("Frame " + index + " dimensions do not match the manifest");
        }
        return bytes;
    }

    /** Returns a bounded, CRC-checked stream; null denotes an intentionally silent video. */
    public InputStream openAudioStream() throws IOException {
        if (metadata.audio() == null) return null;
        return new ByteArrayInputStream(readEntry(zip,
                requireEntry(zip, metadata.audio(), MAX_AUDIO_BYTES), MAX_AUDIO_BYTES));
    }

    @Override
    public void close() throws IOException {
        zip.close();
    }

    private static String frameName(int index) {
        return String.format(Locale.ROOT, "frames/%06d.png", index);
    }

    private static ZipEntry requireEntry(ZipFile zip, String name, long limit) throws IOException {
        ZipEntry entry = zip.getEntry(name);
        if (entry == null || entry.isDirectory()) throw new IOException("Missing archive entry: " + name);
        if (entry.getSize() < 0 || entry.getSize() > limit) {
            throw new IOException("Archive entry exceeds the size limit: " + name);
        }
        return entry;
    }

    private static byte[] readEntry(ZipFile zip, ZipEntry entry, int limit) throws IOException {
        try (InputStream stream = zip.getInputStream(entry);
             ByteArrayOutputStream bytes = new ByteArrayOutputStream((int) Math.min(entry.getSize(), 65536))) {
            byte[] buffer = new byte[16384];
            CRC32 crc = new CRC32();
            int count;
            long total = 0;
            while ((count = stream.read(buffer)) != -1) {
                total += count;
                if (total > limit || total > entry.getSize()) {
                    throw new IOException("Archive entry exceeds declared size: " + entry.getName());
                }
                crc.update(buffer, 0, count);
                bytes.write(buffer, 0, count);
            }
            if (total != entry.getSize() || crc.getValue() != entry.getCrc()) {
                throw new IOException("Archive entry failed integrity check: " + entry.getName());
            }
            return bytes.toByteArray();
        }
    }

    private static Metadata parseMetadata(byte[] bytes) throws IOException {
        try {
            JsonElement parsed = JsonParser.parseString(new String(bytes, StandardCharsets.UTF_8));
            if (!parsed.isJsonObject()) throw new IOException("Manifest must be an object");
            JsonObject object = parsed.getAsJsonObject();
            if (integer(object, "formatVersion") != 1) throw new IOException("Unsupported .bapple formatVersion");
            long width = integer(object, "width");
            long height = integer(object, "height");
            long frameCount = integer(object, "frameCount");
            long duration = integer(object, "durationMicros");
            if (width < 1 || height < 1 || width > MAX_DIMENSION || height > MAX_DIMENSION) {
                throw new IOException("Video dimensions must be between 1 and " + MAX_DIMENSION);
            }
            if (frameCount < 1 || frameCount > MAX_FRAMES) throw new IOException("Invalid frameCount");
            if (duration < 1 || duration > MAX_DURATION_MICROS) throw new IOException("Invalid durationMicros");
            JsonElement timestamps = object.get("frameTimestampsMicros");
            if (timestamps == null || !timestamps.isJsonArray() || timestamps.getAsJsonArray().size() != frameCount) {
                throw new IOException("frameTimestampsMicros must contain exactly frameCount timestamps");
            }
            long[] times = new long[(int) frameCount];
            for (int i = 0; i < times.length; i++) {
                times[i] = exactInteger(timestamps.getAsJsonArray().get(i));
                if ((i == 0 && times[i] != 0) || (i > 0 && times[i] <= times[i - 1])
                        || times[i] < 0 || times[i] >= duration) {
                    throw new IOException("Frame timestamps must start at zero, strictly increase, and precede duration");
                }
            }
            String audio = optionalString(object, "audio");
            if (audio != null && !audio.equals("audio.wav")) throw new IOException("Audio must be audio.wav or null");
            return new Metadata((int) width, (int) height, (int) frameCount, duration, times,
                    audio, optionalString(object, "source"), optionalString(object, "sourceUrl"));
        } catch (RuntimeException error) {
            throw new IOException("Invalid manifest: " + error.getMessage(), error);
        }
    }

    private static long integer(JsonObject object, String name) throws IOException {
        JsonElement value = object.get(name);
        if (value == null) throw new IOException("Manifest is missing " + name);
        return exactInteger(value);
    }

    private static long exactInteger(JsonElement value) throws IOException {
        if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber()) {
            throw new IOException("Manifest integer is not a number");
        }
        try {
            return value.getAsBigDecimal().longValueExact();
        } catch (ArithmeticException | NumberFormatException invalid) {
            throw new IOException("Manifest contains an invalid integer", invalid);
        }
    }

    private static String optionalString(JsonObject object, String name) throws IOException {
        JsonElement value = object.get(name);
        if (value == null || value.isJsonNull()) return null;
        if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString()) {
            throw new IOException("Manifest " + name + " must be a string or null");
        }
        String result = value.getAsString();
        if (result.length() > 4096) throw new IOException("Manifest " + name + " is too long");
        return result;
    }

    public record Metadata(int width, int height, int frameCount, long durationMicros,
                           long[] frameTimestampsMicros, String audio, String source, String sourceUrl) {
        public Metadata {
            frameTimestampsMicros = frameTimestampsMicros.clone();
        }

        @Override
        public long[] frameTimestampsMicros() {
            return frameTimestampsMicros.clone();
        }
    }
}
