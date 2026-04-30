"""
Sector Mapper — маппинг символов в секторы для correlation filter
"""

SECTOR_MAP = {
    # DeFi
    "AAVEUSDT": "DeFi", "UNIUSDT": "DeFi", "MKRUSDT": "DeFi", "LDOUSDT": "DeFi",
    "CRVUSDT": "DeFi", "SNXUSDT": "DeFi", "COMPUSDT": "DeFi", "YFIUSDT": "DeFi",
    "SUSHIUSDT": "DeFi", "1INCHUSDT": "DeFi", "DYDXUSDT": "DeFi", "GMXUSDT": "DeFi",
    "PERPUSDT": "DeFi", "APEXUSDT": "DeFi", "RDNTUSDT": "DeFi", "VELOUSDT": "DeFi",
    "CVXUSDT": "DeFi", "FXSUSDT": "DeFi", "PENDLEUSDT": "DeFi", "RSRUSDT": "DeFi",
    "UMAUSDT": "DeFi", "BALUSDT": "DeFi", "FISUSDT": "DeFi", "LQTYUSDT": "DeFi",
    "RPLUSDT": "DeFi", "SSVUSDT": "DeFi", "FARMUSDT": "DeFi", "DPIUSDT": "DeFi",
    "BADGERUSDT": "DeFi", "TRIBEUSDT": "DeFi", "RENUSDT": "DeFi", "SWRVUSDT": "DeFi",
    
    # GameFi / Metaverse
    "AXSUSDT": "GameFi", "SANDUSDT": "GameFi", "MANAUSDT": "GameFi", "GALAUSDT": "GameFi",
    "ENJUSDT": "GameFi", "ILVUSDT": "GameFi", "YGGUSDT": "GameFi", "MAGICUSDT": "GameFi",
    "GMTUSDT": "GameFi", "IMXUSDT": "GameFi", "PYRUSDT": "GameFi", "WAXPUSDT": "GameFi",
    "ALICEUSDT": "GameFi", "TLMUSDT": "GameFi", "MCUSDT": "GameFi", "DGUSDT": "GameFi",
    "BIGHITUSDT": "GameFi", "SLPUSDT": "GameFi", "STARLUSDT": "GameFi", "MBOXUSDT": "GameFi",
    "DARUSDT": "GameFi", "DPETUSDT": "GameFi", "NAKAUSDT": "GameFi", "GHSTUSDT": "GameFi",
    "RAREUSDT": "GameFi", "ERNUSDT": "GameFi", "SINUSDT": "GameFi", "ATLASUSDT": "GameFi",
    "POLISUSDT": "GameFi", "CUBEUSDT": "GameFi", "BLOKUSDT": "GameFi", "REVVUSDT": "GameFi",
    
    # L1 Blockchains
    "ETHUSDT": "L1", "BTCUSDT": "L1", "BNBUSDT": "L1", "SOLUSDT": "L1",
    "ADAUSDT": "L1", "AVAXUSDT": "L1", "DOTUSDT": "L1", "MATICUSDT": "L1",
    "ARBUSDT": "L1", "OPUSDT": "L1", "NEARUSDT": "L1", "APTUSDT": "L1",
    "SUIUSDT": "L1", "SEIUSDT": "L1", "TIAUSDT": "L1", "INJUSDT": "L1",
    "FTMUSDT": "L1", "ALGOUSDT": "L1", "EOSUSDT": "L1", "XTZUSDT": "L1",
    "ATOMUSDT": "L1", "LUNAUSDT": "L1", "ICPUSDT": "L1", "KDAUSDT": "L1",
    "ROSEUSDT": "L1", "ONEUSDT": "L1", "ZILUSDT": "L1", "EGLDUSDT": "L1",
    "FETUSDT": "L1", "STXUSDT": "L1", "KASUSDT": "L1", "TONUSDT": "L1",
    "TRXUSDT": "L1", "NEOUSDT": "L1", "VETUSDT": "L1", "QTUMUSDT": "L1",
    "ONTUSDT": "L1", "ZENUSDT": "L1", "DASHUSDT": "L1", "XEMUSDT": "L1",
    "IOSTUSDT": "L1", "LUNCUSDT": "L1", "LSKUSDT": "L1", "WANUSDT": "L1",
    "KAVAUSDT": "L1", "CELOUSDT": "L1", "GLMRUSDT": "L1", "MOVRUSDT": "L1",
    "ASTRUSDT": "L1", "ACAUSDT": "L1", "NODLEUSDT": "L1", "OASUSDT": "L1",
    "AGORICUSDT": "L1", "SCRTUSDT": "L1", "DYDXUSDT": "L1", "METISUSDT": "L1",
    "RONUSDT": "L1", "BEAMUSDT": "L1", "XRDUSDT": "L1", "VLXUSDT": "L1",
    "FUSEUSDT": "L1", "CUDOSUSDT": "L1", "HUAUSDT": "L1", "BRISEUSDT": "L1",
    
    # Layer 2
    "OPUSDT": "L2", "ARBUSDT": "L2", "ZKUSDT": "L2", "STRKUSDT": "L2",
    "MNTUSDT": "L2", "IMXUSDT": "L2", "LOOMUSDT": "L2", "METISUSDT": "L2",
    "BOBAUSDT": "L2", "ZKSUSDT": "L2", "LRCUSDT": "L2", "OMGUSDT": "L2",
    "STORJUSDT": "L2", "CTSIUSDT": "L2", "SKLUSDT": "L2", "PONDUSDT": "L2",
    
    # AI / Big Data
    "FETUSDT": "AI", "AGIXUSDT": "AI", "OCEANUSDT": "AI", "RLCUSDT": "AI",
    "PHBUSDT": "AI", "NCDTUSDT": "AI", "AIUSDT": "AI", "AITUSDT": "AI",
    "AGIUSDT": "AI", "BOTUSDT": "AI", "DHXUSDT": "AI", "ORAIUSDT": "AI",
    "VAIUSDT": "AI", "DEAIUSDT": "AI", "DEVAIUSDT": "AI", "CGPTUSDT": "AI",
    "AI16ZUSDT": "AI", "AIOZUSDT": "AI", "RENDERUSDT": "AI", "TNSRUSDT": "AI",
    "ARKMUSDT": "AI", "WLDUSDT": "AI", "NEARUSDT": "AI", "GLMUSDT": "AI",
    "VANAUSDT": "AI", "COMAIUSDT": "AI", "PIPPINUSDT": "AI", "AVAIUSDT": "AI",
    "AIQUSDT": "AI", "AICUSDT": "AI", "NEIUSDT": "AI", "ZEREBROUSDT": "AI",
    
    # Meme
    "DOGEUSDT": "Meme", "SHIBUSDT": "Meme", "PEPEUSDT": "Meme", "FLOKIUSDT": "Meme",
    "BONKUSDT": "Meme", "WIFUSDT": "Meme", "BOMEUSDT": "Meme", "BRETTUSDT": "Meme",
    "WENUSDT": "Meme", "POPCATUSDT": "Meme", "MOGUSDT": "Meme", "PENGUUSDT": "Meme",
    "PnutUSDT": "Meme", "CHILLGUYUSDT": "Meme", "FARTCOINUSDT": "Meme", "HIMAUSDT": "Meme",
    "LADYSUSDT": "Meme", "PEOPLEUSDT": "Meme", "TURBOUSDT": "Meme", "TRUMPUSDT": "Meme",
    "MELANIAUSDT": "Meme", "MAGAUSDT": "Meme", "PEPECOINUSDT": "Meme", "COQUSDT": "Meme",
    "HARRYBETUSDT": "Meme", "GIGAUSDT": "Meme", "SPXUSDT": "Meme", "APUUSDT": "Meme",
    "SHIROUSDT": "Meme", "MUBARAKUSDT": "Meme", "DEGENUSDT": "Meme", "SLERFUSDT": "Meme",
    "MILADYUSDT": "Meme", "ELONUSDT": "Meme", "KISHUUSDT": "Meme", "AKITAUSDT": "Meme",
    
    # RWA / Tokenization
    "ONDOUSDT": "RWA", "CFGUSDT": "RWA", "POLYXUSDT": "RWA", "RIOUSDT": "RWA",
    "LEOXUSDT": "RWA", "TRBUSDT": "RWA", "LABSUSDT": "RWA", "RBTUSDT": "RWA",
    "PROPCUSDT": "RWA", "ELANDUSDT": "RWA", "BSPTUSDT": "RWA", "MIRUSDT": "RWA",
    "SNOWUSDT": "RWA", "UPOUSDT": "RWA", "JETUSDT": "RWA", "AVAXUSDT": "RWA",
    "LINKUSDT": "RWA", "AAVEUSDT": "RWA", "MKRUSDT": "RWA", "UNIUSDT": "RWA",
    "LIDOQUSDT": "RWA", "STETHUSDT": "RWA", "ANKRUSDT": "RWA", "RPLUSDT": "RWA",
    
    # Infrastructure / Oracles
    "LINKUSDT": "Oracle", "BANDUSDT": "Oracle", "API3USDT": "Oracle", "DIAUSDT": "Oracle",
    "TRBUSDT": "Oracle", "PythUSDT": "Oracle", "WINUSDT": "Oracle", "SUPRAUSDT": "Oracle",
    "PYTHUSDT": "Oracle", "DONUSDT": "Oracle", "CHZUSDT": "Oracle", "ORNUSDT": "Oracle",
    "MODUSDT": "Oracle", "REDUSDT": "Oracle", "LITHUSDT": "Oracle", "REEFUSDT": "Oracle",
    "ANKRUSDT": "Infra", "GRTUSDT": "Infra", "LPTUSDT": "Infra", "BATUSDT": "Infra",
    "STORJUSDT": "Infra", "ARUSDT": "Infra", "FILUSDT": "Infra", "SCUSDT": "Infra",
    "HNTUSDT": "Infra", "IOTXUSDT": "Infra", "NUUSDT": "Infra", "KEEPUSDT": "Infra",
    "CVCUSDT": "Infra", "OCEANUSDT": "Infra", "SYSUSDT": "Infra", "AIOZUSDT": "Infra",
    "THETAUSDT": "Infra", "TFUELUSDT": "Infra", "BTTUSDT": "Infra", "HOTUSDT": "Infra",
    "DENTUSDT": "Infra", "RSRUSDT": "Infra", "REQUSDT": "Infra", "SNTUSDT": "Infra",
    
    # Exchange / CeFi
    "BNBUSDT": "Exchange", "OKBUSDT": "Exchange", "GTUSDT": "Exchange", "KCSUSDT": "Exchange",
    "HTUSDT": "Exchange", "CROUSDT": "Exchange", "FTTUSDT": "Exchange", "LEOUSDT": "Exchange",
    "MXUSDT": "Exchange", "BGBUSDT": "Exchange", "BESTUSDT": "Exchange", "TUSDUSDT": "Stable",
    
    # Stablecoins
    "USDTUSDT": "Stable", "USDCUSDT": "Stable", "DAIUSDT": "Stable", "BUSDUSDT": "Stable",
    "TUSDUSDT": "Stable", "USDPUSDT": "Stable", "USDDUSDT": "Stable", "FDUSDUSDT": "Stable",
    "GUSDUSDT": "Stable", "SUSDUSDT": "Stable", "LUSDUSDT": "Stable", "EURSUSDT": "Stable",
    
    # Privacy
    "XMRUSDT": "Privacy", "ZECUSDT": "Privacy", "DASHUSDT": "Privacy", "SCRTUSDT": "Privacy",
    "BEAMUSDT": "Privacy", "GRINUSDT": "Privacy", "SUSDT": "Privacy", "PRIVUSDT": "Privacy",
    "OXENUSDT": "Privacy", "ARRRUSDT": "Privacy", "XVGUSDT": "Privacy", "PIVXUSDT": "Privacy",
    "FIROUSDT": "Privacy", "ZENUSDT": "Privacy", "NAVUSDT": "Privacy", "MASKUSDT": "Privacy",
    
    # Payments
    "XRPUSDT": "Payment", "LTCUSDT": "Payment", "XLMUSDT": "Payment", "XNOUSDT": "Payment",
    "ACHUSDT": "Payment", "CELUSDT": "Payment", "AMPUSDT": "Payment", "PUNDIXUSDT": "Payment",
    "COTIUSDT": "Payment", "STMXUSDT": "Payment", "UMAUSDT": "Payment", "NEXOUSDT": "Payment",
    "CrypteriumUSDT": "Payment", "UTKUSDT": "Payment", "MGOUSDT": "Payment", "SXPUSDT": "Payment",
}


def get_sector(symbol: str) -> str:
    """Получить сектор для символа (или 'Other' если неизвестен)"""
    norm = symbol.upper().replace("-", "").replace("_", "")
    # Убираем USDT/USDC/BUSD для поиска
    base = norm.replace("USDT", "").replace("USDC", "").replace("BUSD", "")
    # Ищем точное совпадение с суффиксом
    if norm in SECTOR_MAP:
        return SECTOR_MAP[norm]
    # Ищем без суффикса
    for suffix in ["USDT", "USDC", "BUSD"]:
        key = base + suffix
        if key in SECTOR_MAP:
            return SECTOR_MAP[key]
    return "Other"


def count_positions_by_sector(positions: list, sector: str) -> int:
    """Подсчитать количество позиций в секторе"""
    count = 0
    for pos in positions:
        sym = pos.get("symbol", "")
        if get_sector(sym) == sector:
            count += 1
    return count
