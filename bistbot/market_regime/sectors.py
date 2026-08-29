SECTOR_SYMBOLS = {
    "airlines":{"THYAO.IS","PGSUS.IS"}, "transport":{"RYSAS.IS","TLMAN.IS"},
    "energy":{"TUPRS.IS","AYGAZ.IS","PETKM.IS"}, "defense":{"ASELS.IS","OTKAR.IS"},
    "banks":{"AKBNK.IS","GARAN.IS","ISCTR.IS","YKBNK.IS","HALKB.IS","VAKBN.IS"},
    "exporters":{"FROTO.IS","TOASO.IS","ARCLK.IS","VESBE.IS"},
    "importers":{"BIMAS.IS","MGROS.IS"},
}

def sector_for_symbol(symbol: str) -> str | None:
    normalized=symbol.upper() if symbol.upper().endswith(".IS") else symbol.upper()+".IS"
    return next((sector for sector,symbols in SECTOR_SYMBOLS.items() if normalized in symbols),None)
