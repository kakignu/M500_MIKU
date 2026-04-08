# HiBy M500 Monitor - Miku Edition

macOS メニューバーに常駐する HiBy M500 バッテリーモニターアプリです。
ミクカラーのダッシュボードで、バッテリー状態をリッチに表示します。

## 機能

- メニューバーからバッテリー情報をすばやく確認
- WKWebView ベースのミクカラーダッシュボード
- USB 接続を検出し自動で WiFi ADB に切り替え
- ケーブルを外しても監視を継続

## 必要条件

- macOS 12.0+
- Python 3.9+
- adb (`brew install android-platform-tools`)
- HiBy M500 の USB デバッグが有効であること

## セットアップ

```bash
# 依存関係のインストール
./setup.sh

# 起動
./run.sh
```

## .app ビルド

```bash
./build_app.sh
```

ビルド後、`HiBy M500 Monitor.app` をダブルクリックで起動できます。

## GitHub Actions

`main` ブランチへの Push で自動ビルドが実行されます。
`v*` タグを Push すると GitHub Releases に .zip が自動アップロードされます。

```bash
git tag v1.0.0
git push origin v1.0.0
```
