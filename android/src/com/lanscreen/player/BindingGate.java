package com.lanscreen.player;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.locks.ReentrantLock;

/** Serializes durable binding writes while UI actions wait by scheduling, never by blocking. */
final class BindingGate {
    interface Scheduler { long now(); void later(Runnable task, long delay); void remove(Runnable task); }
    interface Guard { boolean valid(); }
    interface Abandoned { void run(boolean timedOut); }
    private static final long RETRY_MS = 50, TIMEOUT_MS = 5000;
    private final ReentrantLock lock = new ReentrantLock();
    private final Thread uiThread = Thread.currentThread();
    private final Scheduler scheduler;
    private final Map<Object, Pending> pending = new HashMap<>();
    private boolean closed;

    BindingGate(Scheduler scheduler) { this.scheduler = scheduler; }
    void lockBackground() {
        if (Thread.currentThread() == uiThread) throw new IllegalStateException("UI must use runUi");
        lock.lock();
    }
    void unlockBackground() { lock.unlock(); }
    void runUi(Object key, Guard guard, Runnable action, Abandoned abandoned) {
        runUi(key, guard, () -> { }, action, abandoned);
    }
    void runUi(Object key, Guard guard, Runnable waiting, Runnable action, Abandoned abandoned) {
        requireUi(); cancel(key);
        if (closed) { abandoned.run(false); return; }
        Pending next = new Pending(key, guard, action, abandoned); pending.put(key, next); waiting.run(); next.run();
    }
    void cancel(Object key) {
        requireUi(); Pending previous = pending.remove(key);
        if (previous != null) { scheduler.remove(previous); previous.abandoned.run(false); }
    }
    void cancelAll() { requireUi(); for (Object key : new ArrayList<>(pending.keySet())) cancel(key); }
    void close() { requireUi(); closed = true; cancelAll(); }
    private void requireUi() { if (Thread.currentThread() != uiThread) throw new IllegalStateException("UI scheduler required"); }
    private final class Pending implements Runnable {
        final Object key;
        final Guard guard;
        final Runnable action;
        final Abandoned abandoned;
        final long started = scheduler.now();
        Pending(Object key, Guard guard, Runnable action, Abandoned abandoned) {
            this.key = key; this.guard = guard; this.action = action; this.abandoned = abandoned;
        }
        private void drop(boolean timedOut) { pending.remove(key); scheduler.remove(this); abandoned.run(timedOut); }
        public void run() {
            requireUi();
            if (pending.get(key) != this) return;
            if (closed || !guard.valid()) { drop(false); return; }
            if (scheduler.now() - started >= TIMEOUT_MS) { drop(true); return; }
            if (!lock.tryLock()) {
                try { scheduler.later(this, RETRY_MS); }
                catch (RuntimeException rejected) { drop(false); }
                return;
            }
            try {
                if (closed || !guard.valid()) { drop(false); return; }
                // Remove before invoking UI: successful dialog dismissal must not cancel this action.
                pending.remove(key); scheduler.remove(this); action.run();
            } finally { lock.unlock(); }
        }
    }
}
