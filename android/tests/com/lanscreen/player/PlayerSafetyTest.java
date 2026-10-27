package com.lanscreen.player;

public final class PlayerSafetyTest {
    private static int checks;
    static void check(boolean result) { checks++; if (!result) throw new AssertionError("check " + checks); }
    interface Probe { void run() throws Exception; }
    static void rejects(Probe probe) throws Exception {
        checks++;
        try { probe.run(); throw new AssertionError("expected rejection " + checks); }
        catch (java.io.IOException | IllegalArgumentException expected) { }
    }
    public static void main(String[] args) throws Exception {
        final long M = 1024L * 1024;
        CacheStore.requireTransitionCapacity(800*M, 0, 512*M, 2*1024*M, 256*M, false); checks++;
        CacheStore.requireTransitionCapacity(800*M, 0, 2*1024*M, 100*M, 256*M, false); checks++;
        CacheStore.requireTransitionCapacity(800*M, 0, 512*M, 0, 256*M, false); checks++;
        rejects(() -> CacheStore.requireTransitionCapacity(800*M, 0, 512*M, 2*1024*M, 256*M, true));
        rejects(() -> CacheStore.requireTransitionCapacity(800*M, 0, 2*1024*M, 100*M, 256*M, true));
        rejects(() -> CacheStore.requireTransitionCapacity(0, 1, M, M, 0, false));
        CacheStore.requireTransitionCapacity(800*M, 100*M, 2*1024*M, 2*1024*M, 256*M, true); checks++;
        CacheStore.requireCleanupCapacity(0, 512*M); checks++;
        CacheStore.requireCleanupCapacity(500*M, 512*M); checks++;
        rejects(() -> CacheStore.requireCleanupCapacity(800*M, 512*M));
        check(PlaybackSafety.imageSample(3840,2160,3840,2160,12*M) == 2);
        check(PlaybackSafety.imageSample(1920,1080,1920,1080,12*M) == 1);
        check(PlaybackSafety.imageSample(3840,2160,1920,1080,2*M) == 4);
        check(PlaybackSafety.imageSample(1,2160,1920,1080,2*M) == 2);
        check(PlaybackSafety.imageSample(Integer.MAX_VALUE,1,1920,1080,2*M) > 1);
        rejects(() -> PlaybackSafety.imageSample(0,2160,1920,1080,2*M));
        rejects(() -> PlaybackSafety.imageSample(Integer.MAX_VALUE,Integer.MAX_VALUE,1,1,4));
        java.util.List<Integer> samples = new java.util.ArrayList<>();
        int decodedSample = PlaybackSafety.decodeWithRetries(2, sample -> {
            samples.add(sample); if (sample < 8) throw new OutOfMemoryError("injected"); return sample;
        });
        check(decodedSample == 8); check(samples.equals(java.util.Arrays.asList(2,4,8)));
        samples.clear();
        try { PlaybackSafety.decodeWithRetries(1, sample -> { samples.add(sample); throw new OutOfMemoryError("injected"); }); throw new AssertionError("unbounded OOM recovery"); }
        catch (OutOfMemoryError expected) { check(samples.equals(java.util.Arrays.asList(1,2,4,8))); }
        samples.clear();
        rejects(() -> PlaybackSafety.decodeWithRetries(1, sample -> { samples.add(sample); throw new java.io.IOException("corrupt image"); }));
        check(samples.size() == 1);
        PlaybackSafety.StallTracker tracker = new PlaybackSafety.StallTracker();
        tracker.start(0,0); check(!tracker.stalled(29999,0)); check(tracker.stalled(30000,0));
        tracker.start(0,0);
        for (int n=1;n<=100;n++) check(!tracker.stalled(n*5000L,n*5000));
        tracker.start(0,0); check(!tracker.stalled(25000,500));
        check(!tracker.stalled(54999,500)); check(tracker.stalled(55000,500));
        tracker.start(100000,0); check(!tracker.stalled(120000,0)); // A resumed/rebuilt player resets the clock.
        PlaybackSafety.Failures failures = new PlaybackSafety.Failures();
        failures.failed("codec",0); check(failures.retryAt("codec") == 30000);
        failures.failed("codec",30000); check(failures.retryAt("codec") == 90000);
        for(int n=0;n<10;n++) failures.failed("codec",90000);
        check(failures.retryAt("codec") == 390000);
        failures.succeeded("codec"); check(failures.retryAt("codec") == 0);
        check(failures.requestIntegrity(0)); check(!failures.requestIntegrity(1));
        check(!failures.requestIntegrity(599999)); check(failures.requestIntegrity(600000));
        for(int n=0;n<1000;n++) failures.failed("asset-"+n,0);
        check(failures.size() == 256); check(failures.retryAt("asset-0") == 0);
        failures.clear(); check(failures.size()==0); check(failures.requestIntegrity(600001));
        System.out.println("RESULT " + checks + " assertions passed: transition quota, cleanup, image budgets, stall clocks, failure/integrity backoff.");
        System.out.println("Pure functions only; no files deleted and no network or server started.");
    }
}
