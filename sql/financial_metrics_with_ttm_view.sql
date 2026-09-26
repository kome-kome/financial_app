-- 通期の行と TTM 行（#424 子2・ADR-0051）を同じ形で出す VIEW。
--
-- **`financial_metrics` は変えない。** あちらは画面・スクリーニング・M-1〜M-6・推薦・
-- バックテストの共通の入口で、TTM を混ぜると測る前に全部の結果が変わる。こちらは
-- 学習パネルが `plugins/macro_snapshots.use_fin_rows("with_ttm")` の内側でだけ読む。
--
-- 行の基準（CONTEXT.md「行の基準」）は `basis` 列で区別する: `annual`（通期の決算）と
-- `ttm`（直近12か月の合成・`ttm_financial_records`）。**窓関数は基準ごとに分ける**——
-- 同じ年度に 2 つの基準が混ざったまま Zスコアを取ると期間の違う値を並べることになり、
-- `LAG` の成長率は「通期 → TTM」の比という無意味な値になる（`financial_metrics` が
-- `period_type='annual'` で絞っているのと同じ理由）。
--
-- 比率・Zスコア・成長率の式は `financial_metrics_view.sql` と**同じ文面**にしてある
-- （`tests/test_financial_metrics_with_ttm.py` が照合する）。違いは 4 つだけ:
--   1. 入口が UNION ALL（通期 ＋ TTM）で、補正係数 F と基準の遅れ（#753）は行ごとに `fr.factor` /
--      `fr.shares_lag` / `fr.dps_lag` として渡る。TTM 行の遅れは 1.0（材料の間に分割があると合成しない
--      ので、期末後分割の年は TTM の材料にならない）
--   2. 窓に `basis` が入る
--   3. TTM 行の成長率・前年差は、直前の行が前年度のときだけ出す（間が空いた年は NULL）
--   4. `regression_results` は通期の行にだけ結合する（`sector_ols` は通期のまま・決定8）
CREATE OR REPLACE VIEW financial_metrics_with_ttm AS
WITH src AS (
    SELECT
        fr.id, fr.edinet_code, fr.sec_code, fr.company_name, fr.industry, fr.market,
        fr.year, fr.period_end, fr.doc_id, fr.source, fr.accounting_standard,
        'annual'::varchar AS basis, NULL::date AS filing_date,
        COALESCE(saf.factor, 1.0::double precision) AS factor,
        COALESCE(saf.shares_lag, 1.0::double precision) AS shares_lag,
        COALESCE(saf.dps_lag, 1.0::double precision) AS dps_lag,
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
        fr.stock_price, fr.market_cap, fr.per, fr.pbr, fr.div_yield,
        fr.dps, fr.employees, fr.issued_shares
    FROM financial_records fr
    LEFT JOIN split_adjustment_factors saf
           ON saf.edinet_code = fr.edinet_code AND saf.year = fr.year
    WHERE fr.period_type = 'annual'
    UNION ALL
    -- TTM 行。**id は負にする**——`financial_records.id` と番号が衝突すると、ORM の識別と
    -- 画面のリンクが別の行を指す（どちらも妥当な整数なのでエラーは出ない）。
    SELECT
        -t.id, t.edinet_code, t.sec_code, t.company_name, t.industry, t.market,
        t.year, t.period_end, t.doc_id, t.source, NULL::varchar,
        'ttm'::varchar, t.filing_date,
        COALESCE(t.split_factor, 1.0::double precision),
        1.0::double precision, 1.0::double precision,
        t.bs_total_assets, t.bs_current_assets, t.bs_receivables, t.bs_inventory,
        t.bs_noncurrent_assets, t.bs_buildings, t.bs_machinery, t.bs_ppe_total,
        t.bs_intangible_assets, t.bs_investments_other_assets,
        t.bs_cash, t.bs_investment_securities, t.bs_total_liabilities, t.bs_current_liabilities,
        t.bs_payables, t.bs_noncurrent_liabilities, t.bs_short_term_debt, t.bs_long_term_debt,
        t.bs_bonds_payable, t.bs_total_equity, t.bs_equity_parent, t.bs_paid_in_capital,
        t.bs_retained_earnings, t.bs_bps,
        t.pl_revenue, t.pl_cost_of_sales, t.pl_gross_profit, t.pl_sga, t.pl_operating_profit,
        t.pl_nonoperating_income, t.pl_ordinary_profit, t.pl_pretax_profit, t.pl_net_income,
        t.pl_net_income_attr, t.pl_eps, t.pl_ebitda,
        t.pl_rd_expenses, t.pl_depreciation, t.pl_extraordinary_income, t.pl_extraordinary_loss,
        t.cf_operating_cf, t.cf_investing_cf, t.cf_financing_cf, t.cf_free_cf,
        t.cf_net_change_cash, t.cf_capex,
        t.stock_price, t.market_cap, t.per, t.pbr, t.div_yield,
        t.dps, t.employees, t.issued_shares
    FROM ttm_financial_records t
),
d AS (
    SELECT
        fr.id, fr.edinet_code, fr.sec_code, fr.company_name, fr.industry, fr.market,
        fr.year, fr.period_end, fr.doc_id, fr.source, fr.accounting_standard,
        fr.basis, fr.filing_date,
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
        fr.stock_price,
        -- 補正の向きは `financial_metrics` と同じ（唯一の源は COLUMN_DIRECTION・遅れは BASIS_LAG_COLUMN）。
        -- F は行ごとに違う出どころから来る: 通期は `split_adjustment_factors`、TTM は合成時に決めた
        -- `ttm_financial_records.split_factor`（今期 H1 の提出日より後のイベントの積）。
        ROUND((fr.market_cap * fr.factor * fr.shares_lag)::numeric, 2)::double precision AS market_cap,
        ROUND((fr.per        * fr.factor)::numeric, 2)::double precision AS per,
        ROUND((fr.pbr        * fr.factor)::numeric, 2)::double precision AS pbr,
        ROUND((fr.div_yield  / NULLIF(fr.factor * fr.dps_lag, 0))::numeric, 2)::double precision AS div_yield,
        fr.dps,
        fr.employees, fr.issued_shares,
        fr.factor AS split_factor,
        fr.shares_lag AS split_shares_lag,
        fr.dps_lag AS split_dps_lag,
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
             THEN ROUND((COALESCE(fr.bs_current_assets,0) + COALESCE(fr.bs_investment_securities,0) * 0.7 - COALESCE(fr.bs_total_liabilities,0))::numeric, 0) END AS net_cash,
        CASE WHEN COALESCE(fr.bs_total_assets,0) <> 0
             THEN ROUND(((COALESCE(NULLIF(fr.pl_net_income,0), NULLIF(fr.pl_net_income_attr,0)) - fr.cf_operating_cf)
                         / fr.bs_total_assets)::numeric, 4) END AS accruals
    FROM src fr
    LEFT JOIN companies c ON c.edinet_code = fr.edinet_code
),
n AS (
    SELECT d.*,
        CASE WHEN d.net_cash IS NOT NULL AND COALESCE(d.market_cap,0) <> 0
             THEN ROUND((d.net_cash / (d.market_cap * 1000000))::numeric, 4) END AS nc_ratio
    FROM d
)
SELECT
    n.*,
    CASE WHEN COUNT(n.pl_revenue) OVER yw >= 2
         THEN ROUND(((n.pl_revenue - AVG(n.pl_revenue) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.pl_revenue) OVER yw, 0), 1.0))::numeric, 4) END AS z_revenue,
    CASE WHEN COUNT(n.op_margin) OVER yw >= 2
         THEN ROUND(((n.op_margin - AVG(n.op_margin) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.op_margin) OVER yw, 0), 1.0))::numeric, 4) END AS z_op_margin,
    CASE WHEN COUNT(n.roe) OVER yw >= 2
         THEN ROUND(((n.roe - AVG(n.roe) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.roe) OVER yw, 0), 1.0))::numeric, 4) END AS z_roe,
    CASE WHEN COUNT(n.equity_ratio) OVER yw >= 2
         THEN ROUND(((n.equity_ratio - AVG(n.equity_ratio) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.equity_ratio) OVER yw, 0), 1.0))::numeric, 4) END AS z_equity_ratio,
    CASE WHEN COUNT(n.cf_ratio) OVER yw >= 2
         THEN ROUND(((n.cf_ratio - AVG(n.cf_ratio) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.cf_ratio) OVER yw, 0), 1.0))::numeric, 4) END AS z_cf_ratio,
    CASE WHEN COUNT(n.pl_eps) OVER yw >= 2
         THEN ROUND(((n.pl_eps - AVG(n.pl_eps) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.pl_eps) OVER yw, 0), 1.0))::numeric, 4) END AS z_eps,
    CASE WHEN COUNT(n.de_ratio) OVER yw >= 2
         THEN ROUND(((n.de_ratio - AVG(n.de_ratio) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.de_ratio) OVER yw, 0), 1.0))::numeric, 4) END AS z_de_ratio,
    CASE WHEN COUNT(n.nc_ratio) OVER yw >= 2
         THEN ROUND(((n.nc_ratio - AVG(n.nc_ratio) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.nc_ratio) OVER yw, 0), 1.0))::numeric, 4) END AS z_nc_ratio,
    -- 成長率・前年差。**TTM 行は直前の行が前年度のときだけ出す**——TTM は材料が揃った年度に
    -- しか作らないので、間が空いた社では「2年前との比」が成長率として出てしまう（通期の行は
    -- `financial_metrics` と同じ式のまま）。
    CASE WHEN n.pl_revenue IS NOT NULL AND n.pl_revenue <> 0
          AND LAG(n.pl_revenue) OVER cw IS NOT NULL AND LAG(n.pl_revenue) OVER cw <> 0
          AND (n.basis = 'annual' OR LAG(n.year) OVER cw = n.year - 1)
         THEN ROUND(((n.pl_revenue / LAG(n.pl_revenue) OVER cw - 1) * 100)::numeric, 2) END AS rev_growth,
    CASE WHEN n.pl_operating_profit IS NOT NULL AND n.pl_operating_profit <> 0
          AND LAG(n.pl_operating_profit) OVER cw IS NOT NULL AND LAG(n.pl_operating_profit) OVER cw <> 0
          AND (n.basis = 'annual' OR LAG(n.year) OVER cw = n.year - 1)
         THEN ROUND(((n.pl_operating_profit / LAG(n.pl_operating_profit) OVER cw - 1) * 100)::numeric, 2) END AS op_growth,
    CASE WHEN n.pl_eps IS NOT NULL AND n.pl_eps <> 0
          AND LAG(n.pl_eps) OVER cw IS NOT NULL AND LAG(n.pl_eps) OVER cw <> 0
          AND (n.basis = 'annual' OR LAG(n.year) OVER cw = n.year - 1)
         THEN ROUND(((n.pl_eps / LAG(n.pl_eps) OVER cw - 1) * 100)::numeric, 2) END AS eps_growth,
    CASE WHEN n.roe IS NOT NULL AND LAG(n.roe) OVER cw IS NOT NULL
          AND (n.basis = 'annual' OR LAG(n.year) OVER cw = n.year - 1)
         THEN ROUND((n.roe - LAG(n.roe) OVER cw)::numeric, 2) END AS delta_roe,
    CASE WHEN n.op_margin IS NOT NULL AND LAG(n.op_margin) OVER cw IS NOT NULL
          AND (n.basis = 'annual' OR LAG(n.year) OVER cw = n.year - 1)
         THEN ROUND((n.op_margin - LAG(n.op_margin) OVER cw)::numeric, 2) END AS delta_op_margin,
    CASE WHEN COUNT(n.roe) OVER yws >= 2
         THEN ROUND(((n.roe - AVG(n.roe) OVER yws) / COALESCE(NULLIF(STDDEV_SAMP(n.roe) OVER yws, 0), 1.0))::numeric, 4) END AS z_roe_sec,
    CASE WHEN COUNT(n.op_margin) OVER yws >= 2
         THEN ROUND(((n.op_margin - AVG(n.op_margin) OVER yws) / COALESCE(NULLIF(STDDEV_SAMP(n.op_margin) OVER yws, 0), 1.0))::numeric, 4) END AS z_op_margin_sec,
    -- 株数の遅れだけを掛ける理由は `financial_metrics_view.sql` と同じ（#753）。TTM 行は rr が結合されない。
    rr.predicted_market_cap * n.split_shares_lag AS predicted_market_cap,
    rr.gap_ratio
FROM n
LEFT JOIN regression_results rr
       ON rr.edinet_code = n.edinet_code AND rr.year = n.year AND rr.period_end = n.period_end
      AND n.basis = 'annual'
WINDOW yw AS (PARTITION BY n.year, n.basis),
       yws AS (PARTITION BY n.year, n.industry, n.basis),
       cw AS (PARTITION BY n.edinet_code, n.basis ORDER BY n.year, n.period_end)
