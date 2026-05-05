# 無料PostgreSQL接続メモ

このアプリは `DATABASE_URL` を使って PostgreSQL に接続します。

## 1. 無料PostgreSQLを作成
- Neon / Supabase / Render PostgreSQL などでDBを作成
- 接続文字列（`postgresql://...`）を控える

## 2. ローカル実行
PowerShell:

```powershell
$env:DATABASE_URL="postgresql://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=require"
pip install -r requirements.txt
python app.py
```

## 3. Renderへデプロイ
- Web Service の Environment Variables で `DATABASE_URL` を設定
- Start Command は `gunicorn app:app`（`render.yaml` で設定済み）

これで3つのチェック状態がPostgreSQLに保存され、再アクセス時に復元されます。
