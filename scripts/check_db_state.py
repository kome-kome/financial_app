"""DB 状態確認スクリプト。

主要 6 テーブルの行数と最近 5 件の収集ログを表示する。Supabase 移行後の差分確認や
GitHub Actions パイプライン実行後の件数チェックに使う。

実行: python scripts/check_db_state.py
"""
from dotenv import load_dotenv
load_dotenv()
from database import engine
from sqlalchemy import text

with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT 'companies'           AS tbl, COUNT(*) AS cnt FROM companies
        UNION ALL SELECT 'financial_records',  COUNT(*) FROM financial_records
        UNION ALL SELECT 'stock_price_history', COUNT(*) FROM stock_price_history
        UNION ALL SELECT 'xbrl_raw_documents', COUNT(*) FROM xbrl_raw_documents
        UNION ALL SELECT 'macro_data',         COUNT(*) FROM macro_data
        UNION ALL SELECT 'collection_logs',    COUNT(*) FROM collection_logs
    """)).fetchall()
    for r in rows:
        print(f"  {r[0]:<25} {r[1]:>10,} 件")

    # 最新の収集ログ
    print()
    logs = conn.execute(text(
        "SELECT job_type, status, started_at, finished_at, companies_processed "
        "FROM collection_logs ORDER BY started_at DESC LIMIT 5"
    )).fetchall()
    print("最近の収集ログ:")
    for l in logs:
        print(f"  {l[1]:<10} {l[0]:<15} {str(l[2])[:16]} → {str(l[3])[:16]}  {l[4]} 社")
