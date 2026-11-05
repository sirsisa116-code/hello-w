# 日本株 買い候補ランキングツール

日本株の買い候補を毎日ランキングするツールです。候補を出すだけで、売買はしません。
設計の詳細は [docs/design.md](docs/design.md) を見てください。

現在は**段階3**（株価の取得、テクニカル指標による点数付け、ランキングとレポートの出力、事後検証、過去のランキングの後付け再現）まで作ってあります。
点数に入っているのは、テクニカル指標と適時開示（タイトルによる機械的な分類）です。ファンダメンタルとニュースはまだ入っていません。

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

東証の取引終了後（16時以降）に実行してください。

毎日の実行では、適時開示の XBRL（決算短信や業績予想の修正の数字）も取って `data\xbrl\` に保存します。
TDnet は資料を約1か月しか置いておかないので、**1か月以上あけずに実行してください**。あいた分は二度と取れません。
初回は1か月分をまとめて取るので、決算発表の時期だと20分ほどかかることがあります。それより前に実行すると、当日の株価は未確定として除外されます。

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

- `config/settings.yaml`: 対象銘柄、データの取り方、レポートに載せる銘柄数
  - `universe.mode: prime`（いまの設定）: 東証プライムのうち TOPIX500 相当（Core30・Large70・Mid400、約500銘柄）
  - `universe.mode: watchlist`: `watchlist` に書いた銘柄だけ（約20銘柄、動作確認用）
- `config/weights.yaml`: 点数ルールと分野の重み。書き換えたら先頭の `version` を1つ上げてください

どちらもメモ帳などで開いて編集できます。

## 事後検証（ランキングが当たっていたかを確かめる）

過去のランキングの銘柄を「翌営業日の始値で買い、N営業日後（1・5・10・20）の終値で売った」とした場合の成績を、TOPIX ETF（1306）と比べます。株価は手元の保存データを使うので、先に `run_daily.py` を実行しておくと最新の値動きまで入ります。

```powershell
python scripts/evaluate.py                    # 毎日のランキング（history.csv）を検証
python scripts/evaluate.py --source backfill  # 後付けで再現したランキングを検証
```

結果は `output/evaluation/` に、レポート（`.md`）と1件ずつの明細（`.csv`）で保存されます。

## 適時開示の種類ごとの値動き（イベントスタディ）

「上方修正が出た銘柄は、その後どう動いたか」を、開示の種類ごとに集計します。開示の翌朝に寄り付きで買う前提で、
買う前に動いてしまった分（反応）と、買ったあとに取れた分（N営業日後）を分けて出します。

```powershell
python scripts/event_study.py                     # 直近1年分
python scripts/event_study.py --start 2025-11-01  # 期間を指定
```

開示の分類の決まり（どの言葉なら何点か）は `config/weights.yaml` の `disclosure` にあります。

## 後付けの再現（過去のランキングを今から作る）

毎日のランキングは始めた日からしかたまらないので、過去の各営業日について同じ計算をやり直し、`output/history_backfill.csv` に保存します。毎日の `history.csv` とは混ぜません。

```powershell
python scripts/backfill.py --fetch-days 900   # 初回: 株価を約2年半分取り直してから再現（500銘柄で20〜30分かかることがある）
                                              # 適時開示も、手元にない日の分を取りに行く（初回は1年分で10分前後）
python scripts/backfill.py                    # 2回目以降: 手元の株価だけで再現し直す（点数ルールを変えたときなど）
```

注意: 銘柄は「今の」対象銘柄を使うので、途中で上場廃止になった銘柄などが入りません。そのぶん成績は実際より良く見えやすくなります。また、ニュースは過去にさかのぼれないので再現できません。

## テスト

計算が正しいかを確かめる自動テストです。プログラムを変更したら実行してください。

```powershell
python -m pytest
```
