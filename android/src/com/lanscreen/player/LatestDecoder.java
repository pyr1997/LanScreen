package com.lanscreen.player;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.Callable;
import java.util.concurrent.Executor;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** One running decode and at most one replacement; the receiver owns delivered values. */
final class LatestDecoder<T> {
    interface Disposal<T> { void dispose(T value); }
    interface Receiver<T> { void ready(T value); void failed(Throwable error); }
    private final ThreadPoolExecutor worker = new ThreadPoolExecutor(1, 1, 0L, TimeUnit.MILLISECONDS,
        new ArrayBlockingQueue<Runnable>(1), runnable -> new Thread(runnable, "lanscreen-image-decode"));
    private final Executor delivery;
    private final Disposal<T> disposal;
    private Future<?> future;
    private long generation;
    private boolean closed;
    private T waiting;

    LatestDecoder(Executor delivery, Disposal<T> disposal) { this.delivery = delivery; this.disposal = disposal; }
    synchronized void submit(Callable<T> decode, Receiver<T> receiver) {
        if (closed) return;
        cancel();
        final long ticket = generation;
        future = worker.submit(() -> {
            T result = null;
            Throwable failure = null;
            try { result = decode.call(); } catch (Exception | OutOfMemoryError error) { failure = error; }
            synchronized (LatestDecoder.this) {
                if (closed || ticket != generation) { if (result != null) disposal.dispose(result); return; }
                waiting = result;
            }
            final Throwable error = failure;
            try { delivery.execute(() -> deliver(ticket, receiver, error)); }
            catch (RuntimeException rejected) {
                synchronized (LatestDecoder.this) {
                    if (ticket == generation) discardWaiting();
                }
            }
        });
    }
    private void deliver(long ticket, Receiver<T> receiver, Throwable error) {
        T value;
        synchronized (this) {
            if (closed || ticket != generation) return;
            value = waiting; waiting = null;
        }
        if (error == null) receiver.ready(value);
        else { if (value != null) disposal.dispose(value); receiver.failed(error); }
    }
    private void discardWaiting() { if (waiting != null) { disposal.dispose(waiting); waiting = null; } }
    synchronized void cancel() {
        generation++;
        if (future != null) { future.cancel(true); future = null; }
        worker.purge();
        discardWaiting();
    }
    synchronized void close() { if (!closed) { closed = true; cancel(); worker.shutdownNow(); } }
    boolean awaitTermination(long millis) throws InterruptedException { return worker.awaitTermination(millis, TimeUnit.MILLISECONDS); }
}
