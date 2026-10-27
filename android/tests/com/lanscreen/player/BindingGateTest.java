package com.lanscreen.player;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/** Exercises the production gate with real competing writers and a deterministic UI scheduler. */
public final class BindingGateTest {
    private static int checks;
    private static void check(boolean value) { checks++; if (!value) throw new AssertionError("gate assertion " + checks); }
    private static void await(CountDownLatch latch) throws Exception {
        if (!latch.await(3, TimeUnit.SECONDS)) throw new AssertionError("bounded worker timeout");
    }
    private static final class Clock implements BindingGate.Scheduler {
        static final class Entry { Runnable task; long at; Entry(Runnable task,long at){this.task=task;this.at=at;} }
        long time; boolean reject;
        final List<Entry> tasks=new ArrayList<>();
        public long now(){return time;}
        public void later(Runnable task,long delay){if(reject)throw new IllegalStateException("closed scheduler");tasks.add(new Entry(task,time+delay));}
        public void remove(Runnable task){for(int i=tasks.size()-1;i>=0;i--)if(tasks.get(i).task==task)tasks.remove(i);}
        void advance(long millis){
            long end=time+millis;
            while(true){
                Entry next=null; for(Entry entry:tasks)if(entry.at<=end&&(next==null||entry.at<next.at))next=entry;
                if(next==null)break; tasks.remove(next);time=next.at;next.task.run();
            }
            time=end;
        }
    }
    private static final class Held implements AutoCloseable {
        final CountDownLatch entered=new CountDownLatch(1), release=new CountDownLatch(1);
        final AtomicReference<Throwable> failure=new AtomicReference<>();
        final Thread thread;
        Held(BindingGate gate) throws Exception {
            thread=new Thread(()->{
                gate.lockBackground();
                try {entered.countDown();await(release);}catch(Throwable error){failure.set(error);}
                finally{gate.unlockBackground();}
            },"binding-gate-test-writer");
            thread.start(); await(entered);
        }
        public void close() throws Exception { release.countDown();thread.join(3500);check(!thread.isAlive());if(failure.get()!=null)throw new AssertionError(failure.get()); }
    }
    private static final class State {
        volatile long epoch=1;
        volatile BindingSession current=new BindingSession(repeat('a'),"pending",1);
        volatile String durable="pending";
        volatile boolean visible, dialog=true, destroyed;
    }
    private static final class Controls {
        final boolean[] enabled={true,true,true};
        void editable(boolean value){for(int i=0;i<enabled.length;i++)enabled[i]=value;}
        boolean all(boolean value){return enabled[0]==value&&enabled[1]==value&&enabled[2]==value;}
    }
    private static String repeat(char c){StringBuilder value=new StringBuilder();for(int i=0;i<64;i++)value.append(c);return value.toString();}

    public static void main(String[] args) throws Exception {
        Clock clock=new Clock(); BindingGate gate=new BindingGate(clock);
        AtomicInteger actions=new AtomicInteger(), discarded=new AtomicInteger(), timeouts=new AtomicInteger();
        try(Held held=new Held(gate)) {
            long began=System.nanoTime();
            gate.runUi("settings",()->true,actions::incrementAndGet,t->{discarded.incrementAndGet();if(t)timeouts.incrementAndGet();});
            check(TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-began)<1000);
            check(actions.get()==0 && held.release.getCount()==1); // UI returned while durable writer is still held.
            clock.advance(200);check(actions.get()==0 && clock.tasks.size()==1);
        }
        clock.advance(50);check(actions.get()==1 && discarded.get()==0 && clock.tasks.isEmpty());

        // Duplicate intents replace earlier work; dismissed dialogs cannot later save anything.
        try(Held held=new Held(gate)) {
            AtomicInteger old=new AtomicInteger(), latest=new AtomicInteger();
            gate.runUi("settings",()->true,old::incrementAndGet,t->discarded.incrementAndGet());
            Runnable late=clock.tasks.get(0).task;
            gate.runUi("settings",()->true,latest::incrementAndGet,t->discarded.incrementAndGet());
            check(clock.tasks.size()==1);late.run();check(old.get()==0 && latest.get()==0);
            gate.cancel("settings");late.run();check(clock.tasks.isEmpty() && old.get()==0 && latest.get()==0);
            gate.runUi("apply",()->true,actions::incrementAndGet,t->{if(t)timeouts.incrementAndGet();});
            clock.advance(5000);check(timeouts.get()==1 && clock.tasks.isEmpty() && actions.get()==1);
        }
        gate.runUi("apply",()->true,actions::incrementAndGet,t->{throw new AssertionError("retry discarded");});
        check(actions.get()==2); // Timed-out actions can be explicitly retried, never reported as success.

        // The actual waiting/abandoned hooks keep all settings inputs locked to the submitted snapshot.
        Controls controls=new Controls();AtomicInteger submitted=new AtomicInteger();
        try(Held held=new Held(gate)) {
            gate.runUi("form",()->true,()->controls.editable(false),submitted::incrementAndGet,t->controls.editable(true));
            check(controls.all(false) && submitted.get()==0);
            gate.cancel("form");check(controls.all(true) && submitted.get()==0);
            gate.runUi("form",()->true,()->controls.editable(false),submitted::incrementAndGet,t->controls.editable(true));
            clock.advance(5000);check(controls.all(true) && submitted.get()==0);
            gate.runUi("form",()->true,()->controls.editable(false),submitted::incrementAndGet,t->controls.editable(true));
            gate.runUi("form",()->true,()->controls.editable(false),submitted::incrementAndGet,t->controls.editable(true));
            check(controls.all(false) && clock.tasks.size()==1); // Replacement restores old controls before locking new ones.
        }
        clock.advance(50);check(submitted.get()==1 && clock.tasks.isEmpty());

