#!/usr/bin/env bash
# Framework-only Android build. Requires JDK 17+ and Android SDK platform 28/build-tools 34.0.0.
# Every run has its own directory. Existing build artifacts and signing keys are preserved.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
if [[ -z "$SDK_ROOT" ]]; then
  echo '请先设置 ANDROID_SDK_ROOT，指向 Android SDK。' >&2; exit 1
fi
JDK_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}"
BUILD_TOOLS_VERSION="${BUILD_TOOLS_VERSION:-34.0.0}"
PLATFORM_VERSION="${PLATFORM_VERSION:-28}"
TOOLS="$SDK_ROOT/build-tools/$BUILD_TOOLS_VERSION"
PLATFORM="$SDK_ROOT/platforms/android-$PLATFORM_VERSION/android.jar"
BUILD_ROOT="${BUILD_ROOT:-$PROJECT_DIR/build}"
RUN="$BUILD_ROOT/run-$(date +%Y%m%d-%H%M%S)-$$"
OUT_DIR="${APK_OUTPUT_DIR:-$PROJECT_DIR/../apk}"
mkdir -p "$RUN/gen" "$RUN/classes" "$RUN/dex" "$OUT_DIR"
export JAVA_HOME="${JAVA_HOME:-}"
if [[ -n "$JAVA_HOME" ]]; then export PATH="$JAVA_HOME/bin:$PATH"; fi
"${JDK_BIN}javac" -version
[[ -f "$PLATFORM" ]] || { echo "缺少 Android API $PLATFORM_VERSION 平台。" >&2; exit 1; }
[[ -x "$TOOLS/aapt" ]] || { echo "缺少 Android Build Tools $BUILD_TOOLS_VERSION。" >&2; exit 1; }
"$TOOLS/aapt" package -f -M "$PROJECT_DIR/AndroidManifest.xml" -S "$PROJECT_DIR/res" \
    -I "$PLATFORM" -J "$RUN/gen" -F "$RUN/resources.ap_"
while IFS= read -r -d '' source; do printf '"%s"\n' "$source"; done \
  < <(find "$PROJECT_DIR/src" "$RUN/gen" -name '*.java' -print0) > "$RUN/sources.txt"
"${JDK_BIN}javac" -g:none -parameters -encoding UTF-8 --release 8 -classpath "$PLATFORM" \
    -d "$RUN/classes" "@$RUN/sources.txt"
class_files=()
while IFS= read -r -d '' class_file; do class_files+=("$class_file"); done \
  < <(find "$RUN/classes" -name '*.class' -print0)
"$TOOLS/d8" --min-api 23 --lib "$PLATFORM" --output "$RUN/dex" "${class_files[@]}"
cp "$RUN/resources.ap_" "$RUN/unsigned.apk"
zip -q -j "$RUN/unsigned.apk" "$RUN/dex/"*.dex
"$TOOLS/zipalign" -p -f 4 "$RUN/unsigned.apk" "$RUN/aligned.apk"
KEYSTORE="${LANSCREEN_KEYSTORE:-$BUILD_ROOT/lanscreen-demo.jks}"
KEY_ALIAS="${LANSCREEN_KEY_ALIAS:-lanscreen-demo}"
if [[ -z "${LANSCREEN_KEYSTORE:-}" ]]; then
  export LANSCREEN_KEY_PASSWORD=android
  if [[ ! -f "$KEYSTORE" ]]; then
    "${JDK_BIN}keytool" -genkeypair -keystore "$KEYSTORE" -storepass:env LANSCREEN_KEY_PASSWORD \
      -keypass:env LANSCREEN_KEY_PASSWORD -alias "$KEY_ALIAS" -keyalg RSA -keysize 2048 \
      -validity 10000 -dname 'CN=LanScreen Local Demo, OU=LAN, O=LanScreen, C=CN' -noprompt
  fi
elif [[ -z "${LANSCREEN_KEY_PASSWORD:-}" ]]; then
  echo '设置 LANSCREEN_KEY_PASSWORD 后再使用自有签名证书。' >&2; exit 1
fi
APK="$OUT_DIR/LanScreen-TV-1.3.2.apk"
if [[ -e "$APK" ]]; then
  echo "输出文件已存在，请设置新的 APK_OUTPUT_DIR 后重新构建，保留已有版本：$APK" >&2; exit 1
fi
"$TOOLS/apksigner" sign --ks "$KEYSTORE" --ks-key-alias "$KEY_ALIAS" \
    --ks-pass env:LANSCREEN_KEY_PASSWORD --key-pass env:LANSCREEN_KEY_PASSWORD \
    --v1-signing-enabled true --v2-signing-enabled true --out "$APK" "$RUN/aligned.apk"
"$TOOLS/apksigner" verify --verbose --print-certs "$APK" > "$OUT_DIR/apk-verification.txt"
"$TOOLS/aapt" dump badging "$APK" >> "$OUT_DIR/apk-verification.txt"
"$TOOLS/zipalign" -c -v 4 "$APK" >> "$OUT_DIR/apk-verification.txt"
if command -v shasum >/dev/null; then
  (cd "$OUT_DIR" && shasum -a 256 'LanScreen-TV-1.3.2.apk') > "$OUT_DIR/SHA256SUMS.txt"
else
  (cd "$OUT_DIR" && sha256sum 'LanScreen-TV-1.3.2.apk') > "$OUT_DIR/SHA256SUMS.txt"
fi
echo "APK 已生成：$APK"
echo '已检查签名、Android 安装元数据和 ZIP 对齐；尚需实际电视安装播放验收。'
