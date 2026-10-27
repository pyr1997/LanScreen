package com.lanscreen.player;

import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

public final class LatestDecoderTest {
    static int checks;
    static void check(boolean value) { checks++; if (!value) throw new AssertionError("check " + checks); }
    static Runnable delivery(BlockingQueue<Runnable> ui) throws Exception {
        Runnable task=ui.poll(3,TimeUnit.SECONDS); if(task==null)throw new AssertionError("delivery timeout"); return task;
    }
    static void finish(LatestDecoder<?> worker) throws Exception { worker.close(); check(worker.awaitTermination(3000)); }
    static LatestDecoder.Receiver<Integer> receiver(AtomicInteger delivered, AtomicInteger errors) {
        return new LatestDecoder.Receiver<Integer>() {
            public void ready(Integer value) { delivered.set(value); }
            public void failed(Throwable error) { errors.incrementAndGet(); }
        };
    }
    public static void main(String[] args) throws Exception {
        BlockingQueue<Runnable> ui=new LinkedBlockingQueue<>();
        AtomicInteger disposed=new AtomicInteger(), delivered=new AtomicInteger(), errors=new AtomicInteger();
        LatestDecoder<Integer> worker=new LatestDecoder<>(ui::add, value->disposed.addAndGet(value));
        try {
            worker.submit(()->1,receiver(delivered,errors)); delivery(ui).run();
            check(delivered.get()==1 && disposed.get()==0);
            worker.submit(()->2,receiver(delivered,errors)); Runnable pending=delivery(ui);
            worker.cancel(); check(disposed.get()==2); pending.run(); check(delivered.get()==1);
            worker.submit(()->{throw new OutOfMemoryError("injected");},receiver(delivered,errors)); delivery(ui).run(); check(errors.get()==1);
            worker.submit(()->3,receiver(delivered,errors)); delivery(ui).run(); check(delivered.get()==3);
        } finally { finish(worker); }
        check(disposed.get()==2); // Delivered values belong to the caller, never recycled by worker.
        ui.clear(); disposed.set(0); delivered.set(0);
        AtomicInteger executed=new AtomicInteger(); CountDownLatch started=new CountDownLatch(1), release=new CountDownLatch(1);
        LatestDecoder<Integer> raced=new LatestDecoder<>(ui::add,value->disposed.addAndGet(value));
        try {
            raced.submit(()->{
                executed.incrementAndGet(); started.countDown();
                boolean done=false; while(!done)try{release.await();done=true;}catch(InterruptedException ignored){}
                return 1;
            },receiver(delivered,errors));
            check(started.await(3,TimeUnit.SECONDS));
            for(int n=2;n<=200;n++) { final int value=n; raced.submit(()->{executed.incrementAndGet();return value;},receiver(delivered,errors)); }
            release.countDown(); delivery(ui).run();
            check(executed.get()==2); check(delivered.get()==200); check(disposed.get()==1);
        } finally { release.countDown(); finish(raced); }
        ui.clear(); disposed.set(0); delivered.set(0);
        LatestDecoder<Integer> closed=new LatestDecoder<>(ui::add,value->disposed.addAndGet(value));
        closed.submit(()->9,receiver(delivered,errors)); Runnable late=delivery(ui); finish(closed);
        check(disposed.get()==9); late.run(); check(delivered.get()==0); check(disposed.get()==9);
        CountDownLatch rejectedDisposed = new CountDownLatch(1);
        LatestDecoder<Integer> rejected=new LatestDecoder<>(command->{throw new java.util.concurrent.RejectedExecutionException();},
            value->{disposed.addAndGet(value);rejectedDisposed.countDown();});
        try { rejected.submit(()->11,receiver(delivered,errors)); check(rejectedDisposed.await(3,TimeUnit.SECONDS)); }
        finally { finish(rejected); }
        check(disposed.get()==20);
        System.out.println("RESULT " + checks + " assertions passed: latest-only queue, stale disposal, close-before-delivery, error recovery, rejected delivery.");
        System.out.println("All decode executors terminated; no network/listeners or file deletion.");
    }
}
