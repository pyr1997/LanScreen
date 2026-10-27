#!/bin/zsh
set -e
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR/server"
trap 'echo "启动未完成，请查看上面的提示。按回车关闭。"; read -r' ERR
echo "局域宣传屏 · Mac 启动"
echo "首次运行会在当前文件夹创建独立环境并安装依赖；后续启动可离线运行。"
echo "素材目录：${LANSCREEN_DATA:-$SCRIPT_DIR/server/data}；关闭此窗口会停止服务。"
if ! command -v python3 >/dev/null 2>&1; then
  echo "请先从 https://www.python.org/downloads/macos/ 安装 Python 3.10 或更高版本。"
  read -r
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python 3.10 或以上版本"'
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
CURRENT_REQUIREMENTS="$(shasum -a 256 requirements.txt | cut -d ' ' -f 1)"
SAVED_REQUIREMENTS=""
if [[ -f .venv/lanscreen-requirements.sha256 ]]; then
  SAVED_REQUIREMENTS="$(cat .venv/lanscreen-requirements.sha256)"
fi
if [[ "$CURRENT_REQUIREMENTS" != "$SAVED_REQUIREMENTS" ]]; then
  .venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt
  echo "$CURRENT_REQUIREMENTS" > .venv/lanscreen-requirements.sha256
fi
if [[ ! -x /Applications/LibreOffice.app/Contents/MacOS/soffice ]]; then
  echo "提示：PPT / PPTX 转换需要 LibreOffice。未安装时，图片、视频、PDF 仍可使用。"
  echo "LibreOffice 官方下载：https://www.libreoffice.org/download/download-libreoffice/"
fi
echo "电脑网页地址：http://127.0.0.1:8765"
echo "电视请使用本机局域网 IP，不能填写 127.0.0.1。"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8765}"
exec .venv/bin/python app.py
