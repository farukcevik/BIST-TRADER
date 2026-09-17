from __future__ import annotations

from math import sqrt
from statistics import mean

from .models import (FundamentalAssessment, FundamentalProviderStatus, FundamentalSnapshot,
                     Metric, RedFlag, RedFlagSeverity, ValuationStatus)
from .normalization import safe_ratio, yoy_growth
from .scoring import FundamentalScoringSettings, band, renormalized_score


GENERAL_WEIGHTS={"revenue_yoy":.20,"gross_margin":.15,"net_margin":.15,"roe":.15,
    "ocf_net_income":.15,"equity_assets":.10,"asset_yoy":.10}
BANK_WEIGHTS={"net_income_yoy":.20,"net_interest_income_yoy":.15,"loan_growth":.15,
    "deposit_growth":.15,"roe":.15,"roa":.10,"equity_assets":.10}


class FundamentalEngine:
    """Two-profile fundamental scorer. Missing observations never become zero."""
    def __init__(self, settings: FundamentalScoringSettings | None = None):
        self.settings=settings or FundamentalScoringSettings()

    def evaluate(self,snapshot: FundamentalSnapshot)->FundamentalAssessment:
        periods=sorted(snapshot.periods,key=lambda item:item.period_end,reverse=True)
        profile="BANK" if snapshot.audit_metadata.get("company_type")=="BANK" else "GENERAL"
        weights=BANK_WEIGHTS if profile=="BANK" else GENERAL_WEIGHTS
        if not periods:
            return FundamentalAssessment(symbol=snapshot.symbol,as_of=snapshot.as_of,
                provider_status=FundamentalProviderStatus.UNAVAILABLE,confidence=0,
                missing_metrics=list(weights),red_flags=[RedFlag(code="INSUFFICIENT_FUNDAMENTAL_DATA",
                severity=RedFlagSeverity.CAUTION,explanation="No normalized financial period is available")],
                score_breakdown={"profile":profile,"metric_values":{name:None for name in weights},
                    "renormalized_weights":{}})
        latest=periods[0]; val=lambda name:getattr(latest,name).value
        revenue_yoy=yoy_growth(periods,"revenue");income_yoy=yoy_growth(periods,"net_income")
        interest_yoy=yoy_growth(periods,"net_interest_income");loan_growth=yoy_growth(periods,"loans")
        deposit_growth=yoy_growth(periods,"deposits");asset_yoy=self._balance_yoy(periods,"total_assets")
        income_basis,income_period=self._flow_basis(periods,"net_income")
        cash_income,ocf_basis=self._cash_income_basis(periods)
        roe=safe_ratio(income_basis,self._matched_balance(periods,income_period,"equity"))
        roa=safe_ratio(income_basis,self._matched_balance(periods,income_period,"total_assets"))
        ocf_income=safe_ratio(ocf_basis,abs(cash_income) if cash_income is not None else None)
        equity_assets=safe_ratio(val("equity"),val("total_assets"))
        raw={"revenue_yoy":revenue_yoy,"gross_margin":val("gross_margin"),"net_margin":val("net_margin"),
            "roe":roe,"ocf_net_income":ocf_income,"equity_assets":equity_assets,"asset_yoy":asset_yoy,
            "net_income_yoy":income_yoy,"net_interest_income_yoy":interest_yoy,"loan_growth":loan_growth,
            "deposit_growth":deposit_growth,"roa":roa}
        scores={name:self._score(name,raw[name]) for name in weights}
        score,normalized=renormalized_score(scores,weights)
        available=[name for name in weights if raw[name] is not None]
        missing=[name for name in weights if name not in available]
        coverage=len(available)/len(weights)*100
        confidence=min(100,coverage*.7+min(len(periods),8)/8*30)
        effective=None if score is None else score*sqrt((coverage/100)*(confidence/100))
        flags=[];positives=[]
        self._flag(flags,val("operating_cash_flow") is not None and val("operating_cash_flow")<0,
            "NEGATIVE_OPERATING_CASH_FLOW","Operating cash flow is negative")
        self._flag(flags,val("equity") is not None and val("equity")<0,"NEGATIVE_EQUITY","Reported equity is negative")
        latest_fcf_quality=safe_ratio(val("free_cash_flow"),abs(val("operating_cash_flow")) if val("operating_cash_flow") is not None else None)
        self._flag(flags,latest_fcf_quality is not None and latest_fcf_quality<0,"FREE_CASH_FLOW_WEAK","Latest free cash flow is negative relative to operating cash flow")
        if len(periods)>1:
            previous=periods[1];previous_value=lambda name:getattr(previous,name).value
            self._flag(flags,val("net_debt") is not None and previous_value("net_debt") is not None and val("net_debt")>previous_value("net_debt")*1.1,"NET_DEBT_RISING","Net debt increased by more than 10% from the prior period")
            self._flag(flags,val("ebitda_margin") is not None and previous_value("ebitda_margin") is not None and val("ebitda_margin")<previous_value("ebitda_margin")-.02,"EBITDA_MARGIN_DECLINING","EBITDA margin declined by more than 2 percentage points")
            self._flag(flags,val("net_margin") is not None and previous_value("net_margin") is not None and val("net_margin")<previous_value("net_margin")-.02,"NET_MARGIN_DECLINING","Net margin declined by more than 2 percentage points")
        if revenue_yoy is not None and revenue_yoy<0:self._flag(flags,True,"REVENUE_GROWTH_WEAK",f"Revenue YoY growth is {revenue_yoy:.1f}%")
        if ocf_income is not None and ocf_income<.5:self._flag(flags,True,"EARNINGS_QUALITY_WEAK",f"Operating cash flow/net income is {ocf_income:.2f}")
        if confidence<self.settings.minimum_confidence:flags.append(RedFlag(code="INSUFFICIENT_FUNDAMENTAL_DATA",severity=RedFlagSeverity.CAUTION,explanation=f"Fundamental confidence is only {confidence:.1f}%"))
        if revenue_yoy is not None and revenue_yoy>10:positives.append("POSITIVE_REVENUE_GROWTH")
        if ocf_income is not None and ocf_income>=1:positives.append("EARNINGS_BACKED_BY_CASH_FLOW")
        blocking=set(self.settings.blocking_red_flags)
        for flag in flags:
            if flag.code in blocking:flag.severity=RedFlagSeverity.BLOCKING
        valuation_inputs=self._valuation_inputs(latest)
        valuation_status,valuation_confidence=self._valuation(valuation_inputs)
        derived={"yoy_revenue_growth":revenue_yoy,"revenue_cagr":self._cagr(periods,"revenue"),"yoy_net_income_growth":income_yoy,
            "yoy_net_interest_income_growth":interest_yoy,"yoy_loan_growth":loan_growth,
            "yoy_deposit_growth":deposit_growth,"yoy_asset_growth":asset_yoy,
            "operating_cash_flow_vs_net_income":ocf_income,"roe":roe,"roa":roa,"equity_assets":equity_assets}
        return FundamentalAssessment(symbol=snapshot.symbol,as_of=snapshot.as_of,provider_status=snapshot.provider_status,
            fundamental_score=None if score is None else round(score,4),effective_score=None if effective is None else round(effective,4),
            coverage=round(coverage,4),confidence=round(confidence,4),valuation_status=valuation_status,
            valuation_confidence=valuation_confidence,available_metrics=available,missing_metrics=missing,
            red_flags=flags,positive_factors=positives,score_breakdown={"profile":profile,**scores,
                "metric_values":{name:raw[name] for name in weights},"renormalized_weights":normalized},
            derived_metrics={name:self._derived(value) for name,value in derived.items()},
            blocks_entry=any(flag.severity is RedFlagSeverity.BLOCKING for flag in flags))

    @staticmethod
    def _score(name,value):
        if value is None:return None
        bands={"revenue_yoy":(-10,25),"gross_margin":(0,.40),"net_margin":(0,.20),"roe":(0,.20),
            "ocf_net_income":(0,1.2),"equity_assets":(.10,.60),"asset_yoy":(-10,25),
            "net_income_yoy":(-10,25),"net_interest_income_yoy":(-10,20),"loan_growth":(-5,20),
            "deposit_growth":(-5,20),"roa":(0,.03)}
        return band(value,*bands[name])
    @staticmethod
    def _valuation_inputs(latest):
        values=[]
        for own,sector,historical in (("pe","sector_pe","historical_pe_median"),("pb","sector_pb","historical_pb_median"),("ev_ebitda","sector_ev_ebitda","historical_ev_ebitda_median")):
            comparators=[getattr(latest,name).value for name in (sector,historical) if getattr(latest,name).value and getattr(latest,name).value>0]
            own_value=getattr(latest,own).value
            if own_value is not None and own_value>0 and comparators:values.append(band(own_value/mean(comparators),.7,1.6,False))
        return values
    @staticmethod
    def _valuation(scores):
        if not scores:return ValuationStatus.UNKNOWN,0
        score=mean(scores);confidence=min(100,len(scores)/3*100)
        return (ValuationStatus.CHEAP if score>=70 else ValuationStatus.FAIR if score>=40 else ValuationStatus.EXPENSIVE if score>=15 else ValuationStatus.EXTREME),round(confidence,4)
    @staticmethod
    def _derived(value):return Metric(value=value,available=value is not None,source="derived")
    @staticmethod
    def _flag(flags,condition,code,explanation):
        if condition:flags.append(RedFlag(code=code,severity=RedFlagSeverity.CAUTION,explanation=explanation))
    @staticmethod
    def _flow_basis(periods,field):
        ordered=sorted(periods,key=lambda period:period.period_end,reverse=True)
        recent=ordered[:4];values=[getattr(period,field).value for period in recent]
        gaps=[(recent[index].period_end-recent[index+1].period_end).days for index in range(len(recent)-1)]
        if len(values)==4 and all(value is not None for value in values) and all(60<=gap<=120 for gap in gaps):
            return sum(values),recent[0]
        annual=next((period for period in ordered if period.period_start is not None
            and (period.period_end-period.period_start).days>=300 and getattr(period,field).value is not None),None)
        return (getattr(annual,field).value,annual) if annual is not None else (None,None)
    @staticmethod
    def _matched_balance(periods,flow_period,field):
        if flow_period is None:return None
        ending=getattr(flow_period,field).value
        if ending is None:return None
        prior=next((period for period in periods if 330<=(flow_period.period_end-period.period_end).days<=400
            and getattr(period,field).value is not None),None)
        return (ending+getattr(prior,field).value)/2 if prior is not None else ending
    @staticmethod
    def _cash_income_basis(periods):
        ordered=sorted(periods,key=lambda period:period.period_end,reverse=True)
        recent=ordered[:4]
        gaps=[(recent[index].period_end-recent[index+1].period_end).days for index in range(len(recent)-1)]
        if len(recent)==4 and all(60<=gap<=120 for gap in gaps):
            income=[period.net_income.value for period in recent]
            cash=[period.operating_cash_flow.value for period in recent]
            if all(value is not None for value in income+cash):return sum(income),sum(cash)
        annual=next((period for period in ordered if period.period_start is not None
            and (period.period_end-period.period_start).days>=300 and period.net_income.value is not None
            and period.operating_cash_flow.value is not None),None)
        return ((annual.net_income.value,annual.operating_cash_flow.value) if annual is not None else (None,None))
    @staticmethod
    def _balance_yoy(periods,field):
        ordered=sorted(periods,key=lambda period:period.period_end,reverse=True)
        available=[period for period in ordered if getattr(period,field).value is not None]
        for latest in available:
            prior=next((period for period in available if 330<=(latest.period_end-period.period_end).days<=400),None)
            if prior is not None:
                return safe_ratio(getattr(latest,field).value,getattr(prior,field).value) * 100 - 100 \
                    if getattr(prior,field).value not in {None,0} else None
        annual=[period for period in available if period.period_start is not None
            and (period.period_end-period.period_start).days>=300]
        if len(annual)>=2 and getattr(annual[1],field).value not in {None,0}:
            return (getattr(annual[0],field).value/getattr(annual[1],field).value-1)*100
        return None
    @staticmethod
    def _cagr(periods,field):
        if len(periods)<8:return None
        newest=getattr(periods[0],field).value;oldest=getattr(periods[7],field).value
        if newest is None or oldest is None or newest<=0 or oldest<=0:return None
        years=(periods[0].period_end-periods[7].period_end).days/365.2425
        return ((newest/oldest)**(1/years)-1)*100 if years>0 else None
