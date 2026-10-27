package com.lanscreen.player;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

public final class ImageHandoffTest {
    private static int checks;
    private static void check(boolean value) { checks++; if (!value) throw new AssertionError("image check " + checks); }
    private static final class Frame {
        final String name; int disposals;
        Frame(String name) { this.name=name; }
    }
    private static final class Surface {
        Frame shown, fail, oom;
        final List<String> events = new ArrayList<>();
        void show(Frame frame) {
            if (frame != null && frame == fail) throw new IllegalStateException("injected view failure");
            if (frame != null && frame == oom) throw new OutOfMemoryError("injected view OOM");
            if (frame != null && frame.disposals != 0) throw new AssertionError("displaying recycled frame");
            shown = frame; events.add("show:" + (frame == null ? "none" : frame.name));
        }
        void dispose(Frame frame) {
            if (++frame.disposals != 1) throw new AssertionError("double dispose: " + frame.name);
            events.add("dispose:" + frame.name);
        }
    }
    private static Runnable take(BlockingQueue<Runnable> queue) throws Exception {
        Runnable next=queue.poll(3,TimeUnit.SECONDS); if(next==null)throw new AssertionError("callback timeout"); return next;
    }
    public static void main(String[] args) throws Exception {
        Surface view=new Surface(); ImageHandoff<Frame> frames=new ImageHandoff<>(view::show,view::dispose);
        long first=frames.begin(false); check(!frames.hasImage()); check(frames.needsLoadingHint(first));
        Frame a=new Frame("a"); check(frames.present(first,a,"a-hash")); check(view.shown==a); check(!frames.needsLoadingHint(first));
        check(frames.hasContent("a-hash")); check(!frames.hasContent("a-replaced-hash")); check(!frames.hasContent(""));
        long next=frames.begin(true); check(view.shown==a && a.disposals==0); check(!frames.needsLoadingHint(next));
        Frame b=new Frame("b"); view.events.clear(); check(frames.present(next,b,"b-hash"));
        check(view.events.equals(java.util.Arrays.asList("show:b","dispose:a")));
        check(a.disposals==1 && b.disposals==0 && view.shown==b);
        long outdated=frames.begin(true), latest=frames.begin(true);
        Frame stale=new Frame("stale"); check(!frames.present(outdated,stale,"stale-hash"));
        check(stale.disposals==1 && view.shown==b && b.disposals==0);
        check(frames.hasContent("b-hash") && !frames.hasContent("stale-hash"));
        check(!frames.needsLoadingHint(outdated)); check(!frames.present(outdated,b,"b-hash")); check(b.disposals==0);
        check(frames.present(latest,b,"b-hash")); check(b.disposals==0); // Same already-owned image is not recycled twice.
        long cancelled=frames.begin(true); frames.clear();
        check(view.shown==null && b.disposals==1 && !frames.hasImage());
        check(!frames.hasContent("b-hash"));
        Frame late=new Frame("late"); check(!frames.present(cancelled,late,"late-hash")); check(late.disposals==1 && view.shown==null);
        check(!frames.needsLoadingHint(cancelled));
        long noImage=frames.begin(false); check(frames.needsLoadingHint(noImage));
        Frame c=new Frame("c"); frames.present(noImage,c,"c-hash");
        Frame broken=new Frame("broken"); view.fail=broken;
        try { frames.present(frames.begin(true),broken,"broken-hash"); throw new AssertionError("expected view failure"); }
        catch(IllegalStateException expected) { check(c.disposals==1 && broken.disposals==1 && !frames.hasImage() && view.shown==null); }
        view.fail=null;
        Frame renderOld=new Frame("render-old"), renderNew=new Frame("render-new");
        frames.present(frames.begin(false),renderOld,"render-old-hash"); view.oom=renderNew;
        try { frames.present(frames.begin(true),renderNew,"render-new-hash"); throw new AssertionError("expected renderer OOM"); }
        catch(OutOfMemoryError expected) {
            check(renderOld.disposals==1 && renderNew.disposals==1); check(!frames.hasImage() && view.shown==null);
            check(!frames.hasContent("render-old-hash") && !frames.hasContent("render-new-hash"));
        }
        view.oom=null;

        // Actual LatestDecoder + handoff integration: retain old, then replace only on UI delivery.
        BlockingQueue<Runnable> ui=new LinkedBlockingQueue<>();
        LatestDecoder<Frame> decoder=new LatestDecoder<>(ui::add,view::dispose);
        try {
            Frame old=new Frame("old"), decoded=new Frame("decoded"); frames.present(frames.begin(false),old,"old-hash");
            long ticket=frames.begin(true);
            AtomicInteger displays=new AtomicInteger();
            decoder.submit(()->decoded,new LatestDecoder.Receiver<Frame>() {
                public void ready(Frame frame) { if(frames.present(ticket,frame,frame.name+"-hash")) displays.incrementAndGet(); }
                public void failed(Throwable error) { throw new AssertionError(error); }
            });
            Runnable ready=take(ui); check(view.shown==old && old.disposals==0 && decoded.disposals==0 && displays.get()==0);
            ready.run(); check(view.shown==decoded && old.disposals==1 && decoded.disposals==0 && displays.get()==1);
            Frame queued=new Frame("queued"); long queuedTicket=frames.begin(true);
            decoder.submit(()->queued,new LatestDecoder.Receiver<Frame>() {
                public void ready(Frame frame) { frames.present(queuedTicket,frame,frame.name+"-hash"); }
                public void failed(Throwable error) { throw new AssertionError(error); }
            });
            Runnable cancelledDelivery=take(ui);
            frames.clear(); decoder.cancel(); cancelledDelivery.run();
            check(view.shown==null && decoded.disposals==1 && queued.disposals==1);
        } finally { decoder.close(); check(decoder.awaitTermination(3000)); frames.clear(); }

        // Pause/unbind while a native-like decoder ignores interruption: no late display or leaked result.
        CountDownLatch started=new CountDownLatch(1), release=new CountDownLatch(1);
        LatestDecoder<Frame> running=new LatestDecoder<>(ui::add,view::dispose);
        Frame held=new Frame("held"), background=new Frame("background");
        frames.present(frames.begin(false),held,"held-hash"); long runningTicket=frames.begin(true);
        try {
            running.submit(()->{
                started.countDown(); boolean done=false;
                while(!done)try{release.await();done=true;}catch(InterruptedException ignored){}
                return background;
            },new LatestDecoder.Receiver<Frame>() {
                public void ready(Frame frame) { frames.present(runningTicket,frame,frame.name+"-hash"); }
                public void failed(Throwable error) { throw new AssertionError(error); }
            });
            check(started.await(3,TimeUnit.SECONDS));
            frames.clear(); running.cancel(); check(held.disposals==1 && view.shown==null);
        } finally { release.countDown(); running.close(); check(running.awaitTermination(3000)); }
        check(background.disposals==1 && view.shown==null); check(ui.isEmpty());

        // The actual decode policy never lowers resolution while retaining a previous frame.
        List<Integer> samples=new ArrayList<>(); Frame qualityOld=new Frame("quality-old"), qualityNew=new Frame("quality-new");
        frames.present(frames.begin(false),qualityOld,"quality-old-hash"); long qualityTicket=frames.begin(true);
        try {
            PlaybackSafety.decodeForHandoff(1,frames.hasImage(),sample->{samples.add(sample);throw new OutOfMemoryError("two frames do not fit");});
            throw new AssertionError("expected release-before-retry");
        } catch(PlaybackSafety.NeedsSingleImageRetry expected) {
            check(samples.equals(java.util.Arrays.asList(1))); check(view.shown==qualityOld && qualityOld.disposals==0);
        }
        frames.clear(); check(qualityOld.disposals==1 && !frames.hasImage());
        Frame qualityResult=PlaybackSafety.decodeForHandoff(1,frames.hasImage(),sample->{samples.add(sample);check(qualityOld.disposals==1);return qualityNew;});
        check(samples.equals(java.util.Arrays.asList(1,1))); // Same initial sampling after releasing the old page.
        check(!frames.needsLoadingHint(qualityTicket));
        check(frames.present(frames.begin(false),qualityResult,"quality-new-hash")); frames.clear(); check(qualityNew.disposals==1);
        samples.clear();
        try {
            PlaybackSafety.decodeForHandoff(1,frames.hasImage(),sample->{samples.add(sample);throw new OutOfMemoryError("single image also fails");});
            throw new AssertionError("expected original bounded OOM handling");
        } catch(OutOfMemoryError expected) { check(samples.equals(java.util.Arrays.asList(1,2,4,8))); }

        // A fallback requested by an old decoder cannot retry after pause/empty/unbind cancellation.
        LatestDecoder<Frame> fallbackDecoder=new LatestDecoder<>(ui::add,view::dispose);
        AtomicInteger fallbackRequests=new AtomicInteger(); Frame fallbackOld=new Frame("fallback-old");
        frames.present(frames.begin(false),fallbackOld,"fallback-old-hash");
        try {
            fallbackDecoder.submit(()->PlaybackSafety.decodeForHandoff(1,true,sample->{throw new OutOfMemoryError("injected retained decode failure");}),
                new LatestDecoder.Receiver<Frame>() {
                    public void ready(Frame frame) { throw new AssertionError("unexpected image"); }
                    public void failed(Throwable error) {
                        if(error instanceof PlaybackSafety.NeedsSingleImageRetry)fallbackRequests.incrementAndGet();
                    }
                });
            Runnable failureDelivery=take(ui); frames.clear(); fallbackDecoder.cancel(); failureDelivery.run();
            check(fallbackRequests.get()==0 && fallbackOld.disposals==1 && view.shown==null);
        } finally { fallbackDecoder.close(); check(fallbackDecoder.awaitTermination(3000)); }

        long M=1024L*1024;
        for(long heap:new long[]{48,1024,16*M,32*M,64*M,128*M,256*M,1024*M,Long.MAX_VALUE}) {
            long combined=PlaybackSafety.combinedImageBudget(heap), single=PlaybackSafety.singleImageBudget(heap);
            check(single==Math.max(2*M,Math.min(12*M,heap/8))); check(single*2==combined); check(combined<=24*M);
            check(PlaybackSafety.imageFitsBudget(single,single)); check(!PlaybackSafety.imageFitsBudget(single+1,single));
            check(!PlaybackSafety.imageFitsBudget(0,single));
        }
        check(PlaybackSafety.singleImageBudget(64*M)==8*M); // Original 1080p quality preserved.
        check(PlaybackSafety.imageSample(1920,1080,1920,1080,PlaybackSafety.singleImageBudget(64*M))==1);
        check(PlaybackSafety.singleImageBudget(1024)==2*M); // Keep the preexisting minimum rather than silently changing quality.
        check(PlaybackSafety.imageSample(3840,2160,3840,2160,PlaybackSafety.singleImageBudget(256*M))==2);
        System.out.println("RESULT " + checks + " image handoff assertions passed: replace-before-recycle, retained display, stale/cancel/close disposal, loading fallback, two-image budgets.");
        System.out.println("Controlled decoder workers awaitTermination passed; no Android view/native decoder run, no network/listeners or file deletion.");
    }
}
