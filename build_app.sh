#!/bin/bash
# ── HiBy M500 Monitor - macOS .app バンドル ビルドスクリプト ──
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="HiBy M500 Monitor"
APP_BUNDLE="${SCRIPT_DIR}/${APP_NAME}.app"
BUNDLE_ID="com.hiby.m500monitor.miku"
VERSION="1.0.0"

echo "========================================"
echo "  ${APP_NAME} - .app ビルド"
echo "========================================"
echo ""

# ── 前提チェック ──
if [ ! -f "${SCRIPT_DIR}/hiby_monitor.py" ]; then
    echo "❌ hiby_monitor.py が見つかりません"
    exit 1
fi
if [ ! -d "${SCRIPT_DIR}/.venv" ]; then
    echo "❌ .venv が見つかりません。先に setup.sh を実行してください"
    exit 1
fi

# ── 既存のバンドルを削除 ──
if [ -d "${APP_BUNDLE}" ]; then
    echo "🗑  既存の ${APP_NAME}.app を削除中..."
    rm -rf "${APP_BUNDLE}"
fi

# ── ディレクトリ構造作成 ──
echo "📁 .app バンドル構造を作成中..."
mkdir -p "${APP_BUNDLE}/Contents/MacOS"
mkdir -p "${APP_BUNDLE}/Contents/Resources"

# ── Info.plist ──
cat > "${APP_BUNDLE}/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleExecutable</key>
    <string>launch</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <true/>
    <key>NSHumanReadableCopyright</key>
    <string>HiBy M500 Monitor - Miku Edition</string>
</dict>
</plist>
PLIST

# ── ランチャースクリプト ──
cat > "${APP_BUNDLE}/Contents/MacOS/launch" << 'LAUNCHER'
#!/bin/bash
# HiBy M500 Monitor ランチャー
# .app バンドルの Contents/Resources から実行

RESOURCES_DIR="$(dirname "$0")/../Resources"
PROJECT_DIR="$(cd "${RESOURCES_DIR}/project" && pwd -P)"

# venv を activate して起動
source "${PROJECT_DIR}/.venv/bin/activate"
exec python "${PROJECT_DIR}/hiby_monitor.py"
LAUNCHER
chmod +x "${APP_BUNDLE}/Contents/MacOS/launch"

# ── Resources にプロジェクトへのシンボリックリンク ──
# ファイルをコピーするのではなく symlink を使うことで、
# コードを更新したときに .app の再ビルド不要
ln -s "${SCRIPT_DIR}" "${APP_BUNDLE}/Contents/Resources/project"

# ── アイコン生成 ──
echo "🎨 アプリアイコンを生成中..."

SVG_ICON="${SCRIPT_DIR}/app_icon.svg"
ICONSET_DIR="${SCRIPT_DIR}/.iconset.iconset"
rm -rf "${ICONSET_DIR}"
mkdir -p "${ICONSET_DIR}"