        // Revalidate after acquiring the lock too, not just before trying it.
        AtomicInteger guards=new AtomicInteger();
        gate.runUi("changing",()->guards.incrementAndGet()==1,()->{throw new AssertionError("stale action");},t->discarded.incrementAndGet());
        check(guards.get()==2);
        AtomicInteger successDismiss=new AtomicInteger();
        gate.runUi("dialog",()->true,()->{actions.incrementAndGet();gate.cancel("dialog");},t->successDismiss.incrementAndGet());
        check(successDismiss.get()==0 && actions.get()==3);

        // OnPause/close dismiss all pending actions; even an already extracted timer is inert.
        try(Held held=new Held(gate)) {
            gate.runUi("settings",()->true,actions::incrementAndGet,t->discarded.incrementAndGet());
            gate.runUi("registration",()->true,actions::incrementAndGet,t->discarded.incrementAndGet());
            Runnable late=clock.tasks.get(0).task;gate.cancelAll();late.run();check(clock.tasks.isEmpty() && actions.get()==3);
            clock.reject=true;gate.runUi("rejected",()->true,actions::incrementAndGet,t->discarded.incrementAndGet());
            check(clock.tasks.isEmpty());clock.reject=false;
        }

        // Real commit order: an old active write finishes, then revocation persists; its queued UI grant is stale.
        State state=new State(); BindingSession requested=state.current;
        CountDownLatch activeEntered=new CountDownLatch(1), releaseActive=new CountDownLatch(1), revokeEntered=new CountDownLatch(1), releaseRevoke=new CountDownLatch(1);
        AtomicReference<Throwable> failure=new AtomicReference<>();
        Thread active=new Thread(()->{
            gate.lockBackground();try{activeEntered.countDown();await(releaseActive);state.durable="active";}
            catch(Throwable error){failure.set(error);}finally{gate.unlockBackground();}
        },"binding-gate-test-active-commit");
        Thread revoke=new Thread(()->{
            gate.lockBackground();try{
                state.current=state.current.lock("device_revoked");state.epoch++;state.visible=false;revokeEntered.countDown();
                await(releaseRevoke);state.durable="device_revoked";
            }catch(Throwable error){failure.set(error);}finally{gate.unlockBackground();}
        },"binding-gate-test-revoked-commit");
        active.start();
        try {
            await(activeEntered);
            gate.runUi("registration",()->state.epoch==1 && state.current==requested && !state.destroyed,
                ()->{state.current=new BindingSession(requested.credential,"active",1);state.visible=true;},t->discarded.incrementAndGet());
            revoke.start();releaseActive.countDown();await(revokeEntered);
            check(!state.current.canPlay() && !state.visible && "active".equals(state.durable));
            clock.advance(50);check(!state.current.canPlay() && clock.tasks.isEmpty());
        } finally {
            releaseActive.countDown();releaseRevoke.countDown();active.join(3500);revoke.join(3500);
            check(!active.isAlive() && !revoke.isAlive());if(failure.get()!=null)throw new AssertionError(failure.get());
        }
        check("device_revoked".equals(state.durable) && !state.current.canPlay());

        // Old-server rejection cannot touch the new session. Cancelled reapply never rotates a credential.
        BindingSession old=state.current;long oldEpoch=state.epoch;
        try(Held held=new Held(gate)) {
            gate.runUi("old-rejection",()->state.epoch==oldEpoch && state.current==old,
                ()->state.current=state.current.lock("device_token_invalid"),t->discarded.incrementAndGet());
            state.current=new BindingSession(repeat('b'),"active",2);state.epoch++;
            gate.runUi("reapply",()->state.dialog && !state.destroyed,actions::incrementAndGet,t->discarded.incrementAndGet());
            state.dialog=false;clock.advance(50);check(state.current.canPlay() && actions.get()==3 && clock.tasks.isEmpty());
            state.dialog=true;gate.runUi("destroy",()->!state.destroyed,actions::incrementAndGet,t->discarded.incrementAndGet());
            Runnable late=clock.tasks.get(0).task;state.destroyed=true;gate.close();late.run();check(clock.tasks.isEmpty() && actions.get()==3);
        }
        gate.runUi("closed",()->true,actions::incrementAndGet,t->discarded.incrementAndGet());check(actions.get()==3);
        try {gate.lockBackground();throw new AssertionError("UI blocking entry allowed");}catch(IllegalStateException expected){checks++;}
        System.out.println("RESULT "+checks+" BindingGate assertions passed: slow durable writer, nonblocking UI, replacement, cancellation, timeout/retry, second guard, successful dismissal, durable revoke order, stale sessions, destruction.");
        System.out.println("All explicitly started writer threads joined; no network, services, file deletion or Android UI launched.");
    }
}
