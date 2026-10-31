# 日本株 買い候補ランキングツール

日本株の買い候補を毎日ランキングするツールです。候補を出すだけで、売買はしません。
設計の詳細は [docs/design.md](docs/design.md) を見てください。

現在は**段階1**（銘柄一覧と株価の取得、データの問題の記録）まで作ってあります。

## 初回の準備（Windows）

1. Python 3.11 以上を入れる（https://www.python.org/ ）。インストール画面で「Add python.exe to PATH」にチェックを入れてください。
2. このリポジトリを好きな場所に置く（例: `C:\Users\あなた\kabu\hello-w`）。
3. スタートメニューから「ターミナル」（または PowerShell）を開き、以下を順に実行する。

```powershell
cd C:\Users\あなた\kabu\hello-w      # 置いた場所に移動
python -m venv .venv                   # このツール専用のPython環境を作る（初回だけ）
.venv\Scripts\Activate.ps1             # 専用環境に切り替える（毎回）
pip install -r requirements.txt        # 必要なライブラリを入れる（初回だけ）
```

`Activate.ps1` で「スクリプトの実行が無効」というエラーが出たら、次を1回だけ実行してから、もう一度試してください。

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 毎日の実行

```powershell
cd C:\Users\あなた\kabu\hello-w
.venv\Scripts\Activate.ps1
python scripts/run_daily.py
```

東証の取引終了後（16時以降）に実行してください。それより前に実行すると、当日の株価は未確定として除外されます。

## 出力されるもの

| ファイル | 中身 |
|---|---|
| `output/daily/日付_prices_status.csv` | 銘柄ごとの最新日付・終値・データ件数・取得元 |
| `output/daily/日付_quality.csv` | 取得失敗・データの古さなどの問題の一覧 |
| `data/raw/prices/銘柄コード.csv` | 株価の保存データ（次回からは差分だけ取ります） |

CSV は Excel でそのまま開けます。

## 設定を変える

`config/settings.yaml` をメモ帳などで開いて編集します。対象銘柄（`watchlist`）の追加・削除もここで行います。

## テスト

計算が正しいかを確かめる自動テストです。プログラムを変更したら実行してください。

```powershell
python -m pytest
```
