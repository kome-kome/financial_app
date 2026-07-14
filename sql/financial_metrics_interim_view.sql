-- financial_metrics_interim: 非通期(半期H1等)行の読み取り専用 VIEW（Issue #219② フェーズC）。
-- 通期用 financial_metrics（period_type='annual' 限定）と対をなし、非通期行を #323 イベント
-- 駆動モデルへ供給する。#322(/fins/summary)に無くスコープ外だった roe/roa/asset_turnover/
-- equity_ratio/cf_ratio 系（四半期BS/CF由来の質シグナル）を H1 実績から算出して充足する。
--
-- 設計:
--   - ソースは financial_records の period_type<>'annual' 行のみ（annual は financial_metrics 側）。
--   - 行単位比率は financial_metrics と同一式（op_margin/net_margin/roe/roa/equity_ratio/de_ratio/
--     cf_ratio/rd_intensity/da_intensity/asset_turnover/net_cash/nc_ratio）。H1 行は市場データ
--     （market_cap/per/pbr 等）が未収集のため nc_ratio 等の市場依存派生は NULL になる（想定内）。
--   - point-in-time 用に period_type / filing_date を露出する（#323 は filing_date 基準でリーク防止）。
--   - 成長率は「同一 period_type の前年同期比」= LAG OVER (PARTITION BY edinet_code, period_type
--     ORDER BY year, period_end)。H1 vs 前年 H1 の YoY を与える（通期行と混ざらない）。
--   - Zスコア（年度内クロスセクション）と regression_results JOIN（通期OLS予測）は持たない
--     （#323 は独自に正規化し、年次OLS予測値は H1 に非該当のため）。
CREATE OR REPLACE VIEW financial_metrics_interim AS
WITH d AS (
    SELECT
        fr.id, fr.edinet_code, fr.sec_code, fr.company_name, fr.industry, fr.market,
        fr.year, fr.period_end, fr.period_type, fr.filing_date,
        fr.doc_id, fr.source, fr.accounting_standard,
        fr.bs_total_assets, fr.bs_current_assets, fr.bs_receivables, fr.bs_inventory,
        fr.bs_noncurrent_assets, fr.bs_buildings, fr.bs_machinery, fr.bs_ppe_total,
        fr.bs_intangible_assets, fr.bs_investments_other_assets,
        fr.bs_cash, fr.bs_investment_securities, fr.bs_total_liabilities, fr.bs_current_liabilities,
        fr.bs_payables, fr.bs_noncurrent_liabilities, fr.bs_short_term_debt, fr.bs_long_term_debt,
        fr.bs_bonds_payable, fr.bs_total_equity, fr.bs_equity_parent, fr.bs_paid_in_capital,
        fr.bs_retained_earnings, fr.bs_bps,
        fr.pl_revenue, fr.pl_cost_of_sales, fr.pl_gross_profit, fr.pl_sga, fr.pl_operating_profit,
        fr.pl_nonoperating_income, fr.pl_ordinary_profit, fr.pl_pretax_profit, fr.pl_net_income,
        fr.pl_net_income_attr, fr.pl_eps, fr.pl_ebitda,
        fr.pl_rd_expenses, fr.pl_depreciation, fr.pl_extraordinary_income, fr.pl_extraordinary_loss,
        fr.cf_operating_cf, fr.cf_investing_cf, fr.cf_financing_cf, fr.cf_free_cf,
        fr.cf_net_change_cash, fr.cf_capex,
        fr.stock_price, fr.market_cap, fr.per, fr.pbr, fr.div_yield, fr.dps,
        fr.employees, fr.issued_shares,
        c.is_active, c.delisted_date,
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((COALESCE(fr.pl_operating_profit,0) / fr.pl_revenue * 100)::numeric, 2) END AS op_margin,
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((COALESCE(NULLIF(fr.pl_net_income,0), NULLIF(fr.pl_net_income_attr,0), 0) / fr.pl_revenue * 100)::numeric, 2) END AS net_margin,
        CASE WHEN COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0) <> 0
             THEN ROUND((COALESCE(NULLIF(fr.pl_net_income,0), NULLIF(fr.pl_net_income_attr,0), 0) / COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0) * 100)::numeric, 2) END AS roe,
        CASE WHEN COALESCE(fr.bs_total_assets,0) <> 0
             THEN ROUND((COALESCE(NULLIF(fr.pl_net_income,0), NULLIF(fr.pl_net_income_attr,0), 0) / fr.bs_total_assets * 100)::numeric, 2) END AS roa,
        CASE WHEN COALESCE(fr.bs_total_assets,0) <> 0
             THEN ROUND((COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0) / fr.bs_total_assets * 100)::numeric, 2) END AS equity_ratio,
        CASE WHEN COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0) <> 0
             THEN ROUND(((COALESCE(fr.bs_short_term_debt,0) + COALESCE(fr.bs_long_term_debt,0)) / COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0))::numeric, 4) END AS de_ratio,
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((COALESCE(fr.cf_operating_cf,0) / fr.pl_revenue * 100)::numeric, 2) END AS cf_ratio,
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((fr.pl_rd_expenses / fr.pl_revenue * 100)::numeric, 2) END AS rd_intensity,
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((fr.pl_depreciation / fr.pl_revenue * 100)::numeric, 2) END AS da_intensity,
        CASE WHEN COALESCE(fr.bs_total_assets,0) <> 0
             THEN ROUND((COALESCE(fr.pl_revenue,0) / fr.bs_total_assets)::numeric, 4) END AS asset_turnover,
        CASE WHEN COALESCE(fr.bs_current_assets,0) <> 0 OR COALESCE(fr.bs_total_liabilities,0) <> 0
             THEN ROUND((COALESCE(fr.bs_current_assets,0) + COALESCE(fr.bs_investment_securities,0) * 0.7 - COALESCE(fr.bs_total_liabilities,0))::numeric, 0) END AS net_cash
    FROM financial_records fr
    LEFT JOIN companies c ON c.edinet_code = fr.edinet_code
    WHERE fr.period_type <> 'annual'
),
n AS (
    SELECT d.*,
        CASE WHEN d.net_cash IS NOT NULL AND COALESCE(d.market_cap,0) <> 0
             THEN ROUND((d.net_cash / (d.market_cap * 1000000))::numeric, 4) END AS nc_ratio
    FROM d
)
SELECT
    n.*,
    CASE WHEN n.pl_revenue IS NOT NULL AND n.pl_revenue <> 0
          AND LAG(n.pl_revenue) OVER cw IS NOT NULL AND LAG(n.pl_revenue) OVER cw <> 0
         THEN ROUND(((n.pl_revenue / LAG(n.pl_revenue) OVER cw - 1) * 100)::numeric, 2) END AS rev_growth,
    CASE WHEN n.pl_operating_profit IS NOT NULL AND n.pl_operating_profit <> 0
          AND LAG(n.pl_operating_profit) OVER cw IS NOT NULL AND LAG(n.pl_operating_profit) OVER cw <> 0
         THEN ROUND(((n.pl_operating_profit / LAG(n.pl_operating_profit) OVER cw - 1) * 100)::numeric, 2) END AS op_growth,
    CASE WHEN n.pl_eps IS NOT NULL AND n.pl_eps <> 0
          AND LAG(n.pl_eps) OVER cw IS NOT NULL AND LAG(n.pl_eps) OVER cw <> 0
         THEN ROUND(((n.pl_eps / LAG(n.pl_eps) OVER cw - 1) * 100)::numeric, 2) END AS eps_growth
FROM n
WINDOW cw AS (PARTITION BY n.edinet_code, n.period_type ORDER BY n.year, n.period_end)
