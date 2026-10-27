package com.lanscreen.player;

import java.io.File;
import java.io.IOException;
import java.util.Set;

/** Only owns regular, direct children of the application's private media directory. */
final class CacheStore {
    interface Removal { void remove(File file) throws IOException; }
    interface Guard { void check() throws IOException; }
    interface Links { boolean symbolic(File file) throws IOException; }
    static final Removal DELETE = file -> {
        if (!file.delete() && file.exists()) throw new IOException("缓存文件无法清理");
    };
    final File directory;
    private final Removal removal;
    private final Links links;
    CacheStore(File directory, Removal removal, Links links) throws IOException {
        File absolute = new File(directory.getAbsoluteFile().getParentFile().getCanonicalFile(), directory.getName());
        if (links.symbolic(absolute) || !absolute.getCanonicalFile().equals(absolute)) throw new IOException("缓存目录不允许符号链接");
        if (!absolute.isDirectory() && !absolute.mkdirs()) throw new IOException("无法创建缓存目录");
        this.directory = absolute;
        this.removal = removal; this.links = links;
    }
    File file(String name) throws IOException {
        if (!ownedName(name)) throw new IOException("缓存文件名无效");
        return requireSafe(new File(directory, name));
    }
    File requireSafe(File file) throws IOException {
        File absolute = new File(file.getAbsoluteFile().getParentFile().getCanonicalFile(), file.getName());
        if (links.symbolic(absolute) || !directory.equals(absolute.getParentFile()) || !absolute.equals(absolute.getCanonicalFile()) ||
            !ownedName(absolute.getName()) || (absolute.exists() && !absolute.isFile()))
            throw new IOException("缓存路径无效");
        return absolute;
    }
    private static boolean ownedName(String name) {
        return name.matches("[a-f0-9]{64}(?:-repair[01]|-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})?\\.(?:media|part)");
    }
    private File[] entries() throws IOException {
        if (links.symbolic(directory) || !directory.equals(directory.getCanonicalFile())) throw new IOException("缓存目录已改变");
        File[] entries = directory.listFiles();
        if (entries == null) throw new IOException("无法读取缓存目录");
        return entries;
    }
    long bytes() throws IOException {
        long result = 0;
        for (File file : entries()) {
            if (links.symbolic(file) || !file.getAbsoluteFile().equals(file.getCanonicalFile())) throw new IOException("缓存中存在不安全的链接");
            if (file.isFile()) {
                if (Long.MAX_VALUE - result < file.length()) throw new IOException("缓存大小异常");
                result += file.length();
            }
        }
        return result;
    }
    void sweep(Set<String> protectedPaths, Guard guard) throws IOException {
        for (File file : entries()) {
            guard.check();
            if (!ownedName(file.getName())) continue;
            requireSafe(file);
            if (!protectedPaths.contains(file.getAbsolutePath())) removal.remove(file);
        }
    }
    void removeUnprotected(File file, Set<String> protectedPaths) throws IOException {
        requireSafe(file);
        if (protectedPaths.contains(file.getAbsolutePath())) throw new IOException("正在使用的缓存不可删除");
        if (file.exists()) removal.remove(file);
    }
    File repairDestination(String key, Set<String> protectedPaths) throws IOException {
        String[] slots = {key + ".media", key + "-repair0.media", key + "-repair1.media"};
        for (String slot : slots) {
            File file = file(slot);
            if (!protectedPaths.contains(file.getAbsolutePath())) return file;
        }
        throw new IOException("缓存修复等待当前素材切换");
    }
    static void requireTransitionCapacity(long current, long additional, long limit, long free, long reserve,
                                          boolean requiresDownload) throws IOException {
        if (requiresDownload) { requireCapacity(current, additional, limit, free, reserve); return; }
        // A complete reusable or empty manifest can release old files without allocating media bytes.
        // Persist and stop the previous decoder before any active files are swept.
        if (current < 0 || additional != 0 || limit < 0 || free < 0 || reserve < 0)
            throw new IOException("缓存交接参数无效");
    }
    static void requireCleanupCapacity(long current, long limit) throws IOException {
        if (current < 0 || limit < 0 || current > limit)
            throw new IOException("未使用缓存已清理，当前清单仍超出缓存上限，请缩小清单或提高限额");
    }
    static void requireCapacity(long current, long additional, long limit, long free, long reserve) throws IOException {
        if (current < 0 || additional < 0 || limit < 0 || free < 0 || reserve < 0 ||
            current > limit || additional > limit - current || free < reserve || additional > free - reserve)
            throw new IOException("缓存空间不足：当前清单与新清单须同时容纳，请减少素材或提高缓存上限");
    }
}
