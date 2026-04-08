#!/bin/bash
# ── HiBy M500 Monitor セットアップスクリプト ──
set -euo pipefail

echo "========================================"
echo "  HiBy M500 Monitor - セットアップ"
echo "========================================"
echo ""

# 1. Homebrew 確認
if ! command -v brew &>/dev/null; then
    echo "❌ Homebrew が見つかりません。"
    echo "   https://brew.sh からインストールしてください。"
    exit 1
fi
echo "✅ Homebrew 検出"

# 2. ADB インストール
if ! command -v adb &>/dev/null; then
    echo "📦 android-platform-tools をインストール中..."
    brew install android-platform-tools
else
    echo "✅ adb 検出: $(adb version | head -1)"
fi

# 3. Python 確認
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 が見つかりません。"
    exit 1
fi
echo "✅ Python: $(python3 --version)"

# 4. Python 仮想環境
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

if [ ! -d "${VENV_DIR}" ]; then
    echo "📦 仮想環境を作成中..."
    python3 -m venv "${VENV_DIR}"
fi
source "${VENV_DIR}/bin/activate"

echo "📦 依存パッケージをインストール中..."
pip install --quiet --upgrade pip
pip install --quiet pyobjc-framework-Cocoa pyobjc-framework-WebKit

echo ""
echo "========================================"
echo "  セットアップ完了！"
echo "========================================"
echo ""
echo "▶ 使い方:"
echo "  1. HiBy M500 をUSBで接続"
echo "  2. M500 側で USBデバッグ を有効化"
echo "     設定 → デバイスについて → ビルド番号を7回タップ"
echo "     → 開発者オプション → USBデバッグ ON"
echo "  3. 起動:"
echo "     ${SCRIPT_DIR}/run.sh"
echo ""
echo "  メニューバーに 🎵 アイコンが表示されます。"
echo "  クリックするとポップオーバーでダッシュボードが開きます。"
echo ""