# SVG → PNG 変換 (Python + 組み込みライブラリで)
# cairosvg がなくても動くように、qlmanage フォールバック付き
generate_icon() {
    local svg_path="$1"
    local iconset_dir="$2"

    # 必要なサイズ (name size)
    local sizes=(
        "icon_16x16.png 16"
        "icon_16x16@2x.png 32"
        "icon_32x32.png 32"
        "icon_32x32@2x.png 64"
        "icon_128x128.png 128"
        "icon_128x128@2x.png 256"
        "icon_256x256.png 256"
        "icon_256x256@2x.png 512"
        "icon_512x512.png 512"
        "icon_512x512@2x.png 1024"
    )

    # まず最大サイズの PNG を生成
    local max_png="${iconset_dir}/_max.png"
    local converted=false

    # 方法1: qlmanage (macOS 内蔵)
    if command -v qlmanage &>/dev/null; then
        qlmanage -t -s 1024 -o "${iconset_dir}" "${svg_path}" 2>/dev/null || true
        local ql_output="${iconset_dir}/$(basename "${svg_path}").png"
        if [ -f "${ql_output}" ]; then
            mv "${ql_output}" "${max_png}"
            converted=true
        fi
    fi

    # 方法2: rsvg-convert (homebrew)
    if [ "${converted}" = false ] && command -v rsvg-convert &>/dev/null; then
        rsvg-convert -w 1024 -h 1024 "${svg_path}" -o "${max_png}" && converted=true
    fi

    # 方法3: Python cairosvg
    if [ "${converted}" = false ]; then
        "${SCRIPT_DIR}/.venv/bin/python" -c "
try:
    import cairosvg
    cairosvg.svg2png(url='${svg_path}', write_to='${max_png}', output_width=1024, output_height=1024)
except ImportError:
    # cairosvg がない場合は Pillow で簡易アイコンを生成
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new('RGBA', (1024, 1024), (13, 17, 23, 255))
        draw = ImageDraw.Draw(img)
        # 丸角四角
        draw.rounded_rectangle([32, 32, 992, 992], radius=180, fill=(13, 17, 23), outline=(57, 197, 187), width=6)
        # 中央にテキスト
        try:
            font = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 120)
            sfont = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 60)
        except:
            font = ImageFont.load_default()
            sfont = font
        draw.text((512, 420), '♪', fill=(57, 197, 187), font=font, anchor='mm')
        draw.text((512, 560), 'M500', fill=(232, 163, 181), font=sfont, anchor='mm')
        draw.text((512, 640), 'MIKU', fill=(57, 197, 187), font=sfont, anchor='mm')
        img.save('${max_png}')
    except ImportError:
        import sys; sys.exit(1)
" 2>/dev/null && converted=true
    fi

    if [ "${converted}" = false ]; then
        echo "⚠️  アイコン変換ツールが見つかりません。デフォルトアイコンを使用します。"
        echo "   より良いアイコンのために: brew install librsvg"
        return 1
    fi

    # 各サイズにリサイズ
    for entry in "${sizes[@]}"; do
        local name size
        name=$(echo "$entry" | cut -d' ' -f1)
        size=$(echo "$entry" | cut -d' ' -f2)
        sips -z "${size}" "${size}" "${max_png}" --out "${iconset_dir}/${name}" &>/dev/null
    done

    rm -f "${max_png}"
    return 0
}

ICON_GENERATED=false
if [ -f "${SVG_ICON}" ]; then
    if generate_icon "${SVG_ICON}" "${ICONSET_DIR}"; then
        # iconutil で .icns に変換
        iconutil -c icns "${ICONSET_DIR}" -o "${APP_BUNDLE}/Contents/Resources/AppIcon.icns" 2>/dev/null && ICON_GENERATED=true
    fi
fi

rm -rf "${ICONSET_DIR}"

if [ "${ICON_GENERATED}" = true ]; then
    echo "✅ アプリアイコン生成完了"
else
    echo "⚠️  アイコン生成をスキップ（デフォルトアイコンを使用）"
fi

# ── Dock で正しくアイコンキャッシュをリセット ──
touch "${APP_BUNDLE}"

echo ""
echo "========================================"
echo "  ✅ ビルド完了！"
echo "========================================"
echo ""
echo "  📍 ${APP_BUNDLE}"
echo ""
echo "  起動方法:"
echo "    1. Finder で上のフォルダを開く"
echo "    2. 「${APP_NAME}.app」をダブルクリック"
echo "    3. (任意) Dock にドラッグ & ドロップ"
echo ""
echo "  💡 /Applications に移動するには:"
echo "    cp -r \"${APP_BUNDLE}\" /Applications/"
echo ""

# ── Finder でフォルダを開く（オプション） ──
if [ "${1:-}" = "--open" ]; then
    open -R "${APP_BUNDLE}"
fi
