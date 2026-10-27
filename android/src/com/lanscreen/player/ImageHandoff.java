package com.lanscreen.player;

/** UI-thread owner of the displayed image. Decoder results transfer ownership only on acceptance. */
final class ImageHandoff<T> {
    interface Display<T> { void show(T value); }
    interface Disposal<T> { void dispose(T value); }
    private final Display<T> display;
    private final Disposal<T> disposal;
    private T current;
    private String contentKey = "";
    private long generation;
    ImageHandoff(Display<T> display, Disposal<T> disposal) { this.display = display; this.disposal = disposal; }
    long begin(boolean retainCurrent) {
        generation++;
        if (!retainCurrent) clearCurrent();
        return generation;
    }
    long ticket() { return generation; }
    boolean hasImage() { return current != null; }
    boolean hasContent(String key) { return current != null && !contentKey.isEmpty() && contentKey.equals(key); }
    boolean needsLoadingHint(long ticket) { return ticket == generation && current == null; }
    boolean present(long ticket, T next, String key) {
        if (next == null) throw new IllegalArgumentException("missing decoded image");
        if (ticket != generation) { if (next != current) disposal.dispose(next); return false; }
        T previous = current;
        try { display.show(next); }
        catch (RuntimeException | Error failure) {
            current = null; contentKey = ""; generation++;
            try { display.show(null); } catch (RuntimeException | Error ignored) { }
            if (previous != null && previous != next) disposal.dispose(previous);
            disposal.dispose(next);
            throw failure;
        }
        current = next; contentKey = key == null ? "" : key;
        if (previous != null && previous != next) disposal.dispose(previous);
        return true;
    }
    void clear() { begin(false); }
    private void clearCurrent() {
        T previous = current; current = null; contentKey = "";
        try { display.show(null); }
        finally { if (previous != null) disposal.dispose(previous); }
    }
}
