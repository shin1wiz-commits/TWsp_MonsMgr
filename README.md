# TWsp_MonsMgr v2.8.24 正式基礎マスター移行版

app/ はアプリZIP、tools/altema-master/ は659体基礎マスター再構築系、
.github/workflows/ はAPKビルドとマスター再構築を分離して管理します。

v2.8.24:
- 最終検証済み659体の no/name/rank/family/size を monsters.csv に全面採用
- No.593/594 の確認済み補正を反映
- No.80 の Altema /monster/430 fallback をアプリ内基礎データ取得にも反映
- rebuild tool は「ミストウィング / ミストウイング」を同一対象として照合
- versionCode 48 / versionName 2.8.24
