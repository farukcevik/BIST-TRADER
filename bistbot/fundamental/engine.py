from __future__ import annotations

from math import sqrt
from statistics import mean

from .models import (FundamentalAssessment, FundamentalProviderStatus, FundamentalSnapshot,
                     RedFlag, RedFlagSeverity, ValuationStatus)
from .normalization import safe_ratio, trend_consistency, yoy_growth
from .scoring import FundamentalScoringSettings, band, renormalized_score


class FundamentalEngine:
    def __init__(self, settings: FundamentalScoringSettings | None = None):
        self.settings = settings or FundamentalScoringSettings()

    def evaluate(self, snapshot: FundamentalSnapshot) -> FundamentalAssessment:
        periods = sorted(snapshot.periods, key=lambda item: item.period_end, reverse=True)
        if not periods:
            return FundamentalAssessment(symbol=snapshot.symbol, as_of=snapshot.as_of,
                provider_status=FundamentalProviderStatus.UNAVAILABLE, confidence=0,
                missing_metrics=self._metric_names(), red_flags=[RedFlag(code="INSUFFICIENT_FUNDAMENTAL_DATA",
                severity=RedFlagSeverity.CAUTION, explanation="No normalized financial period is available")])
        latest = periods[0]
        val = lambda name: getattr(latest, name).value
        ttm=lambda name:self._ttm(periods,name)
        revenue_growth, ebitda_growth = yoy_growth(periods, "revenue"), yoy_growth(periods, "ebitda")
        income_growth = yoy_growth(periods, "net_income")
        operating_growth = yoy_growth(periods, "operating_profit")
        growth_values = [band(item, -10, 25) for item in (revenue_growth, ebitda_growth, income_growth) if item is not None]
        margins = [band(item * 100, 0, 25) for item in (val("ebitda_margin"), val("net_margin")) if item is not None]
        roe = safe_ratio(ttm("net_income"), val("equity")); roa = safe_ratio(ttm("net_income"), val("total_assets"))
        debt_equity=safe_ratio(val("total_debt"),val("equity")); net_debt_equity=safe_ratio(val("net_debt"),val("equity"))
        profitability = margins + ([band(roe * 100, 0, 20)] if roe is not None and val("equity") > 0 else []) + ([band(roa * 100, 0, 10)] if roa is not None else [])
        ttm_income,ttm_ocf,ttm_fcf=ttm("net_income"),ttm("operating_cash_flow"),ttm("free_cash_flow")
        ocf_income = safe_ratio(ttm_ocf, abs(ttm_income) if ttm_income is not None else None)
        fcf_quality = safe_ratio(ttm_fcf, abs(ttm_ocf) if ttm_ocf is not None else None)
        latest_fcf_quality=safe_ratio(val("free_cash_flow"),abs(val("operating_cash_flow")) if val("operating_cash_flow") is not None else None)
        cash_quality = ([band(ocf_income, 0, 1.2)] if ocf_income is not None else []) + ([band(fcf_quality, -.5, .5)] if fcf_quality is not None else [])
        ttm_ebitda=ttm("ebitda"); net_debt_ebitda = safe_ratio(val("net_debt"), ttm_ebitda)
        balance = ([band(net_debt_ebitda, 0, 4, False)] if net_debt_ebitda is not None and val("ebitda") > 0 else [])
        # Positive equity is necessary but, without leverage inputs, does not
        # establish a strong balance sheet. Keep the observation available and
        # neutral rather than turning one reported metric into a perfect score.
        if val("equity") is not None: balance.append(50 if val("equity") > 0 else 0)
        valuation_inputs=[]
        for own, sector, historical in (("pe","sector_pe","historical_pe_median"),("pb","sector_pb","historical_pb_median"),("ev_ebitda","sector_ev_ebitda","historical_ev_ebitda_median")):
            comparators=[item for item in (val(sector),val(historical)) if item and item > 0]
            if val(own) is not None and val(own) > 0 and comparators:
                valuation_inputs.append(band(val(own)/mean(comparators), .7, 1.6, False))
        consistencies=[trend_consistency(periods, field, count) for field in ("revenue","ebitda","net_income") for count in (4,8)]
        components={"growth_score":self._mean(growth_values), "profitability_score":self._mean(profitability),
            "cash_flow_quality_score":self._mean(cash_quality), "balance_sheet_score":self._mean(balance),
            "valuation_score":self._mean(valuation_inputs), "consistency_score":self._mean([x for x in consistencies if x is not None])}
        score, normalized = renormalized_score(components, self.settings.weights)
        available=[name for name in self._metric_names() if getattr(latest,name).available]
        missing=[name for name in self._metric_names() if name not in available]
        coverage=len(available)/len(self._metric_names())*100
        confidence=min(100, coverage*.7 + min(len(periods),8)/8*30)
        # The raw score intentionally retains renormalization across known
        # components. BUY consumers use this calibrated score so sparse but
        # excellent observations cannot masquerade as complete evidence.
        effective_score=None if score is None else score*sqrt((coverage/100)*(confidence/100))
        flags=[]; positives=[]
        self._flag(flags, val("operating_cash_flow") is not None and val("operating_cash_flow") < 0, "NEGATIVE_OPERATING_CASH_FLOW", "Operating cash flow is negative")
        self._flag(flags, latest_fcf_quality is not None and latest_fcf_quality < 0, "FREE_CASH_FLOW_WEAK", "Latest free cash flow is negative relative to operating cash flow")
        if net_debt_ebitda is not None and net_debt_ebitda > 4:
            self._flag(flags, True, "NET_DEBT_HIGH", f"Net debt/EBITDA is {net_debt_ebitda:.2f}")
        if len(periods)>1:
            previous=periods[1]
            previous_value=lambda name:getattr(previous,name).value
            self._flag(flags, val("net_debt") is not None and previous_value("net_debt") is not None and val("net_debt")>previous_value("net_debt")*1.1,
                "NET_DEBT_RISING", "Net debt increased by more than 10% from the prior period")
            self._flag(flags, val("ebitda_margin") is not None and previous_value("ebitda_margin") is not None and val("ebitda_margin")<previous_value("ebitda_margin")-.02,
                "EBITDA_MARGIN_DECLINING", "EBITDA margin declined by more than 2 percentage points")
            self._flag(flags, val("net_margin") is not None and previous_value("net_margin") is not None and val("net_margin")<previous_value("net_margin")-.02,
                "NET_MARGIN_DECLINING", "Net margin declined by more than 2 percentage points")
        if revenue_growth is not None and revenue_growth < 0:
            self._flag(flags, True, "REVENUE_GROWTH_WEAK", f"Revenue YoY growth is {revenue_growth:.1f}%")
        if ocf_income is not None and ocf_income < .5:
            self._flag(flags, True, "EARNINGS_QUALITY_WEAK", f"Operating cash flow/net income is {ocf_income:.2f}")
        self._flag(flags, val("equity") is not None and val("equity") < 0, "NEGATIVE_EQUITY", "Reported equity is negative")
        valuation_status, valuation_confidence=self._valuation(valuation_inputs)
        self._flag(flags, valuation_status is ValuationStatus.EXTREME, "EXTREME_VALUATION", "Relative valuation is extreme")
        if confidence < self.settings.minimum_confidence: flags.append(RedFlag(code="INSUFFICIENT_FUNDAMENTAL_DATA", severity=RedFlagSeverity.CAUTION, explanation=f"Fundamental confidence is only {confidence:.1f}%"))
        if revenue_growth is not None and revenue_growth > 10: positives.append("POSITIVE_REVENUE_GROWTH")
        if ocf_income is not None and ocf_income >= 1: positives.append("EARNINGS_BACKED_BY_CASH_FLOW")
        if net_debt_ebitda is not None and net_debt_ebitda <= 2: positives.append("CONSERVATIVE_NET_DEBT")
        blocking=set(self.settings.blocking_red_flags)
        for flag in flags:
            if flag.code in blocking: flag.severity=RedFlagSeverity.BLOCKING
        return FundamentalAssessment(symbol=snapshot.symbol,as_of=snapshot.as_of,provider_status=snapshot.provider_status,
            fundamental_score=None if score is None else round(score,4),
            effective_score=None if effective_score is None else round(effective_score,4),
            coverage=round(coverage,4),confidence=round(confidence,4),
            valuation_status=valuation_status,valuation_confidence=valuation_confidence,available_metrics=available,
            missing_metrics=missing,red_flags=flags,positive_factors=positives,
            score_breakdown={**components,"renormalized_weights":normalized},
            derived_metrics={name:self._derived(value) for name,value in {"yoy_revenue_growth":revenue_growth,
                "revenue_cagr":self._cagr(periods,"revenue"),"yoy_operating_profit_growth":operating_growth,
                "yoy_ebitda_growth":ebitda_growth,"yoy_net_income_growth":income_growth,"operating_cash_flow_vs_net_income":ocf_income,
                "free_cash_flow_quality":fcf_quality,"net_debt_ebitda":net_debt_ebitda,"roe":roe,"roa":roa,
                "debt_equity":debt_equity,"net_debt_equity":net_debt_equity,
                **{f"{field}_{count}q_consistency":trend_consistency(periods,field,count) for field in ("revenue","ebitda","net_income") for count in (4,8)}}.items()},
            blocks_entry=any(f.severity is RedFlagSeverity.BLOCKING for f in flags))

    @staticmethod
    def _mean(values: list[float]) -> float | None: return mean(values) if values else None
    @staticmethod
    def _metric_names() -> list[str]: return ["revenue","gross_profit","operating_profit","ebitda","net_income","operating_cash_flow","investing_cash_flow","capex","free_cash_flow","short_term_financial_debt","long_term_financial_debt","total_debt","cash_and_equivalents","net_debt","equity","total_assets","total_liabilities","market_cap","pe","pb","enterprise_value","ev_ebitda","gross_margin","operating_margin","ebitda_margin","net_margin"]
    @staticmethod
    def _flag(flags, condition, code, explanation):
        if condition: flags.append(RedFlag(code=code,severity=RedFlagSeverity.CAUTION,explanation=explanation))
    @staticmethod
    def _derived(value):
        from .models import Metric
        return Metric(value=value,available=value is not None,source="derived")
    @staticmethod
    def _valuation(scores):
        if not scores: return ValuationStatus.UNKNOWN, 0
        score=mean(scores); confidence=min(100,len(scores)/3*100)
        return (ValuationStatus.CHEAP if score>=70 else ValuationStatus.FAIR if score>=40 else ValuationStatus.EXPENSIVE if score>=15 else ValuationStatus.EXTREME), round(confidence,4)
    @staticmethod
    def _cagr(periods,field):
        # Eight quarters provide a two-year comparison; shorter histories remain unknown.
        if len(periods)<8:return None
        newest=getattr(periods[0],field).value; oldest=getattr(periods[7],field).value
        if newest is None or oldest is None or newest<=0 or oldest<=0:return None
        years=(periods[0].period_end-periods[7].period_end).days/365.2425
        if years<=0:return None
        return ((newest/oldest)**(1/years)-1)*100
    @staticmethod
    def _ttm(periods,field):
        values=[getattr(period,field).value for period in periods[:4]]
        return sum(values) if len(values)==4 and all(value is not None for value in values) else None
