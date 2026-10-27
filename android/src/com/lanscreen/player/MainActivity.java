package com.lanscreen.player;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.StatFs;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.VideoView;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.UUID;
import java.util.Set;
import java.util.HashSet;
import java.util.Map;
import java.util.HashMap;
import java.util.Collections;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Framework-only TV player: no cloud services, browser engine or runtime downloads. */
public final class MainActivity extends Activity {
    private static final int BG = Color.rgb(5, 8, 15), INK = Color.rgb(240, 246, 252);
    private static final int TEAL = Color.rgb(45, 212, 191), MUTED = Color.rgb(155, 174, 197);
    private static final String VERSION = "1.3.2";
    private static final long DEFAULT_LIMIT = 2L * 1024 * 1024 * 1024, DEFAULT_RESERVE = 256L * 1024 * 1024;
    private static final long MAX_MEDIA_BYTES = 1024L * 1024 * 1024;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private final ExecutorService heartbeatNetwork = Executors.newSingleThreadExecutor();
    private final LatestDecoder<Bitmap> imageDecoder = new LatestDecoder<>(command -> {
        if (!ui.post(command)) throw new java.util.concurrent.RejectedExecutionException("activity handler closed");
    }, value -> { if (!value.isRecycled()) value.recycle(); });
    private final PlaybackSafety.StallTracker stallTracker = new PlaybackSafety.StallTracker();
    private final PlaybackSafety.Failures failures = new PlaybackSafety.Failures();
    private boolean playingVideo;
    private int videoRecoveries;
    private String playingKey = "";
    private final BindingGate bindingGate = new BindingGate(new BindingGate.Scheduler() {
        public long now() { return android.os.SystemClock.uptimeMillis(); }
        public void later(Runnable task, long delay) {
            if (!ui.postDelayed(task, delay)) throw new java.util.concurrent.RejectedExecutionException("activity handler closed");
        }
        public void remove(Runnable task) { ui.removeCallbacks(task); }
    });
    private volatile BindingSession binding = new BindingSession("", "initializing", 0);
    private volatile boolean loadingBinding = true;
    private boolean registrationBusy;
    private SharedPreferences prefs;
    private FrameLayout root;
    private ImageView image;
    private VideoView video;
    private TextView message;
    private final ImageHandoff<Bitmap> imageFrames = new ImageHandoff<>(value -> {
        image.setImageBitmap(value); image.setVisibility(value == null ? View.GONE : View.VISIBLE);
    }, value -> { if (!value.isRecycled()) value.recycle(); });
    private Runnable imageLoadingHint, imageDecodeTimeout;
    private volatile JSONObject active, pending, prepared;
    private volatile long appliedRevision = -1, downloadedRevision = -1, cleanupRevision = -1;
    private volatile long cacheBytes = -1, freeBytes = -1, cacheLimitBytes = DEFAULT_LIMIT, cacheReserveBytes = DEFAULT_RESERVE;
    private volatile long cleanupRequested = 0, cleanupCompletedRequest = 0, cleanupCommandEpoch = 0;
    private volatile String cleanupState = "pending", cleanupError = "";
    private final Set<HttpURLConnection> connections = Collections.synchronizedSet(new HashSet<HttpURLConnection>());
    private volatile boolean loadingCache;
    private boolean cleanupQueued, cleanupRerun;
    private final Object lifecycle = new Object();
    private volatile String base = "", token = "", deviceName = "公司宣传屏", deviceId;
    private volatile long generation = 0;
    private volatile boolean destroyed = false;
    private volatile String currentItem = "", playbackStatus = "idle";
    private boolean resumed, paused, dialogVisible, syncing, heartbeatBusy;
    private int index = -1, refreshSeconds = 30;
    private volatile int playbackSerial = 0;
    private int integrityRequest = 0, integrityChecked = -1;
    private String syncState = "等待连接";
    private AlertDialog controls, settings;

