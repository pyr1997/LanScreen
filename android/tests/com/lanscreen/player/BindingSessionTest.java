package com.lanscreen.player;
public final class BindingSessionTest {
    private static int assertions;
    private static void check(boolean result) { assertions++; if (!result) throw new AssertionError("binding assertion " + assertions); }
    public static void main(String[] args) {
        String credential = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        BindingSession first = new BindingSession(credential,"unregistered",0);
        check(!first.canPlay()); check(first.canRegister()); check(!first.canReapply());
        BindingSession pending = new BindingSession(credential,"pending",1);
        check(!pending.canPlay()); check(pending.canRegister()); check(!pending.canReapply());
        BindingSession active = new BindingSession(credential,"active",1);
        check(active.canPlay()); check(active.canRegister()); check(!active.canReapply());
        for(String code:new String[]{"device_revoked","device_unknown","device_token_invalid","upgrade_required","device_pending"}) {
            check(BindingSession.isLockCode(code));
            BindingSession locked = active.lock(BindingSession.stateForError(code));
            check(!locked.canPlay()); check(locked.credential.equals(credential)); check(locked.version==1);
            check(locked.canRegister()==code.equals("device_pending"));
            check(locked.canReapply()==(code.equals("device_revoked")||code.equals("device_unknown")||code.equals("device_token_invalid")));
        }
        check(!BindingSession.isLockCode("key_invalid")); check(!BindingSession.isLockCode("network_timeout"));
        check(!new BindingSession(credential,"storage_error",1).canPlay());
        check(!new BindingSession(credential,"storage_error",1).canRegister());
        check(!new BindingSession(credential,"active",0).canPlay());
        for(String bad:new String[]{"", "a", credential.toUpperCase(), " "+credential}) {
            check(!BindingSession.validCredential(bad)); check(!new BindingSession(bad,"active",1).canPlay());
        }
        check(BindingSession.validCredential(credential));
        check(!BindingSession.validCredential(null));
        // Loading a persisted revocation must remain revoked; neither normal restart nor network retry mutates it.
        BindingSession restored = new BindingSession(active.credential,"device_revoked",active.version);
        check(!restored.canRegister()); check(!restored.canPlay()); check(restored.canReapply());
        check(new BindingSession(credential,"active",2).version != active.version);
        BindingSession storageRecovered = new BindingSession(credential,BindingSession.restoredState("storage_error"),1);
        check(!storageRecovered.canPlay()); check(storageRecovered.canRegister());
        check(BindingSession.restoredState("device_revoked").equals("device_revoked"));
        check(!new BindingSession(credential,BindingSession.restoredState("device_revoked"),1).canRegister());
        check(!new BindingSession(credential,"unexpected_server_state",1).canPlay());
        check(!new BindingSession(credential,"unexpected_server_state",1).canRegister());
        check(BindingSession.errorCode(403, "").equals("device_token_invalid"));
        check(BindingSession.errorCode(403, null).equals("device_token_invalid"));
        check(BindingSession.errorCode(403, "unknown_future_code").equals("device_token_invalid"));
        check(BindingSession.errorCode(403, "key_invalid").equals("device_token_invalid"));
        check(BindingSession.errorCode(403, "device_revoked").equals("device_revoked"));
        check(BindingSession.errorCode(403, "device_pending").equals("device_pending"));
        check(BindingSession.errorCode(401, "key_invalid").equals("key_invalid"));
        check(BindingSession.errorCode(426, "").equals("upgrade_required"));
        System.out.println("RESULT " + assertions + " binding assertions passed; pure Java, no network or file deletion.");
    }
}
