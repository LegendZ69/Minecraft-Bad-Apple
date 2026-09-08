package dev.legend.badapple.playback;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

import static org.junit.jupiter.api.Assertions.*;

class VideoArchiveTest {
    @TempDir Path directory;

    @Test
    void readsExactFrameBytesAndProtectsMetadataFromMutation() throws Exception {
        byte[] png = png(1, 1);
        Path path = archive(manifest(), Map.of("frames/000000.png", png, "frames/000001.png", png));
        try (VideoArchive archive = VideoArchive.open(path)) {
            assertEquals(2, archive.metadata().frameCount());
            assertArrayEquals(png, archive.readFrame(1));
            assertNull(archive.openAudioStream());
            long[] times = archive.metadata().frameTimestampsMicros();
            times[1] = 999;
            assertArrayEquals(new long[]{0, 500_000}, archive.metadata().frameTimestampsMicros());
            assertThrows(IndexOutOfBoundsException.class, () -> archive.readFrame(2));
        }
    }

    @Test
    void rejectsMissingFramesAndUnsupportedVersion() throws Exception {
        assertThrows(IOException.class, () -> VideoArchive.open(archive(manifest(), Map.of())));
        JsonObject version = manifest();
        version.addProperty("formatVersion", 2);
        assertThrows(IOException.class, () -> VideoArchive.open(archive(version, frames())));
    }

    @Test
    void rejectsDuplicateDescendingAndOutOfRangeTimestamps() throws Exception {
        for (long[] times : new long[][]{{0, 0}, {1, 500_000}, {0, -1}, {0, 1_000_000}}) {
            JsonObject object = manifest();
            JsonArray values = new JsonArray();
            for (long time : times) values.add(time);
            object.add("frameTimestampsMicros", values);
            assertThrows(IOException.class, () -> VideoArchive.open(archive(object, frames())));
        }
        JsonObject missing = manifest();
        missing.add("frameTimestampsMicros", new JsonArray());
        assertThrows(IOException.class, () -> VideoArchive.open(archive(missing, frames())));
    }

    @Test
    void rejectsUnboundedDimensionsFrameCountsAndNonIntegerFields() throws Exception {
        for (String field : new String[]{"width", "height", "frameCount", "durationMicros"}) {
            JsonObject object = manifest();
            object.addProperty(field, Long.MAX_VALUE);
            assertThrows(IOException.class, () -> VideoArchive.open(archive(object, frames())));
        }
        JsonObject fractional = manifest();
        fractional.addProperty("frameCount", 2.5);
        assertThrows(IOException.class, () -> VideoArchive.open(archive(fractional, frames())));
        JsonObject stringInteger = manifest();
        stringInteger.addProperty("width", "1");
        assertThrows(IOException.class, () -> VideoArchive.open(archive(stringInteger, frames())));
    }

    @Test
    void rejectsAudioPathsOutsideTheDefinedContainerFormat() throws Exception {
        JsonObject object = manifest();
        object.addProperty("audio", "../audio.wav");
        assertThrows(IOException.class, () -> VideoArchive.open(archive(object, frames())));
    }

    @Test
    void rejectsPngDimensionMismatchBeforeImageDecoding() throws Exception {
        Path path = archive(manifest(), Map.of("frames/000000.png", png(2, 1), "frames/000001.png", png(1, 1)));
        try (VideoArchive archive = VideoArchive.open(path)) {
            assertThrows(IOException.class, () -> archive.readFrame(0));
        }
    }

    @Test
    void checksStoredEntryCrcInsteadOfTrustingZipMetadata() throws Exception {
        byte[] png = png(1, 1);
        Path path = archive(manifest(), Map.of("frames/000000.png", png, "frames/000001.png", png));
        byte[] contents = Files.readAllBytes(path);
        int imageOffset = indexOf(contents, png);
        assertTrue(imageOffset >= 0);
        contents[imageOffset + png.length - 1] ^= 1;
        Files.write(path, contents);
        try (VideoArchive archive = VideoArchive.open(path)) {
            // The modified frame depends on entry order; every frame must still be checked.
            assertThrows(IOException.class, () -> {
                archive.readFrame(0);
                archive.readFrame(1);
            });
        }
    }

    @Test
    void rejectsOversizedDeclaredEntriesWithoutAllocatingTheirContents() throws Exception {
        Path path = archive(manifest(), frames());
        byte[] contents = Files.readAllBytes(path);
        byte[] name = "frames/000000.png".getBytes(StandardCharsets.UTF_8);
        for (int i = 0; i < contents.length - 46; i++) {
            if (contents[i] == 'P' && contents[i + 1] == 'K' && contents[i + 2] == 1 && contents[i + 3] == 2
                    && matchesAt(contents, i + 46, name)) {
                int size = VideoArchive.MAX_FRAME_BYTES + 1;
                for (int n = 0; n < 4; n++) contents[i + 24 + n] = (byte) (size >>> (8 * n));
                Files.write(path, contents);
                assertThrows(IOException.class, () -> VideoArchive.open(path));
                return;
            }
        }
        fail("Expected central directory frame entry");
    }

    static JsonObject manifest() {
        JsonObject object = new JsonObject();
        object.addProperty("formatVersion", 1);
        object.addProperty("width", 1);
        object.addProperty("height", 1);
        object.addProperty("frameCount", 2);
        object.addProperty("durationMicros", 1_000_000);
        JsonArray timestamps = new JsonArray();
        timestamps.add(0);
        timestamps.add(500_000);
        object.add("frameTimestampsMicros", timestamps);
        object.add("audio", JsonNull.INSTANCE);
        return object;
    }

    private Map<String, byte[]> frames() throws IOException {
        return Map.of("frames/000000.png", png(1, 1), "frames/000001.png", png(1, 1));
    }

    Path archive(JsonObject manifest, Map<String, byte[]> entries) throws IOException {
        Path path = Files.createTempFile(directory, "video-", ".bapple");
        Map<String, byte[]> all = new LinkedHashMap<>();
        all.put("manifest.json", manifest.toString().getBytes(StandardCharsets.UTF_8));
        all.putAll(entries);
        try (ZipOutputStream zip = new ZipOutputStream(Files.newOutputStream(path))) {
            for (var pair : all.entrySet()) {
                byte[] bytes = pair.getValue();
                CRC32 crc = new CRC32();
                crc.update(bytes);
                ZipEntry entry = new ZipEntry(pair.getKey());
                entry.setMethod(ZipEntry.STORED);
                entry.setSize(bytes.length);
                entry.setCompressedSize(bytes.length);
                entry.setCrc(crc.getValue());
                zip.putNextEntry(entry);
                zip.write(bytes);
                zip.closeEntry();
            }
        }
        return path;
    }

    static byte[] png(int width, int height) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        ImageIO.write(new BufferedImage(width, height, BufferedImage.TYPE_INT_RGB), "png", output);
        return output.toByteArray();
    }

    private static int indexOf(byte[] haystack, byte[] needle) {
        for (int i = 0; i <= haystack.length - needle.length; i++) {
            if (matchesAt(haystack, i, needle)) return i;
        }
        return -1;
    }

    private static boolean matchesAt(byte[] haystack, int offset, byte[] needle) {
        if (offset + needle.length > haystack.length) return false;
        for (int j = 0; j < needle.length; j++) if (haystack[offset + j] != needle[j]) return false;
        return true;
    }
}
