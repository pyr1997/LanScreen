package com.lanscreen.player;

/** Immutable authorization state; network failures never grant a binding or rotate credentials. */
final class BindingSession {
    final String credential, state;
    final long version;
    BindingSession(String credential, String state, long version) {
        this.credential = credential; this.state = state; this.version = version;
    }
    boolean canPlay() { return "active".equals(state) && version > 0 && validCredential(credential); }
    boolean canRegister() { return validCredential(credential) && ("unregistered".equals(state) || "pending".equals(state) || "active".equals(state)); }
    boolean canReapply() { return "device_revoked".equals(state) || "device_unknown".equals(state) || "device_token_invalid".equals(state); }
    BindingSession lock(String code) { return new BindingSession(credential, code, version); }
    static boolean validCredential(String credential) { return credential != null && credential.matches("[a-f0-9]{64}"); }
    static boolean isLockCode(String code) {
        return "device_revoked".equals(code) || "device_pending".equals(code) || "device_unknown".equals(code) ||
            "device_token_invalid".equals(code) || "upgrade_required".equals(code);
    }
    static String errorCode(int status, String code) {
        if (status == 403) return isLockCode(code) ? code : "device_token_invalid";
        if (status == 426) return "upgrade_required";
        return code;
    }
    static String restoredState(String state) { return "storage_error".equals(state) ? "unregistered" : state; }
    static String stateForError(String code) { return "device_pending".equals(code) ? "pending" : code; }
}
