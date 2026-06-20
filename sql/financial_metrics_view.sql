CREATE OR REPLACE VIEW financial_metrics AS
WITH d AS (
    SELECT
        fr.id, fr.edinet_code, fr.sec_code, fr.company_name, fr.industry, fr.market,
        fr.year, fr.period_end, fr.doc_id, fr.source, fr.accounting_standard,
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
        -- C2 結線: 研究開発集約度・減価償却集約度（無次元 [%]）。分子は非 COALESCE で
        -- null 伝播させる（R&D/D&A 未開示企業は intensity も null → price_predictor が自動除外）。
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((fr.pl_rd_expenses / fr.pl_revenue * 100)::numeric, 2) END AS rd_intensity,
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((fr.pl_depreciation / fr.pl_revenue * 100)::numeric, 2) END AS da_intensity,
        -- 総資産回転率（無次元・回）。デュポン分解 ROA ≈ net_margin × asset_turnover の中核因子。
        CASE WHEN COALESCE(fr.bs_total_assets,0) <> 0
             THEN ROUND((COALESCE(fr.pl_revenue,0) / fr.bs_total_assets)::numeric, 4) END AS asset_turnover,
        CASE WHEN COALESCE(fr.bs_current_assets,0) <> 0 OR COALESCE(fr.bs_total_liabilities,0) <> 0
             THEN ROUND((COALESCE(fr.bs_current_assets,0) + COALESCE(fr.bs_investment_securities,0) * 0.7 - COALESCE(fr.bs_total_liabilities,0))::numeric, 0) END AS net_cash
    FROM financial_records fr
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
    CASE WHEN n.pl_revenue IS NOT NULL AND n.pl_revenue <> 0
          AND LAG(n.pl_revenue) OVER cw IS NOT NULL AND LAG(n.pl_revenue) OVER cw <> 0
         THEN ROUND(((n.pl_revenue / LAG(n.pl_revenue) OVER cw - 1) * 100)::numeric, 2) END AS rev_growth,
    CASE WHEN n.pl_operating_profit IS NOT NULL AND n.pl_operating_profit <> 0
          AND LAG(n.pl_operating_profit) OVER cw IS NOT NULL AND LAG(n.pl_operating_profit) OVER cw <> 0
         THEN ROUND(((n.pl_operating_profit / LAG(n.pl_operating_profit) OVER cw - 1) * 100)::numeric, 2) END AS op_growth,
    CASE WHEN n.pl_eps IS NOT NULL AND n.pl_eps <> 0
          AND LAG(n.pl_eps) OVER cw IS NOT NULL AND LAG(n.pl_eps) OVER cw <> 0
         THEN ROUND(((n.pl_eps / LAG(n.pl_eps) OVER cw - 1) * 100)::numeric, 2) END AS eps_growth,
    rr.predicted_market_cap,
    rr.gap_ratio
FROM n
LEFT JOIN regression_results rr
       ON rr.edinet_code = n.edinet_code AND rr.year = n.year AND rr.period_end = n.period_end
WINDOW yw AS (PARTITION BY n.year),
       cw AS (PARTITION BY n.edinet_code ORDER BY n.year, n.period_end)