    private final Runnable advance = new Runnable() { public void run() { nextItem(); } };
    private final Runnable watchdog = new Runnable() { public void run() {
        if (!resumed || paused || dialogVisible) return;
        recoverVideo("视频未能完成播放");
    }};
    private final Runnable videoProgress = new Runnable() { public void run() {
        if (!resumed || paused || dialogVisible || destroyed || !playingVideo) return;
        try {
            if (stallTracker.stalled(android.os.SystemClock.elapsedRealtime(), video.getCurrentPosition())) {
                recoverVideo("视频播放停滞"); return;
            }
        } catch (Exception error) { recoverVideo("视频播放状态异常"); return; }
        ui.postDelayed(this, 5000L);
    }};
    private final Runnable poll = new Runnable() { public void run() {
        if (!resumed || destroyed) return;
        refreshBinding();
        ui.postDelayed(this, 30000L);
    }};

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        prefs = getSharedPreferences("lanscreen", MODE_PRIVATE);
        base = prefs.getString("server", ""); token = prefs.getString("token", "");
        deviceName = prefs.getString("deviceName", android.os.Build.MODEL);
        deviceId = prefs.getString("deviceId", "");
        if (deviceId.isEmpty()) {
            deviceId = UUID.randomUUID().toString();
            // loadBinding commits this identity together with its authorization record before any request.
        }
        root = new FrameLayout(this); root.setBackgroundColor(BG);
        root.setFocusable(true); root.setFocusableInTouchMode(true);
        video = new VideoView(this);
        FrameLayout.LayoutParams vp = new FrameLayout.LayoutParams(-1, -1, Gravity.CENTER);
        root.addView(video, vp);
        image = new ImageView(this); image.setScaleType(ImageView.ScaleType.FIT_CENTER);
        root.addView(image, new FrameLayout.LayoutParams(-1, -1));
        message = new TextView(this); message.setTextColor(INK); message.setTextSize(26);
        message.setGravity(Gravity.CENTER); message.setPadding(dp(60), dp(40), dp(60), dp(40));
        root.addView(message, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root); immerse(); root.requestFocus();
        loadBinding(false);
    }

    @Override public void onResume() {
        super.onResume(); resumed = true; immerse();
        if (base.isEmpty() || token.isEmpty()) {
            showMessage("局域宣传屏\n\n连接公司局域网，在设置中填写服务器地址与播放口令");
            if (!dialogVisible) ui.postDelayed(new Runnable() { public void run() { showSettings(); } }, 200);
        } else if (!dialogVisible && !paused) startCurrentOrNext();
        ui.removeCallbacks(poll); ui.post(poll);
    }
    @Override public void onPause() {
        resumed = false; bindingGate.cancelAll(); ui.removeCallbacks(poll); stopMedia(); super.onPause();
    }
    @Override public void onDestroy() {
        destroyed = true; generation++; bindingGate.close(); ui.removeCallbacksAndMessages(null);
        cancelConnections(); network.shutdownNow(); heartbeatNetwork.shutdownNow(); imageDecoder.close(); releaseBitmap(); super.onDestroy();
    }
    @Override public void onWindowFocusChanged(boolean focus) {
        super.onWindowFocusChanged(focus); if (focus) immerse();
    }
    private void immerse() {
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN |
            View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY |
            View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION |
            View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }
    private int dp(int n) { return (int)(n * getResources().getDisplayMetrics().density + 0.5f); }
    private String bindingScope() { return sha256(base + "\n" + token); }
    private String cacheKey() { return "manifest_" + sha256(base + "\n" + token + "\n" + binding.credential + "\n" + binding.version); }
    private boolean authorized() { return !loadingBinding && binding.canPlay(); }
    private void showBindingState() {
        String state = binding.state;
        String text = "device_revoked".equals(state) ? "此电视已被管理员解绑\n如需重新使用，请在控制面板重新申请绑定" :
            "device_token_invalid".equals(state) ? "设备绑定凭证无效\n请在控制面板重新申请绑定" :
            "device_unknown".equals(state) ? "服务器没有此设备的有效绑定\n请在控制面板重新申请绑定" :
            "upgrade_required".equals(state) ? "服务端要求升级电视应用，请联系管理员" :
            "storage_error".equals(state) ? "无法保存设备授权状态，已停止播放\n请检查电视存储并重新打开应用" :
            loadingBinding ? "正在读取设备绑定状态…" : "等待管理员批准此电视\n请在电脑后台的电视设备中批准绑定申请";
        currentItem = ""; playbackStatus = "idle"; showMessage(text + "\n\n设备编号：" + deviceId +
            (binding.version > 0 ? "\n接入批次：" + binding.version : "") + "\n\n遥控器确定键 · 打开控制面板");
    }
    private void resetPlaybackForBinding() {
        stopMedia();
        synchronized (lifecycle) { active = null; pending = null; prepared = null; }
        index = -1; appliedRevision = downloadedRevision = cleanupRevision = -1;
        cleanupRequested = cleanupCompletedRequest = 0; cleanupState = "pending"; cleanupError = "";
        currentItem = ""; playbackStatus = "idle";
    }
    private String bindingJSON(BindingSession session) throws Exception {
        return new JSONObject().put("credential", session.credential).put("state", session.state).put("version", session.version).toString();
    }
    private void saveBinding(String scope, BindingSession session) throws Exception {
        if (!prefs.edit().putString("deviceId", deviceId).putString("binding_" + scope, bindingJSON(session)).commit()) throw new IOException("无法保存设备绑定状态");
    }
    private void loadBinding(boolean reapply) {
        final long epoch = generation;
        final String scope = bindingScope(), serverKey = "deviceCredential_" + sha256(base);
        final boolean configured = !base.isEmpty() && !token.isEmpty();
        loadingBinding = true; loadingCache = false; binding = new BindingSession("", "initializing", 0);
        resetPlaybackForBinding();
        if (!configured) { loadingBinding = false; return; }
        network.execute(() -> {
            BindingSession loaded;
            try {
                bindingGate.lockBackground();
                try {
                    checkEpoch(epoch);
                    String credential = prefs.getString(serverKey, "");
                    if (reapply || credential.isEmpty()) {
                        byte[] bytes = new byte[32]; new java.security.SecureRandom().nextBytes(bytes); credential = hex(bytes);
                        if (!prefs.edit().putString(serverKey, credential).commit()) throw new IOException("无法保存设备凭证");
                    }
                    JSONObject stored = new JSONObject(prefs.getString("binding_" + scope, "{}"));
                    String state = !reapply && credential.equals(stored.optString("credential", "")) ? stored.optString("state", "unregistered") : "unregistered";
                    long version = !reapply && credential.equals(stored.optString("credential", "")) ? stored.optLong("version", 0) : 0;
                    state = BindingSession.restoredState(state);
                    if (!BindingSession.validCredential(credential)) state = "device_token_invalid";
                    loaded = new BindingSession(credential, state, version);
                    saveBinding(scope, loaded); checkEpoch(epoch);
                } finally { bindingGate.unlockBackground(); }
            } catch (Exception failure) { loaded = new BindingSession("", "storage_error", 0); }
            final BindingSession ready = loaded;
            ui.post(() -> {
                if (destroyed || epoch != generation) return;
                binding = ready; loadingBinding = false;
                if (authorized()) loadCached(); else showBindingState();
                refreshBinding();
            });
        });
    }
    private void lockBinding(String code, long epoch) {
        final long lockedEpoch;
        bindingGate.lockBackground();
        try {
            if (destroyed || epoch != generation) return;
            binding = binding.lock(BindingSession.stateForError(code));
            generation++; lockedEpoch = generation;
            // Stop visible content without waiting for a potentially slow durable lock record.
            ui.post(() -> {
                if (destroyed || lockedEpoch != generation) return;
                loadingBinding = false; loadingCache = false;
                resetPlaybackForBinding(); showBindingState();
            });
            try { saveBinding(bindingScope(), binding); }
            catch (Exception failure) { binding = binding.lock("storage_error"); }
        } finally { bindingGate.unlockBackground(); }
        cancelConnections();
        ui.post(() -> {
            if (destroyed || lockedEpoch != generation) return;
            loadingBinding = false; loadingCache = false;
            resetPlaybackForBinding(); showBindingState();
        });
    }
    private void refreshBinding() {
        if (destroyed || loadingBinding || registrationBusy || base.isEmpty() || token.isEmpty() || !binding.canRegister()) return;
        registrationBusy = true;
        final long epoch = generation;
        final String server = base, credential = token, scope = bindingScope();
        final BindingSession requested = binding;
        heartbeatNetwork.execute(() -> {
            HttpURLConnection c = null;
            try {
                c = connection(server + "/api/player/register", credential, epoch);
                c.setRequestMethod("POST"); c.setDoOutput(true); c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                byte[] body = new JSONObject().put("deviceId", deviceId).put("name", deviceName).put("version", VERSION)
                    .put("protocolVersion", 3).toString().getBytes(StandardCharsets.UTF_8);
                c.setFixedLengthStreamingMode(body.length);
                try (java.io.OutputStream out = c.getOutputStream()) { out.write(body); }
                requireOK(c, epoch);
                JSONObject reply = new JSONObject(readText(c.getInputStream(), 65536, epoch, 60000L));
                String state = reply.getString("bindingState"); long version = reply.getLong("bindingVersion");
                if ((!state.equals("active") && !state.equals("pending")) || version <= 0 || !deviceId.equals(reply.getString("deviceId"))) {
                    lockBinding("device_unknown", epoch); throw new IOException("服务器绑定响应无效");
                }
                final BindingSession accepted = new BindingSession(requested.credential, state, version);
                bindingGate.lockBackground();
                try {
                    checkEpoch(epoch);
                    if (!requested.state.equals(accepted.state) || requested.version != accepted.version) {
                        try { saveBinding(scope, accepted); } catch (Exception storage) { throw new BindingStorageFailure(); }
                    }
                } finally { bindingGate.unlockBackground(); }
                ui.post(() -> bindingGate.runUi("registration", () -> !destroyed && resumed && epoch == generation && binding == requested, () -> {
                    boolean changed = !binding.state.equals(accepted.state) || binding.version != accepted.version;
                    binding = accepted;
                    if (changed) generation++;
                    if (changed) { cancelConnections(); resetPlaybackForBinding(); }
                    if (!authorized()) showBindingState();
                    else if (changed) loadCached();
                    else { sendHeartbeat(); sync(); }
                }, timedOut -> { }));
            } catch (Exception error) {
                if (error instanceof BindingStorageFailure) lockBinding("storage_error", epoch);
                ui.post(() -> {
                    if (destroyed || epoch != generation) return;
                    syncState = error instanceof HttpFailure ? error.getMessage() : "绑定服务暂不可用";
                    if (authorized()) { sendHeartbeat(); sync(); }
                    else if (!dialogVisible) { showBindingState(); message.append("\n\n" + syncState); }
                });
            } finally {
                if (c != null) close(c);
                ui.post(() -> { registrationBusy = false; });
            }
        });
    }
    private void loadCached() {
        if (!authorized()) { showBindingState(); return; }
        integrityRequest++; failures.clear(); loadingCache = true;
        synchronized (lifecycle) { active = null; pending = null; prepared = null; } index = -1;
        appliedRevision = downloadedRevision = cleanupRevision = -1;
        cleanupRequested = cleanupCompletedRequest = 0;
        cacheBytes = freeBytes = -1; cleanupState = "pending"; cleanupError = "";
        final long epoch = generation;
        final String owner = cacheKey();
        network.execute(() -> {
            JSONObject cached = null;
            try {
                checkEpoch(epoch);
                String raw = owner.equals(prefs.getString("manifestOwner", "")) ?
                    prefs.getString("currentManifest", "{}") : prefs.getString(owner, "{}");
                JSONObject parsed = new JSONObject(raw);
                JSONArray items = parsed.getJSONArray("items");
                CacheStore store = store();
                for (int i = 0; i < items.length(); i++) {
                    checkEpoch(epoch);
                    JSONObject item = items.getJSONObject(i);
                    File file = store.requireSafe(new File(item.getString("local")));
                    item.put("local", file.getAbsolutePath());
                    if (!file.isFile() || file.length() <= 0 || file.length() != item.optLong("cachedBytes", file.length()))
                        throw new IOException("缓存缺失");
                }
                checkEpoch(epoch);
                SharedPreferences.Editor editor = prefs.edit();
                for (String key : prefs.getAll().keySet()) if (key.startsWith("manifest_")) editor.remove(key);
                if (!editor.putString("manifestOwner", owner).putString("currentManifest", parsed.toString()).commit())
                    throw new IOException("离线清单迁移失败");
                checkEpoch(epoch); cached = parsed;
            } catch (Exception ignored) { }
            final JSONObject ready = cached;
            ui.post(() -> {
                if (destroyed || epoch != generation) return;
                loadingCache = false;
                if (ready != null) {
                    synchronized (lifecycle) { pending = ready; downloadedRevision = ready.optLong("revision", -1); }
                    applyPolicy(ready);
                    syncState = "已读取离线缓存，正在连接服务器";
                } else syncState = "等待首次同步";
                startCurrentOrNext(); sync();
            });
        });
    }
    private void cancelImageFeedback() {
        if (imageLoadingHint != null) ui.removeCallbacks(imageLoadingHint);
        if (imageDecodeTimeout != null) ui.removeCallbacks(imageDecodeTimeout);
        imageLoadingHint = imageDecodeTimeout = null;
    }
    private void stopMedia() { stopMedia(false); }
    private void stopMedia(boolean retainImage) {
        playbackSerial++; imageDecoder.cancel(); cancelImageFeedback();
        ui.removeCallbacks(advance); ui.removeCallbacks(watchdog); ui.removeCallbacks(videoProgress); playingVideo = false;
        try { video.stopPlayback(); } catch (Exception ignored) { }
        video.setVisibility(View.GONE);
        imageFrames.begin(retainImage);
    }
    private void releaseBitmap() { imageFrames.clear(); }
    private void showMessage(String text) {
        message.setText(text); message.setVisibility(View.VISIBLE);
    }
    private void startCurrentOrNext() {
        if (!authorized()) { showBindingState(); return; }
        videoRecoveries = 0;
        if (index >= 0 && pending == null) playIndex(); else nextItem();
    }
    private void nextItem() {
        if (!resumed || paused || dialogVisible || destroyed || !authorized()) return;
        if (pending != null) applyPendingManifest(true);
        JSONArray items = active == null ? null : active.optJSONArray("items");
        if (items == null || items.length() == 0) {
            showEmptyPlaylist();
            return;
        }
        long now = android.os.SystemClock.elapsedRealtime(), retry = Long.MAX_VALUE;
        for (int i = 0; i < items.length(); i++) {
            index = (index + 1) % items.length();
            JSONObject item = items.optJSONObject(index);
            long after = item == null ? 0 : failures.retryAt(failureKey(item));
            if (after <= now) { videoRecoveries = 0; playIndex(); return; }
            retry = Math.min(retry, after);
        }
        stopMedia(); currentItem = ""; playbackStatus = "error";
        showMessage("当前素材暂时无法播放，稍后自动重试\n\n遥控器确定键 · 打开控制面板");
        ui.postDelayed(advance, Math.max(1000L, Math.min(30000L, retry - now)));
    }
    private void applyPendingManifest() { applyPendingManifest(false); }
    private void applyPendingManifest(boolean retainImage) {
        if (pending == null) return;
        // A normal nonempty handover may keep its last decoded image until the next image is ready.
        // Explicit withdrawal always uses the complete stop path, even while paused or in a dialog.
        JSONArray incoming = pending.optJSONArray("items");
        boolean keep = retainImage && imageStillPublished(incoming) && authorized() && resumed && !paused && !dialogVisible;
        stopMedia(keep);
        synchronized (lifecycle) { active = pending; pending = null; index = -1; appliedRevision = active.optLong("revision", -1); }
        cleanupRevision = -1; cleanupState = "pending"; queueCleanup();
    }
    private boolean imageStillPublished(JSONArray incoming) {
        if (incoming == null || !imageFrames.hasImage()) return false;
        for (int i = 0; i < incoming.length(); i++) {
            JSONObject item = incoming.optJSONObject(i);
            if (item != null && "image".equals(item.optString("type")) && imageFrames.hasContent(failureKey(item))) return true;
        }
        return false;
    }
    private void showEmptyPlaylist() {
        stopMedia(); currentItem = ""; playbackStatus = "idle";
        showMessage("局域宣传屏\n\n" + (base.isEmpty() ? "请先填写连接设置" :
            "暂无播放内容\n请在电脑管理页面发布播放清单") + "\n\n遥控器确定键 · 打开控制面板");
    }
    private String failureKey(JSONObject item) { return item.optString("id", "") + "\n" + item.optString("sha256", ""); }
    private void playIndex() {
        if (!resumed || paused || dialogVisible || destroyed || !authorized()) return;
        try {
            JSONObject item = active.getJSONArray("items").getJSONObject(index);
            final boolean isImage = "image".equals(item.getString("type"));
            boolean retainImage = isImage && imageFrames.hasImage();
            stopMedia(retainImage);
            final int serial = playbackSerial;
            final long epoch = generation, imageTicket = imageFrames.ticket();
            final File file = new File(item.getString("local"));
            playingKey = failureKey(item);
            final String nextItemId = item.getString("id");
            if (!retainImage) currentItem = "";
            playbackStatus = "loading";
            if (!file.isFile()) throw new IOException("缓存缺失");
            if ("video".equals(item.getString("type"))) {
                message.setVisibility(View.GONE); video.setVisibility(View.VISIBLE);
                video.setOnCompletionListener(mp -> { if (serial == playbackSerial) { failures.succeeded(playingKey); nextItem(); } });
                video.setOnErrorListener((mp, what, extra) -> {
                    if (serial == playbackSerial) skipFailed("视频格式不受本机支持");
                    return true;
                });
                video.setOnPreparedListener(mp -> {
                    if (serial != playbackSerial || epoch != generation || !resumed || dialogVisible || paused || destroyed || !authorized()) return;
                    mp.setLooping(false); ui.removeCallbacks(watchdog);
                    long duration = mp.getDuration();
                    // The overall deadline is secondary to progress checks for a stalled decoder.
                    ui.postDelayed(watchdog, duration > 0 ? duration + 60000L : 6 * 3600000L);
                    playingVideo = true; currentItem = nextItemId; playbackStatus = "playing";
                    stallTracker.start(android.os.SystemClock.elapsedRealtime(), 0);
                    ui.removeCallbacks(videoProgress); ui.postDelayed(videoProgress, 5000L);
                    video.start();
                });
                video.setVideoURI(Uri.fromFile(file));
                ui.postDelayed(watchdog, 60000L);
                video.start(); root.requestFocus();
            } else {
                message.setVisibility(View.GONE);
                imageLoadingHint = () -> {
                    if (!destroyed && epoch == generation && serial == playbackSerial && authorized() && resumed && !paused && !dialogVisible &&
                        imageFrames.needsLoadingHint(imageTicket)) showMessage("正在读取图片…");
                };
                imageDecodeTimeout = () -> {
                    if (!destroyed && epoch == generation && serial == playbackSerial && authorized() && resumed && !paused && !dialogVisible)
                        skipFailed("图片读取超时");
                };
                ui.postDelayed(imageLoadingHint, 1000L);
                ui.postDelayed(imageDecodeTimeout, 30000L);
                final int maxW = Math.min(3840, Math.max(1920, getResources().getDisplayMetrics().widthPixels));
                final int maxH = Math.min(2160, Math.max(1080, getResources().getDisplayMetrics().heightPixels));
                final int duration = Math.max(1, Math.min(3600, item.optInt("duration", 10)));
                imageDecoder.submit(() -> decodeImage(file, maxW, maxH, serial, epoch, retainImage), new LatestDecoder.Receiver<Bitmap>() {
                    public void ready(Bitmap decoded) {
                        if (decoded == null) { failed(new IOException("图片无法解码")); return; }
                        if (destroyed || epoch != generation || serial != playbackSerial || !resumed || paused || dialogVisible || !authorized()) {
                            decoded.recycle(); return;
                        }
                        cancelImageFeedback();
                        try {
                            if (!imageFrames.present(imageTicket, decoded, failureKey(item))) return;
                            message.setVisibility(View.GONE); currentItem = nextItemId; playbackStatus = "playing";
                            failures.succeeded(playingKey);
                            ui.postDelayed(advance, duration * 1000L);
                        } catch (OutOfMemoryError failure) {
                            if (retainImage) retryImageWithoutRetention(); else skipFailed("此图片无法显示");
                        } catch (RuntimeException failure) { skipFailed("此图片无法显示"); }
                    }
                    public void failed(Throwable error) {
                        if (destroyed || epoch != generation || serial != playbackSerial || !resumed || paused || dialogVisible || !authorized()) return;
                        if (retainImage && error instanceof PlaybackSafety.NeedsSingleImageRetry) retryImageWithoutRetention();
                        else skipFailed("此图片无法播放");
                    }
                });
            }
        } catch (Exception | OutOfMemoryError e) { skipFailed("此素材无法播放"); }
    }
    private void retryImageWithoutRetention() {
        // Once cleared, playIndex sees no old image: this fallback cannot recur for the same attempt.
        stopMedia(); currentItem = "";
        playIndex();
    }
    private Bitmap decodeImage(File file, int maxW, int maxH, int serial, long epoch, boolean retainPrevious) throws Exception {
        try { return decodeImageAttempt(file, maxW, maxH, serial, epoch, retainPrevious); }
        catch (OutOfMemoryError failure) {
            // This also covers bounds decoding and options allocation before the pixel decode begins.
            if (retainPrevious) throw new PlaybackSafety.NeedsSingleImageRetry();
            throw failure;
        }
    }
    private Bitmap decodeImageAttempt(File file, int maxW, int maxH, int serial, long epoch, boolean retainPrevious) throws Exception {
        checkEpoch(epoch);
        if (serial != playbackSerial) throw new IOException("图片读取已取消");
        BitmapFactory.Options bounds = new BitmapFactory.Options(); bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(file.getAbsolutePath(), bounds);
        long budget = PlaybackSafety.singleImageBudget(Runtime.getRuntime().maxMemory());
        int sample = PlaybackSafety.imageSample(bounds.outWidth, bounds.outHeight, maxW, maxH, budget);
        return PlaybackSafety.decodeForHandoff(sample, retainPrevious, currentSample -> {
            checkEpoch(epoch);
            if (serial != playbackSerial) throw new IOException("图片读取已取消");
            BitmapFactory.Options options = new BitmapFactory.Options(); options.inSampleSize = currentSample;
            options.inPreferredConfig = Bitmap.Config.ARGB_8888;
            Bitmap decoded = BitmapFactory.decodeFile(file.getAbsolutePath(), options);
            if (decoded == null) throw new IOException("图片无法解码");
            if (!PlaybackSafety.imageFitsBudget(decoded.getAllocationByteCount(), budget)) {
                decoded.recycle(); throw new OutOfMemoryError("图片缓冲超过预算");
            }
            if (destroyed || epoch != generation || serial != playbackSerial || Thread.currentThread().isInterrupted()) {
                decoded.recycle(); throw new IOException("图片读取已取消");
            }
            return decoded;
        });
    }
    private void recoverVideo(String reason) {
        if (destroyed || !resumed || paused || dialogVisible) return;
        if (videoRecoveries < 1) { videoRecoveries++; syncState = reason + "，正在重试"; playIndex(); }
        else skipFailed(reason + "，本轮已跳过");
    }
    private void skipFailed(String error) {
        long now = android.os.SystemClock.elapsedRealtime();
        failures.failed(playingKey, now);
        if (failures.requestIntegrity(now)) integrityRequest++;
        stopMedia(); releaseBitmap(); currentItem = ""; playbackStatus = "error";
        showMessage(error + "，即将播放下一项\n\n遥控器确定键 · 打开控制面板");
        ui.postDelayed(advance, 2500L);
    }

    private CacheStore store() throws IOException {
        return new CacheStore(new File(getFilesDir(), "media"), CacheStore.DELETE, file -> {
            try { return android.system.OsConstants.S_ISLNK(android.system.Os.lstat(file.getPath()).st_mode); }
            catch (android.system.ErrnoException e) {
                if (e.errno == android.system.OsConstants.ENOENT) return false;
                throw new IOException("无法验证缓存路径", e);
            }
        });
    }
    private void checkEpoch(long epoch) throws IOException {
        if (destroyed || epoch != generation || Thread.currentThread().isInterrupted()) throw new IOException("操作已取消");
    }
    private void addProtected(Set<String> paths, JSONObject manifest) throws Exception {
        if (manifest == null) return;
        JSONArray items = manifest.getJSONArray("items");
        for (int i = 0; i < items.length(); i++) paths.add(items.getJSONObject(i).getString("local"));
    }
    private Set<String> protectedPaths() throws Exception {
        Set<String> paths = new HashSet<>();
        // Snapshot all three references atomically; never omit a manifest during UI handover.
        JSONObject p, a, r;
        synchronized (lifecycle) { p = pending; a = active; r = prepared; }
        addProtected(paths, p); addProtected(paths, a); addProtected(paths, r);
        return paths;
    }
    private void updateMetrics(CacheStore store) throws IOException {
        cacheBytes = store.bytes(); freeBytes = new StatFs(store.directory.getPath()).getAvailableBytes();
    }
    private void applyPolicy(JSONObject manifest) {
        JSONObject policy = manifest.optJSONObject("cachePolicy");
        long limit = policy == null ? DEFAULT_LIMIT : policy.optLong("maxBytes", DEFAULT_LIMIT);
        long reserve = policy == null ? DEFAULT_RESERVE : policy.optLong("reserveBytes", DEFAULT_RESERVE);
        cacheLimitBytes = Math.max(256L * 1024 * 1024, Math.min(32L * 1024 * 1024 * 1024, limit));
        cacheReserveBytes = Math.max(128L * 1024 * 1024, Math.min(4L * 1024 * 1024 * 1024, reserve));
    }
    private void capacity(CacheStore store, long extra) throws Exception {
        updateMetrics(store);
        try { CacheStore.requireCapacity(cacheBytes, extra, cacheLimitBytes, freeBytes, cacheReserveBytes); }
        catch (IOException e) { throw new NoSpace(e.getMessage()); }
    }
    private void queueCleanup() {
        if (destroyed || loadingCache || !authorized()) return;
        if (cleanupQueued) { cleanupRerun = true; return; }
        cleanupQueued = true;
        final long epoch = generation;
        network.execute(() -> {
            try { cleanup(epoch); }
            finally { ui.post(() -> {
                cleanupQueued = false;
                if (cleanupRerun) { cleanupRerun = false; queueCleanup(); }
            }); }
        });
    }
    private void cleanup(long epoch) {
        try {
            checkEpoch(epoch);
            CacheStore store = store();
            long requested, commandEpoch, targetRevision;
            JSONObject targetActive, targetPending, targetPrepared;
            synchronized (lifecycle) {
                requested = cleanupRequested; commandEpoch = cleanupCommandEpoch;
                targetRevision = appliedRevision; targetActive = active; targetPending = pending; targetPrepared = prepared;
            }
            cleanupState = "running"; cleanupError = "";
            Set<String> protectedFiles = new HashSet<>();
            addProtected(protectedFiles, targetActive); addProtected(protectedFiles, targetPending); addProtected(protectedFiles, targetPrepared);
            store.sweep(protectedFiles, () -> checkEpoch(epoch));
            checkEpoch(epoch); updateMetrics(store);
            // Active files are always protected, including when the requested quota is too small.
            try { CacheStore.requireCleanupCapacity(cacheBytes, cacheLimitBytes); }
            catch (IOException error) { throw new NoSpace(error.getMessage()); }
            checkEpoch(epoch);
            synchronized (lifecycle) {
                if (targetActive == active && targetPending == pending && targetPrepared == prepared && targetRevision == appliedRevision && commandEpoch == cleanupCommandEpoch) {
                    cleanupCompletedRequest = Math.max(cleanupCompletedRequest, requested);
                    cleanupRevision = targetRevision;
                    cleanupState = downloadedRevision == appliedRevision && cleanupRevision == appliedRevision ? "complete" : "pending";
                } else {
                    cleanupRevision = -1; cleanupState = "pending";
                    ui.post(() -> { if (!destroyed && epoch == generation) queueCleanup(); });
                }
            }
        } catch (Exception e) {
            if (destroyed || epoch != generation) return;
            cleanupState = e instanceof NoSpace ? "blocked" : "error";
            cleanupError = e.getMessage() == null ? "缓存清理未完成" : e.getMessage();
        }
    }
    private void sync() {
        if (syncing || loadingCache || base.isEmpty() || token.isEmpty() || destroyed || !authorized()) return;
        syncing = true;
        final String server = base, credential = token, owner = cacheKey();
        final long epoch = generation;
        final int checkRequest = integrityRequest;
        final boolean verifyLocal = integrityChecked != checkRequest;
        network.execute(() -> {
            String result = "网络暂不可用 · 使用已缓存清单";
            boolean succeeded = false;
            JSONObject candidate = null;
            try {
                checkEpoch(epoch);
                JSONObject previous = pending != null ? pending : active;
                HttpURLConnection c = connection(server + "/api/player/manifest", credential, epoch);
                String raw;
                try { requireOK(c, epoch); raw = readText(c.getInputStream(), 4 * 1024 * 1024, epoch, 120000L); }
                finally { close(c); }
                checkEpoch(epoch);
                JSONObject manifest = new JSONObject(raw);
                if (manifest.optLong("bindingVersion", -1) != binding.version) {
                    lockBinding("device_pending", epoch); throw new HttpFailure("设备绑定已变化，正在重新确认");
                }
                JSONArray items = manifest.getJSONArray("items");
                if (items.length() > 5000 || manifest.optLong("revision", -1) < 0) throw new IOException("清单无效");
                long revision = manifest.getLong("revision");
                applyPolicy(manifest);
                CacheStore cache = store();
                boolean same = previous != null && previous.optLong("revision", -1) == revision;
                if (same) {
                    JSONArray old = previous.getJSONArray("items");
                    for (int i = 0; i < old.length(); i++) {
                        JSONObject item = old.getJSONObject(i);
                        File file = cache.requireSafe(new File(item.getString("local")));
                        if (!valid(file, item.optString("sha256", ""), item.optLong("cachedBytes", -1), verifyLocal, epoch)) {
                            same = false; break;
                        }
                    }
                }
                if (!same) {
                    Map<String, File> reusable = new HashMap<>();
                    Map<String, JSONObject> unique = new HashMap<>();
                    long additional = 0;
                    for (int i = 0; i < items.length(); i++) {
                        checkEpoch(epoch);
                        JSONObject item = items.getJSONObject(i);
                        String type = item.getString("type");
                        if (!type.equals("image") && !type.equals("video")) throw new IOException("未知素材类型");
                        item.getString("id");
                        URL origin = new URL(server), target = new URL(origin, item.getString("url"));
                        if (!target.getProtocol().equals(origin.getProtocol()) || !target.getHost().equalsIgnoreCase(origin.getHost()) ||
                            target.getPort() != origin.getPort() || !target.getPath().startsWith("/media/"))
                            throw new IOException("素材地址不属于当前服务器");
                        String checksum = item.optString("sha256", "").toLowerCase(java.util.Locale.ROOT);
                        if (!checksum.matches("[a-f0-9]{64}")) throw new IOException("素材校验值无效");
                        long bytes = item.optLong("bytes", -1);
                        if (bytes > MAX_MEDIA_BYTES) throw new IOException("单个素材超过 1GB");
                        String key = sha256(target.toString() + "\n" + credential + "\n" + checksum);
                        item.put("downloadUrl", target.toString()); item.put("cacheId", key);
                        if (unique.containsKey(key)) continue;
                        unique.put(key, item);
                        File existing = findReusable(cache, key, checksum, bytes, previous, epoch);
                        if (existing != null) reusable.put(key, existing);
                        else if (bytes > 0) additional += bytes;
                    }
                    // Sweep only files outside the last fully persisted/active/pending manifests.
                    // It is safe even if this attempt later fails: old playback is still protected.
                    Set<String> retain = protectedPaths();
                    for (File file : reusable.values()) retain.add(file.getAbsolutePath());
                    cache.sweep(retain, () -> checkEpoch(epoch));
                    updateMetrics(cache);
                    try { CacheStore.requireTransitionCapacity(cacheBytes, additional, cacheLimitBytes, freeBytes,
                        cacheReserveBytes, reusable.size() != unique.size()); }
                    catch (IOException error) { throw new NoSpace(error.getMessage()); }
                    for (int i = 0; i < items.length(); i++) {
                        checkEpoch(epoch);
                        JSONObject item = items.getJSONObject(i);
                        String key = item.getString("cacheId");
                        File local = reusable.get(key);
                        if (local == null) {
                            local = download(cache, item.getString("downloadUrl"), credential, key,
                                item.getString("sha256"), item.optLong("bytes", -1), epoch);
                            reusable.put(key, local);
                        }
                        item.put("local", local.getAbsolutePath()); item.put("cachedBytes", local.length());
                        item.remove("downloadUrl"); item.remove("cacheId");
                    }
                    checkEpoch(epoch);
                    // Save one owner-scoped complete manifest off the UI thread, before any handover.
                    SharedPreferences.Editor editor = prefs.edit();
                    for (String key : prefs.getAll().keySet()) if (key.startsWith("manifest_")) editor.remove(key);
                    if (!editor.putString("manifestOwner", owner).putString("currentManifest", manifest.toString()).commit())
                        throw new IOException("清单保存失败");
                    checkEpoch(epoch);
                    synchronized (lifecycle) { prepared = manifest; downloadedRevision = revision; } cleanupState = "pending"; cleanupError = "";
                    candidate = manifest;
                } else {
                    downloadedRevision = revision;
                    cleanup(epoch);
                }
                result = "连接正常 · 清单版本 " + revision;
                succeeded = true;
            } catch (Exception e) {
                if (epoch == generation && !destroyed) {
                    cleanupState = e instanceof NoSpace ? "blocked" : "error";
                    cleanupError = e instanceof NoSpace || e instanceof HttpFailure ? e.getMessage() : "素材同步未完成，保留原清单";
                    result = cleanupError + " · 使用已缓存清单";
                }
            } finally {
                try { if (epoch == generation && !destroyed) updateMetrics(store()); } catch (Exception ignored) { }
            }
            final JSONObject ready = candidate; final String status = result; final boolean ok = succeeded;
            ui.post(() -> {
                syncing = false;
                if (destroyed || epoch != generation) return;
                if (ok) integrityChecked = checkRequest;
                syncState = status;
                if (ready != null) {
                    synchronized (lifecycle) { pending = ready; prepared = null; }
                    refreshSeconds = Math.max(10, Math.min(300, ready.optInt("refreshSeconds", 30)));
                    if (ready.optJSONArray("items").length() == 0) {
                        // Explicit withdrawal must not wait for a long video, pause or an open control dialog.
                        applyPendingManifest(); showEmptyPlaylist();
                    } else if (active == null || active.optJSONArray("items") == null || active.optJSONArray("items").length() == 0)
                        nextItem();
                }
                if (cleanupRequested > cleanupCompletedRequest) queueCleanup();
                if (active == null && pending == null && !dialogVisible)
                    showMessage("局域宣传屏\n\n" + syncState + "\n\n遥控器确定键 · 检查连接设置");
            });
        });
    }
    private boolean valid(File file, String checksum, long expected, boolean hash, long epoch) throws Exception {
        checkEpoch(epoch);
        return file.isFile() && file.length() > 0 && (expected <= 0 || file.length() == expected) &&
            (!hash || (!checksum.isEmpty() && checksum.equalsIgnoreCase(fileHash(file, epoch))));
    }
    private File findReusable(CacheStore cache, String key, String checksum, long bytes, JSONObject previous, long epoch) throws Exception {
        for (String suffix : new String[]{".media", "-repair0.media", "-repair1.media"}) {
            File file = cache.file(key + suffix);
            if (valid(file, checksum, bytes, true, epoch)) return file;
        }
        if (previous != null) {
            JSONArray items = previous.getJSONArray("items");
            for (int i = 0; i < items.length(); i++) {
                JSONObject item = items.getJSONObject(i);
                if (!checksum.equalsIgnoreCase(item.optString("sha256", ""))) continue;
                File file = cache.requireSafe(new File(item.getString("local")));
                if (valid(file, checksum, bytes, true, epoch)) return file;
            }
        }
        return null;
    }
    private static final class BindingStorageFailure extends IOException { }
    private static final class HttpFailure extends IOException { HttpFailure(String msg) { super(msg); } }
    private static final class NoSpace extends IOException { NoSpace(String msg) { super(msg); } }
    private HttpURLConnection connection(String url, String credential, long epoch) throws IOException {
        checkEpoch(epoch);
        HttpURLConnection c = (HttpURLConnection)new URL(url).openConnection();
        c.setConnectTimeout(10000); c.setReadTimeout(15000); c.setInstanceFollowRedirects(false);
        c.setRequestProperty("Authorization", "Bearer " + credential);
        c.setRequestProperty("X-LanScreen-Device-Id", deviceId);
        c.setRequestProperty("X-LanScreen-Device-Token", binding.credential);
        c.setRequestProperty("Accept-Encoding", "identity");
        c.setRequestProperty("User-Agent", "LanScreen-TV/" + VERSION);
        connections.add(c);
        if (destroyed || epoch != generation) { close(c); throw new IOException("操作已取消"); }
        return c;
    }
    private void close(HttpURLConnection c) { connections.remove(c); c.disconnect(); }
    private void cancelConnections() {
        HttpURLConnection[] snapshot;
        synchronized (connections) { snapshot = connections.toArray(new HttpURLConnection[0]); }
        for (HttpURLConnection c : snapshot) close(c);
    }
    private void requireOK(HttpURLConnection c, long epoch) throws IOException {
        int status = c.getResponseCode();
        if (status == 200) return;
        String code = status == 426 ? "upgrade_required" : "", explanation = "服务器返回 " + status;
        InputStream failure = c.getErrorStream();
        if (failure != null) try {
            JSONObject body = new JSONObject(readText(failure, 8192, epoch, 15000L));
            code = body.optString("code", code); explanation = body.optString("error", explanation);
        } catch (Exception ignored) { }
        code = BindingSession.errorCode(status, code);
        if (BindingSession.isLockCode(code)) lockBinding(code, epoch);
        if (status == 401 || "key_invalid".equals(code)) explanation = "播放口令无效，请在连接设置中更新口令";
        throw new HttpFailure(explanation);
    }
    private File download(CacheStore cache, String url, String credential, String key, String checksum, long expected, long epoch) throws Exception {
        HttpURLConnection c = connection(url, credential, epoch);
        try {
            requireOK(c, epoch); long length = c.getContentLength();
            if (length > MAX_MEDIA_BYTES || (expected > 0 && length >= 0 && expected != length)) throw new IOException("素材大小不符");
            File staging = cache.file(key + ".part");
            Set<String> protect = protectedPaths();
            cache.removeUnprotected(staging, protect);
            capacity(cache, Math.max(length, Math.max(expected, 0)));
            long start = android.os.SystemClock.elapsedRealtime();
            long count = 0, startingBytes = cacheBytes;
            byte[] buffer = new byte[128 * 1024];
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            try (InputStream in = c.getInputStream(); FileOutputStream out = new FileOutputStream(staging)) {
                int n;
                while ((n = in.read(buffer)) != -1) {
                    checkEpoch(epoch);
                    if (android.os.SystemClock.elapsedRealtime() - start > 30 * 60 * 1000L) throw new IOException("下载超时");
                    if (count + n > MAX_MEDIA_BYTES) throw new IOException("单个素材超过 1GB");
                    long free = new StatFs(cache.directory.getPath()).getAvailableBytes();
                    try { CacheStore.requireCapacity(startingBytes + count, n, cacheLimitBytes, free, cacheReserveBytes); }
                    catch (IOException e) { throw new NoSpace(e.getMessage()); }
                    out.write(buffer, 0, n); digest.update(buffer, 0, n); count += n;
                }
                out.getFD().sync();
            }
            checkEpoch(epoch);
            if (count == 0 || (length >= 0 && count != length) || (expected > 0 && count != expected)) throw new IOException("素材下载不完整");
            if (!checksum.equals(hex(digest.digest()))) throw new IOException("素材校验失败");
            // At most three deterministic slots per asset. A corrupt active slot remains until handover.
            protect = protectedPaths();
            File destination = cache.repairDestination(key, protect);
            cache.removeUnprotected(destination, protect);
            checkEpoch(epoch);
            if (!staging.renameTo(destination)) throw new IOException("无法提交缓存文件");
            updateMetrics(cache); return destination;
        } finally { close(c); }
    }
    private void heartbeat(String server, String credential, long epoch) {
        HttpURLConnection c = null;
        try {
            c = connection(server + "/api/player/heartbeat", credential, epoch); c.setRequestMethod("POST");
            c.setDoOutput(true); c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            JSONObject body = new JSONObject().put("deviceId", deviceId).put("name", deviceName)
                .put("version", VERSION).put("protocolVersion", 3).put("bindingVersion", binding.version).put("status", playbackStatus).put("currentItem", currentItem)
                .put("appliedRevision", appliedRevision).put("downloadedRevision", downloadedRevision).put("cleanupRevision", cleanupRevision)
                .put("cacheBytes", cacheBytes).put("freeBytes", freeBytes).put("cacheLimitBytes", cacheLimitBytes)
                .put("cacheReserveBytes", cacheReserveBytes).put("cleanupState", cleanupState).put("cleanupError", cleanupError)
                .put("cleanupCompletedRequest", cleanupCompletedRequest);
            byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
            c.setFixedLengthStreamingMode(bytes.length);
            try (java.io.OutputStream out = c.getOutputStream()) { out.write(bytes); }
            requireOK(c, epoch);
            JSONObject reply = new JSONObject(readText(c.getInputStream(), 65536, epoch, 60000L));
            if (reply.optLong("bindingVersion", -1) != binding.version) {
                lockBinding("device_pending", epoch); throw new HttpFailure("设备绑定已变化，正在重新确认");
            }
            long requested = Math.max(0, reply.optLong("cleanupRequest", 0));
            checkEpoch(epoch);
            ui.post(() -> {
                if (destroyed || epoch != generation) return;
                boolean needsCleanup;
                synchronized (lifecycle) {
                    // A restored server database may restart command numbering for this device.
                    if (requested < cleanupCompletedRequest || requested < cleanupRequested) {
                        cleanupCommandEpoch++; cleanupCompletedRequest = 0; cleanupRequested = requested;
                    } else cleanupRequested = Math.max(cleanupRequested, requested);
                    needsCleanup = cleanupRequested > cleanupCompletedRequest;
                }
                if (needsCleanup) {
                    if (!"running".equals(cleanupState)) cleanupState = "pending";
                    queueCleanup();
                }
            });
        } catch (Exception ignored) {} finally { if (c != null) close(c); }
    }
    private void sendHeartbeat() {
        if (heartbeatBusy || base.isEmpty() || token.isEmpty() || destroyed || !authorized()) return;
        heartbeatBusy = true;
        final String server = base, credential = token; final long epoch = generation;
        heartbeatNetwork.execute(() -> {
            heartbeat(server, credential, epoch);
            ui.post(() -> { heartbeatBusy = false; });
        });
    }
    private String readText(InputStream in, int max, long epoch, long timeout) throws IOException {
        long start = android.os.SystemClock.elapsedRealtime();
        try (InputStream stream = in; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] b = new byte[8192]; int n;
            while ((n = stream.read(b)) != -1) {
                checkEpoch(epoch);
                if (android.os.SystemClock.elapsedRealtime() - start > timeout) throw new IOException("请求超时");
                if (out.size() + n > max) throw new IOException("响应过大"); out.write(b, 0, n);
            }
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        }
    }
    private static String sha256(String value) {
        try { return hex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8))); }
        catch (Exception e) { throw new IllegalStateException(e); }
    }
    private String fileHash(File file, long epoch) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256"); byte[] buffer = new byte[128 * 1024];
        try (InputStream in = new FileInputStream(file)) {
            int n; while ((n = in.read(buffer)) != -1) { checkEpoch(epoch); digest.update(buffer, 0, n); }
        }
        return hex(digest.digest());
    }
    private static String hex(byte[] data) {
        StringBuilder out = new StringBuilder();
        for (byte b : data) out.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
        return out.toString();
    }

    @Override public boolean onKeyDown(int key, KeyEvent event) {
        if (dialogVisible) return super.onKeyDown(key, event);
        if (key == KeyEvent.KEYCODE_DPAD_CENTER || key == KeyEvent.KEYCODE_ENTER ||
            key == KeyEvent.KEYCODE_MENU || key == KeyEvent.KEYCODE_BACK) {
            showControls(); return true;
        }
        if (key == KeyEvent.KEYCODE_DPAD_RIGHT || key == KeyEvent.KEYCODE_MEDIA_NEXT) { nextItem(); return true; }
        if (key == KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE && !authorized()) { showBindingState(); return true; }
        if (key == KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE) { paused = !paused; if (paused) { playbackStatus = "paused"; stopMedia(); showMessage("已暂停\n\n按确定键打开控制面板"); } else startCurrentOrNext(); return true; }
        return super.onKeyDown(key, event);
    }
    private LinearLayout panel() {
        LinearLayout box = new LinearLayout(this); box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(28), dp(14), dp(28), dp(14)); return box;
    }
    private TextView label(String text, int size, int color) {
        TextView t = new TextView(this); t.setText(text); t.setTextColor(color); t.setTextSize(size);
        t.setPadding(0, dp(8), 0, dp(8)); return t;
    }
    private Button button(String title, View.OnClickListener listener) {
        Button b = new Button(this); b.setText(title); b.setTextSize(18); b.setAllCaps(false);
        b.setTextColor(INK); b.setFocusable(true); b.setMinHeight(dp(48));
        b.setOnFocusChangeListener((v, focused) -> {
            GradientDrawable bg = new GradientDrawable(); bg.setCornerRadius(dp(8));
            bg.setColor(focused ? Color.rgb(19, 89, 91) : Color.rgb(25, 39, 58));
            bg.setStroke(dp(focused ? 3 : 1), focused ? TEAL : Color.rgb(57, 76, 99)); v.setBackground(bg);
        });
        b.setOnClickListener(listener); return b;
    }
    private void sizeDialog(AlertDialog dialog) {
        if (dialog.getWindow() != null) dialog.getWindow().setLayout(
            Math.min(getResources().getDisplayMetrics().widthPixels - dp(80), dp(800)), -2);
    }
    private void showControls() {
        if (dialogVisible || destroyed) return;
        dialogVisible = true; stopMedia(); playbackStatus = paused ? "paused" : "idle";
        LinearLayout box = panel(); box.addView(label("局域宣传屏", 28, INK));
        box.addView(label(deviceName + "  ·  v" + VERSION + "\n" + syncState + "\n" +
            (base.isEmpty() ? "服务器尚未设置" : base), 16, MUTED));
        Button resume = button(!authorized() ? "返回" : paused ? "继续播放" : "返回播放", v -> { paused = false; controls.dismiss(); });
        box.addView(resume);
        if (!authorized()) box.addView(label("设备编号：" + deviceId + (binding.version > 0 ? "\n接入批次：" + binding.version : "") + "\n" +
            (binding.canReapply() ? "此电视需要重新申请绑定" : "upgrade_required".equals(binding.state) ? "需要升级电视应用" :
            "storage_error".equals(binding.state) ? "授权状态保存失败，已停止播放" : "请在电脑后台批准绑定申请"), 14, MUTED));
        if (binding.canReapply()) box.addView(button("重新申请绑定", v -> {
            controls.setOnDismissListener(null); controls.dismiss(); dialogVisible = false;
            dialogVisible = true;
            AlertDialog confirm = new AlertDialog.Builder(this).setTitle("重新申请绑定")
                .setMessage("将生成新的设备凭证，并等待电脑后台管理员重新批准。不会删除本地素材文件。")
                .setPositiveButton("提交申请", null).setNegativeButton("取消", null).create();
            confirm.setOnDismissListener(d -> { bindingGate.cancel(confirm); dialogVisible = false; startCurrentOrNext(); });
            confirm.show(); sizeDialog(confirm);
            confirm.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(clicked -> {
                if (!binding.canReapply()) { confirm.dismiss(); return; }
                queueBindingAction(confirm, "提交申请", () -> {
                    generation++; cancelConnections(); paused = false; loadBinding(true); confirm.dismiss();
                });
            });
        }));
        box.addView(button("下一项", v -> {
            paused = false;
            JSONArray items = active == null ? null : active.optJSONArray("items");
            index = items == null || items.length() == 0 ? -1 : (index + 1) % items.length();
            controls.dismiss();
        }));
        box.addView(button(paused ? "保持暂停" : "暂停播放", v -> { paused = true; controls.dismiss(); }));
        box.addView(button("连接设置", v -> {
            controls.setOnDismissListener(null); controls.dismiss(); dialogVisible = false; showSettings();
        }));
        box.addView(button("退出应用", v -> { controls.setOnDismissListener(null); controls.dismiss(); finish(); }));
        controls = new AlertDialog.Builder(this).setView(box).create();
        controls.setOnDismissListener(d -> {
            dialogVisible = false;
            if (!authorized()) showBindingState();
            else if (paused) { playbackStatus = "paused"; showMessage("已暂停\n\n遥控器确定键 · 继续播放"); }
            else startCurrentOrNext();
            root.requestFocus(); immerse();
        });
        controls.show(); sizeDialog(controls); resume.requestFocus();
    }
    private EditText input(LinearLayout box, String title, String value, boolean secret) {
        box.addView(label(title, 16, MUTED));
        EditText e = new EditText(this); e.setText(value); e.setTextSize(19); e.setTextColor(INK);
        e.setSingleLine(true); e.setSelectAllOnFocus(true);
        e.setInputType(InputType.TYPE_CLASS_TEXT | (secret ? InputType.TYPE_TEXT_VARIATION_PASSWORD : InputType.TYPE_TEXT_VARIATION_URI));
        e.setPadding(dp(12), dp(8), dp(12), dp(8)); box.addView(e, new LinearLayout.LayoutParams(-1, dp(52)));
        return e;
    }
    private void queueBindingAction(AlertDialog dialog, String buttonLabel, Runnable action, View... inputs) {
        final long epoch = generation;
        final BindingSession requested = binding;
        final Button submit = dialog.getButton(AlertDialog.BUTTON_POSITIVE);
        bindingGate.runUi(dialog, () -> !destroyed && resumed && dialogVisible && dialog.isShowing() &&
            epoch == generation && binding == requested, () -> {
            submit.setEnabled(false); submit.setText("正在等待，请稍候…");
            for (View input : inputs) input.setEnabled(false);
        }, action, timedOut -> {
            if (destroyed || !dialog.isShowing()) return;
            submit.setEnabled(true); submit.setText(buttonLabel);
            for (View input : inputs) input.setEnabled(true);
            if (resumed) android.widget.Toast.makeText(this, timedOut ? "等待超时，未提交，请重试" :
                "设备状态已变化，未提交，请重新操作", android.widget.Toast.LENGTH_LONG).show();
        });
    }
    private void showSettings() {
        if (dialogVisible || destroyed || !resumed) return;
        dialogVisible = true; stopMedia();
        LinearLayout box = panel();
        box.addView(label("连接公司宣传服务器", 26, INK));
        box.addView(label("电视与服务器须在同一局域网。播放口令在电脑管理页面查看。", 15, MUTED));
        final EditText address = input(box, "服务器地址", base.isEmpty() ? "http://192.168.1.100:8765" : base, false);
        final EditText credential = input(box, "播放口令", token, true);
        final EditText name = input(box, "屏幕名称", deviceName, false);
        box.addView(label("图片按设置时长切换，视频播完切换。断网继续播放完整缓存。", 14, MUTED));
        ScrollView scroll = new ScrollView(this); scroll.addView(box);
        final AlertDialog dialog = new AlertDialog.Builder(this).setView(scroll).setPositiveButton("保存并连接", null)
            .setNegativeButton("返回", null).create();
        settings = dialog;
        dialog.setOnDismissListener(d -> { bindingGate.cancel(dialog); dialogVisible = false; startCurrentOrNext(); root.requestFocus(); immerse(); });
        dialog.show(); sizeDialog(dialog);
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            String nextBase = address.getText().toString().trim();
            while (nextBase.endsWith("/")) nextBase = nextBase.substring(0, nextBase.length() - 1);
            String nextToken = credential.getText().toString().trim();
            try {
                URL parsed = new URL(nextBase);
                if ((!parsed.getProtocol().equals("http") && !parsed.getProtocol().equals("https")) ||
                    parsed.getHost().isEmpty() || parsed.getUserInfo() != null || parsed.getQuery() != null ||
                    parsed.getRef() != null || !parsed.getPath().isEmpty()) throw new Exception();
            } catch (Exception e) { address.setError("请输入 http://服务器IP:8765，不要附加路径"); address.requestFocus(); return; }
            if (nextToken.isEmpty() || nextToken.length() > 512 || nextToken.contains("\n") || nextToken.contains("\r")) {
                credential.setError("请输入有效的播放口令"); credential.requestFocus(); return;
            }
            final String savedBase = nextBase, savedToken = nextToken, savedName = name.getText().toString().trim();
            queueBindingAction(dialog, "保存并连接", () -> {
                generation++; cancelConnections(); base = savedBase; token = savedToken;
                deviceName = savedName.isEmpty() ? android.os.Build.MODEL : savedName;
                prefs.edit().putString("server", base).putString("token", token).putString("deviceName", deviceName).apply();
                paused = false; loadBinding(false); dialog.dismiss();
                ui.removeCallbacks(poll); ui.post(poll);
            }, address, credential, name);
        });
        address.requestFocus();
    }
}
