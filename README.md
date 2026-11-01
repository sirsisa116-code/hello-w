# 日本株 買い候補ランキングツール

日本株の買い候補を毎日ランキングするツールです。候補を出すだけで、売買はしません。
設計の詳細は [docs/design.md](docs/design.md) を見てください。

現在は**段階2**（株価の取得、テクニカル指標による点数付け、ランキングとレポートの出力）まで作ってあります。
ファンダメンタルとニュースはまだ点数に入っていません。

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
| `output/daily/基準日_report.md` | 読むためのレポート。上位銘柄と、それぞれの点数の根拠 |
| `output/daily/基準日_ranking.csv` | 全銘柄の点数と指標の値 |
| `output/history.csv` | 毎日のランキングを全部つなげたもの（事後検証で使う） |
| `output/daily/実行日_prices_status.csv` | 銘柄ごとの最新日付・終値・データ件数・取得元 |
| `output/daily/実行日_quality.csv` | 取得失敗・データの古さなどの問題の一覧 |
| `data/raw/prices/銘柄コード.csv` | 株価の保存データ（次回からは差分だけ取ります） |

「基準日」は何日の終値で計算したか、「実行日」はツールを動かした日です。週末に実行すると、基準日は金曜日になります。
同じ基準日で何度実行しても、`history.csv` の中でその日の分が入れ替わるだけで、重複はしません。

CSV は Excel でそのまま開けます。レポート（`.md`）はメモ帳でも読めますが、VS Code などで開くと表がきれいに表示されます。

## 設定を変える

- `config/settings.yaml`: 対象銘柄（`watchlist`）、データの取り方、レポートに載せる銘柄数
- `config/weights.yaml`: 点数ルールと分野の重み。書き換えたら先頭の `version` を1つ上げてください

どちらもメモ帳などで開いて編集できます。

## テスト

計算が正しいかを確かめる自動テストです。プログラムを変更したら実行してください。

```powershell
python -m pytest
```
