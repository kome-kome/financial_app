"""財務レコード（financial_metrics VIEW 読み取りモデル）の表示用シリアライザ。

api.py の routing から引き上げた純粋関数。company / 財務一覧画面が消費する
bs/pl/cf/val/nc/zscore のネスト dict へ整形する（残差表示・チャート用）。
ORM 行の属性を読むだけで db/HTTP に依存しないため直接テストできる。
"""


def record_to_dict(r) -> dict:
    """FinancialMetric（または同等の属性を持つ行）を表示用ネスト dict へ整形する。"""
    return {
        "edinet_code":  r.edinet_code,
        "sec_code":     r.sec_code,
        "company_name": r.company_name,
        "industry":     r.industry,
        "is_active":     r.is_active is not False,
        "delisted_date": r.delisted_date.isoformat() if r.delisted_date else None,
        "year": r.year, "period_end": r.period_end.isoformat() if r.period_end else None,
        "bs": {
            "total_assets": r.bs_total_assets,
            "current_assets": r.bs_current_assets,
            "receivables": r.bs_receivables,
            "inventory": r.bs_inventory,
            "noncurrent_assets": r.bs_noncurrent_assets,
            "buildings": r.bs_buildings,
            "machinery": r.bs_machinery,
            "ppe_total": r.bs_ppe_total,
            "intangible_assets": r.bs_intangible_assets,
            "investments_other_assets": r.bs_investments_other_assets,
            "cash": r.bs_cash,
            "total_liabilities": r.bs_total_liabilities,
            "total_equity": r.bs_total_equity,
            "equity_parent": r.bs_equity_parent,
            "short_term_debt": r.bs_short_term_debt,
            "long_term_debt": r.bs_long_term_debt,
            "bps": r.bs_bps,
            "equity_ratio": r.equity_ratio,
        },
        "pl": {
            "revenue": r.pl_revenue,
            "cost_of_sales": r.pl_cost_of_sales,
            "gross_profit": r.pl_gross_profit,
            "sga": r.pl_sga,
            "operating_profit": r.pl_operating_profit,
            "nonoperating_income": r.pl_nonoperating_income,
            "ordinary_profit": r.pl_ordinary_profit,
            "pretax_profit": r.pl_pretax_profit,
            "extraordinary_income": r.pl_extraordinary_income,
            "extraordinary_loss": r.pl_extraordinary_loss,
            "net_income": r.pl_net_income,
            "eps": r.pl_eps,
            "ebitda": r.pl_ebitda,
            "rd_expenses": r.pl_rd_expenses,
            "depreciation": r.pl_depreciation,
            "op_margin": r.op_margin,
            "net_margin": r.net_margin,
            "rev_growth": r.rev_growth,
            "eps_growth": r.eps_growth,
        },
        "cf": {
            "operating_cf": r.cf_operating_cf,
            "investing_cf": r.cf_investing_cf,
            "financing_cf": r.cf_financing_cf,
            "free_cf": r.cf_free_cf,
            "capex": r.cf_capex,
            "cf_ratio": r.cf_ratio,
        },
        "val": {
            "market_cap": r.market_cap,
            "stock_price": r.stock_price,
            "per": r.per, "pbr": r.pbr,
            "div_yield": r.div_yield,
            "dps": r.dps,
            "de_ratio": r.de_ratio,
            "roe": r.roe, "roa": r.roa,
        },
        "nc": {
            "net_cash": r.net_cash,
            "nc_ratio": r.nc_ratio,
        },
        "nonfin": {
            "employees": r.employees,
            "issued_shares": r.issued_shares,
        },
        "zscore": {
            "z_revenue": r.z_revenue,
            "z_op_margin": r.z_op_margin,
            "z_roe": r.z_roe,
            "z_equity_ratio": r.z_equity_ratio,
            "z_cf_ratio": r.z_cf_ratio,
            "z_eps": r.z_eps,
            "z_de_ratio": r.z_de_ratio,
            "z_nc_ratio": r.z_nc_ratio,
        },
        "predicted_market_cap": r.predicted_market_cap,
        "gap_ratio": r.gap_ratio,
    }
