package com.lanscreen.player;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;

/** Real private-cache filesystem exercises; removed synthetic files are retained as evidence. */
public final class CacheStoreTest {
    private static int passed;
    private static File root;
    interface Test { void run() throws Exception; }
    static void test(String name, Test test) throws Exception {
        test.run(); passed++; System.out.println("PASS " + name);
    }
    static void check(boolean value) { if (!value) throw new AssertionError(); }
    static void rejects(Test test) throws Exception {
        try { test.run(); throw new AssertionError("expected rejection"); } catch (IOException expected) { }
    }
    static String key(int n) { return String.format("%064x", n); }
    static File dir(String name) throws Exception {
        File result = new File(root, name); if (!result.mkdirs()) throw new IOException("fixture exists"); return result;
    }
    static CacheStore store(String name) throws Exception {
        File fixture = dir(name), cache = new File(fixture, "media"), evidence = new File(fixture, "retained");
        if (!evidence.mkdir()) throw new IOException("fixture setup");
        return new CacheStore(cache, file -> Files.move(file.toPath(), new File(evidence, file.getName()).toPath()), file -> Files.isSymbolicLink(file.toPath()));
    }
    static File write(CacheStore store, String name, int bytes) throws Exception {
        File file = store.file(name); Files.write(file.toPath(), new byte[bytes]); return file;
    }
    static Set<String> protect(File... files) {
        Set<String> result = new HashSet<>(); for (File file : files) result.add(file.getAbsolutePath()); return result;
    }
    public static void main(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("new unique fixture root required");
        root = new File(args[0]).getCanonicalFile(); if (!root.mkdirs()) throw new IOException("fixture root already exists");
        test("active and pending survive; obsolete complete and partial leave cache", () -> {
            CacheStore cache = store("handover");
            File active = write(cache, key(1)+".media", 11), pending = write(cache, key(2)+".media", 13);
            File old = write(cache, key(3)+".media", 17), part = write(cache, key(4)+".part", 19);
            cache.sweep(protect(active, pending), () -> { });
            check(active.exists() && pending.exists() && !old.exists() && !part.exists()); check(cache.bytes() == 24);
            check(new File(cache.directory.getParentFile(), "retained/" + old.getName()).length() == 17);
            check(new File(cache.directory.getParentFile(), "retained/" + part.getName()).length() == 19);
        });
        test("successful handover reclaims only old active; repeated cleanup idempotent", () -> {
            CacheStore cache = store("applied"); File old = write(cache,key(1)+".media",7), next = write(cache,key(2)+".media",9);
            cache.sweep(protect(old,next),()->{}); check(old.exists());
            cache.sweep(protect(next),()->{}); cache.sweep(protect(next),()->{});
            check(!old.exists() && next.exists() && cache.bytes()==9);
        });
        test("unrecognized app files are preserved and counted", () -> {
            CacheStore cache = store("unknown"); File other = new File(cache.directory,"notes.txt"); Files.write(other.toPath(),new byte[5]);
            cache.sweep(Collections.emptySet(),()->{}); check(other.exists() && cache.bytes()==5);
        });
        test("path traversal and foreign directories rejected", () -> {
            CacheStore cache=store("paths"); rejects(()->cache.file("../"+key(1)+".media"));
            rejects(()->cache.requireSafe(new File(root,key(1)+".media")));
        });
        test("leaf symlink neither followed nor removed", () -> {
            CacheStore cache=store("leaf-link"); File outside=new File(root,"outside.bin"); Files.write(outside.toPath(),new byte[23]);
            File link=new File(cache.directory,key(1)+".media"); Files.createSymbolicLink(link.toPath(),outside.toPath());
            rejects(()->cache.file(link.getName())); rejects(()->cache.sweep(Collections.emptySet(),()->{})); check(outside.length()==23 && Files.isSymbolicLink(link.toPath()));
        });
        test("dangling symlink rejected before a staging write can follow it", () -> {
            CacheStore cache=store("dangling-link"); File missing=new File(root,"must-not-be-created.bin");
            File link=new File(cache.directory,key(9)+".part"); Files.createSymbolicLink(link.toPath(),missing.toPath());
            rejects(()->cache.file(link.getName())); rejects(()->cache.sweep(Collections.emptySet(),()->{}));
            check(!missing.exists() && Files.isSymbolicLink(link.toPath()));
        });
        test("trusted ancestor alias accepted and normalized", () -> {
            File fixture=dir("ancestor"), real=new File(fixture,"real"); check(real.mkdir());
            File alias=new File(fixture,"alias"); Files.createSymbolicLink(alias.toPath(),real.toPath());
            CacheStore cache=new CacheStore(new File(alias,"media"),file->{throw new IOException("not needed");}, file -> Files.isSymbolicLink(file.toPath()));
            File file=write(cache,key(1)+".media",4);
            check(cache.directory.equals(new File(real,"media")));
            check(cache.requireSafe(new File(alias,"media/"+file.getName())).equals(file));
        });
        test("cache directory leaf symlink rejected", () -> {
            File fixture=dir("directory-link"), outside=dir("foreign-media"), link=new File(fixture,"media");
            Files.createSymbolicLink(link.toPath(),outside.toPath()); rejects(()->new CacheStore(link, file->{throw new IOException("not needed");}, file -> Files.isSymbolicLink(file.toPath())));
        });
        test("repair keeps corrupt active file and uses bounded deterministic slot", () -> {
            CacheStore cache=store("repair"); File active=write(cache,key(1)+".media",3);
            File repair=cache.repairDestination(key(1),protect(active)); check(repair.getName().equals(key(1)+"-repair0.media"));
            Files.write(repair.toPath(),new byte[4]); rejects(()->cache.removeUnprotected(active,protect(active)));
            cache.sweep(protect(repair),()->{}); check(!active.exists() && repair.exists());
            check(cache.repairDestination(key(1),protect(repair)).getName().equals(key(1)+".media"));
        });
        test("all repair slots protected blocks instead of creating UUID copies", () -> {
            CacheStore cache=store("repair-full"); Set<String> protectedFiles=protect(cache.file(key(1)+".media"),cache.file(key(1)+"-repair0.media"),cache.file(key(1)+"-repair1.media"));
            rejects(()->cache.repairDestination(key(1),protectedFiles));
        });
        test("cancelled cleanup leaves protected playback and does not continue", () -> {
            CacheStore cache=store("cancel"); File active=write(cache,key(1)+".media",7), old=write(cache,key(2)+".media",8);
            rejects(()->cache.sweep(protect(active),()->{throw new IOException("generation changed");})); check(active.exists() && old.exists());
        });
        test("quota includes old playback and full new plan", () -> {
            CacheStore.requireCapacity(60,40,100,80,40); rejects(()->CacheStore.requireCapacity(60,41,100,1000,40));
            rejects(()->CacheStore.requireCapacity(60,40,100,79,40)); rejects(()->CacheStore.requireCapacity(101,0,100,1000,40));
        });
        test("unknown size streaming never writes a chunk past quota or reserve", () -> {
            long written=0; int accepted=0;
            for (int i=0;i<5;i++) {
                try { CacheStore.requireCapacity(40+written,16,100,100-written,40); written+=16; accepted++; }
                catch (IOException expected) { break; }
            }
            check(accepted==3 && written==48); rejects(()->CacheStore.requireCapacity(Long.MAX_VALUE-2,5,Long.MAX_VALUE,100,0));
        });
        test("over-quota complete replacement switches before old bytes are reclaimed", () -> {
            CacheStore cache=store("quota-recovery");
            File old=write(cache,key(1)+".media",800), next=write(cache,key(2)+".media",200);
            cache.sweep(protect(old,next),()->{});
            CacheStore.requireTransitionCapacity(cache.bytes(),0,512,0,256,false);
            check(old.exists() && next.exists());
            cache.sweep(protect(next),()->{});
            CacheStore.requireCleanupCapacity(cache.bytes(),512);
            check(cache.bytes()==200 && !old.exists() && next.exists());
            check(new File(cache.directory.getParentFile(),"retained/"+old.getName()).length()==800);
        });
        test("empty withdrawal remains possible below reserve and reclaims old media", () -> {
            CacheStore cache=store("empty-recovery"); File old=write(cache,key(1)+".media",800);
            CacheStore.requireTransitionCapacity(cache.bytes(),0,512,0,256,false);
            cache.sweep(protect(old),()->{}); check(old.exists());
            cache.sweep(Collections.emptySet(),()->{});
            CacheStore.requireCleanupCapacity(cache.bytes(),512); check(cache.bytes()==0);
            check(new File(cache.directory.getParentFile(),"retained/"+old.getName()).length()==800);
        });
        System.out.println("RESULT " + passed + "/" + passed + " passed. Synthetic removal bytes retained at " + root);
        System.out.println("Boundary: production File.delete operator reviewed but not executed; no Android runtime/decoder test.");
    }
}
