package com.lanscreen.player;

import java.util.LinkedHashMap;

/** Platform-independent limits and clocks used by the actual player. */
final class PlaybackSafety {
    private PlaybackSafety() { }
    static long combinedImageBudget(long maxHeapBytes) {
        return 2 * singleImageBudget(maxHeapBytes);
    }
    // Preserve the established image quality policy; retaining a previous image never changes this limit.
    static long singleImageBudget(long maxHeapBytes) { return Math.max(2L * 1024 * 1024, Math.min(12L * 1024 * 1024, maxHeapBytes / 8)); }
    static boolean imageFitsBudget(long allocationBytes, long budgetBytes) {
        return allocationBytes > 0 && allocationBytes <= budgetBytes;
    }
    static int imageSample(int width, int height, int maxWidth, int maxHeight, long budgetBytes) {
        if (width <= 0 || height <= 0 || maxWidth <= 0 || maxHeight <= 0 || budgetBytes < 4) throw new IllegalArgumentException("invalid image dimensions");
        int sample = 1;
        while (true) {
            long w = Math.max(1L, ((long)width + sample - 1) / sample);
            long h = Math.max(1L, ((long)height + sample - 1) / sample);
            if (w <= maxWidth && h <= maxHeight && w <= budgetBytes / 4 / h) return sample;
            if (sample >= (1 << 30)) throw new IllegalArgumentException("image exceeds sampling range");
            sample *= 2;
        }
    }
    interface ImageAttempt<T> { T decode(int sample) throws Exception; }
    static final class NeedsSingleImageRetry extends Exception {
        NeedsSingleImageRetry() { super("retry image after releasing the previous frame"); }
    }
    static <T> T decodeForHandoff(int sample, boolean retainPrevious, ImageAttempt<T> attempt) throws Exception {
        if (!retainPrevious) return decodeWithRetries(sample, attempt);
        try { return attempt.decode(sample); }
        catch (OutOfMemoryError exhausted) { throw new NeedsSingleImageRetry(); }
    }
    static <T> T decodeWithRetries(int sample, ImageAttempt<T> attempt) throws Exception {
        for (int n = 0; n < 4; n++) {
            try { return attempt.decode(sample); }
            catch (OutOfMemoryError exhausted) {
                if (n == 3 || sample >= (1 << 29)) throw exhausted;
                sample *= 2;
            }
        }
        throw new IllegalStateException("decode attempts exhausted");
    }
    static final class StallTracker {
        private long changedAt;
        private int position;
        void start(long now, int current) { changedAt = now; position = current; }
        boolean stalled(long now, int current) {
            if (current < position || (long)current - position >= 250) { changedAt = now; position = current; }
            return now - changedAt >= 30000L;
        }
    }
    static final class Failures {
        private static final int MAX_ENTRIES = 256;
        private static final long INTEGRITY_INTERVAL = 10 * 60 * 1000L;
        private static final class Entry { int count; long retryAt; }
        private final LinkedHashMap<String, Entry> entries = new LinkedHashMap<>();
        private long nextIntegrity;
        void clear() { entries.clear(); nextIntegrity = 0; }
        void failed(String key, long now) {
            Entry entry = entries.remove(key);
            if (entry == null) entry = new Entry();
            entry.count = Math.min(5, entry.count + 1);
            entry.retryAt = now + Math.min(300000L, 30000L << (entry.count - 1));
            entries.put(key, entry);
            while (entries.size() > MAX_ENTRIES) entries.remove(entries.keySet().iterator().next());
        }
        void succeeded(String key) { entries.remove(key); }
        long retryAt(String key) { Entry entry = entries.get(key); return entry == null ? 0 : entry.retryAt; }
        boolean requestIntegrity(long now) {
            if (now < nextIntegrity) return false;
            nextIntegrity = now + INTEGRITY_INTERVAL; return true;
        }
        int size() { return entries.size(); }
    }
}
