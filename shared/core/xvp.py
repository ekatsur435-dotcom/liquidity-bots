"""
XVP (Extended Volume Profile) v1.0
Расширенный профиль объёма с поиском зон ликвидности.

Логика:
  1. Строим гистограмму объёма по ценовым уровням
  2. Находим POC (Point of Control) - уровень с макс объёмом
  3. Находим VAH/VAL (Value Area High/Low) - зона стоимости
  4. Идентифицируем Single Print - зоны без объёма между профилями
  5. Расширяем диапазон в поисках скрытой ликвидности

Применение:
  - Уровни для входа (подтянуться к POC)
  - Зоны для SL (за Single Print)
  - Цели для TP (VAH/VAL или противоположный Single Print)
  - Фильтр: не входить если цена далеко от POC
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger("xvp")


@dataclass
class XVPConfig:
    """Конфигурация XVP"""
    levels: int = 24                    # Количество ценовых уровней
    value_area_pct: float = 68.0       # Процент объёма в зоне стоимости
    min_volume_spike: float = 2.0     # Минимальный всплеск для значимости
    lookback_candles: int = 50         # Свечей для анализа


@dataclass
class VolumeNode:
    """Узел объёма на ценовом уровне"""
    price_level: float
    volume: float
    bid_volume: float                  # Объём покупок
    ask_volume: float                  # Объём продаж
    trades_count: int


@dataclass
class XVPResult:
    """Результат XVP анализа"""
    poc: float                          # Point of Control (макс объём)
    poc_volume: float
    
    vah: float                        # Value Area High (верх зоны стоимости)
    val: float                        # Value Area Low (низ зоны стоимости)
    
    value_area_volume: float          # Объём в зоне стоимости
    total_volume: float
    
    nodes: List[VolumeNode] = field(default_factory=list)
    
    single_prints_above: List[Tuple[float, float]] = field(default_factory=list)  # (low, high)
    single_prints_below: List[Tuple[float, float]] = field(default_factory=list)
    
    # Ключевые уровни для торговли
    support_levels: List[float] = field(default_factory=list)
    resistance_levels: List[float] = field(default_factory=list)
    
    is_valid: bool = False
    reasons: List[str] = field(default_factory=list)


class XVPAnalyzer:
    """
    Extended Volume Profile Analyzer.
    Строит профиль объёма и находит ключевые уровни.
    """
    
    def __init__(self, config: Optional[XVPConfig] = None):
        self.cfg = config or XVPConfig()
        
    def build_profile(self, candles: List[Dict]) -> List[VolumeNode]:
        """Построить профиль объёма из свечей"""
        if not candles:
            return []
        
        # Определяем диапазон цен
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        
        max_price = max(highs)
        min_price = min(lows)
        price_range = max_price - min_price
        
        if price_range == 0:
            return []
        
        # Создаём уровни
        level_height = price_range / self.cfg.levels
        
        # Агрегируем объёмы по уровням
        volume_by_level = defaultdict(lambda: {"volume": 0, "bid": 0, "ask": 0, "trades": 0})
        
        for candle in candles:
            # Определяем какие уровни пересекает свеча
            low_level = int((candle["low"] - min_price) / level_height)
            high_level = int((candle["high"] - min_price) / level_height)
            
            # Распределяем объём равномерно по уровням свечи
            levels_count = max(1, high_level - low_level + 1)
            vol_per_level = candle.get("volume", 0) / levels_count
            
            # Delta (buy/sell) - используем close vs open как прокси
            delta = 1 if candle["close"] > candle["open"] else -1
            bid_vol = vol_per_level * (0.5 + 0.3 * delta)  # Больше если bullish
            ask_vol = vol_per_level * (0.5 - 0.3 * delta)
            
            for level in range(low_level, high_level + 1):
                if level < 0 or level >= self.cfg.levels:
                    continue
                
                price_level = min_price + (level + 0.5) * level_height
                
                volume_by_level[price_level]["volume"] += vol_per_level
                volume_by_level[price_level]["bid"] += bid_vol
                volume_by_level[price_level]["ask"] += ask_vol
                volume_by_level[price_level]["trades"] += 1
        
        # Создаём ноды
        nodes = []
        for price, data in sorted(volume_by_level.items()):
            nodes.append(VolumeNode(
                price_level=round(price, 8),
                volume=round(data["volume"], 2),
                bid_volume=round(data["bid"], 2),
                ask_volume=round(data["ask"], 2),
                trades_count=data["trades"]
            ))
        
        return nodes
    
    def find_poc(self, nodes: List[VolumeNode]) -> Tuple[float, float]:
        """Найти POC (Point of Control)"""
        if not nodes:
            return 0.0, 0.0
        
        max_vol_node = max(nodes, key=lambda n: n.volume)
        return max_vol_node.price_level, max_vol_node.volume
    
    def find_value_area(self, nodes: List[VolumeNode], poc_price: float) -> Tuple[float, float, float]:
        """Найти Value Area (VAH/VAL)"""
        if not nodes:
            return 0.0, 0.0, 0.0
        
        total_volume = sum(n.volume for n in nodes)
        target_volume = total_volume * (self.cfg.value_area_pct / 100)
        
        # Сортируем узлы по расстоянию от POC
        nodes_by_distance = sorted(nodes, key=lambda n: abs(n.price_level - poc_price))
        
        # Добавляем узлы пока не достигнем целевого объёма
        cumulative_volume = 0
        va_nodes = []
        
        for node in nodes_by_distance:
            if cumulative_volume + node.volume > target_volume:
                break
            cumulative_volume += node.volume
            va_nodes.append(node)
        
        if not va_nodes:
            return poc_price, poc_price, 0.0
        
        prices = [n.price_level for n in va_nodes]
        vah = max(prices)
        val = min(prices)
        
        return vah, val, cumulative_volume
    
    def find_single_prints(self, 
                            nodes: List[VolumeNode], 
                            vah: float, 
                            val: float,
                            max_price: float,
                            min_price: float
                           ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        """Найти зоны Single Print (пустые зоны в профиле)"""
        
        # Выше VAH
        above_nodes = [n for n in nodes if n.price_level > vah]
        single_prints_above = []
        
        if above_nodes:
            prices = sorted([n.price_level for n in above_nodes])
            for i in range(len(prices) - 1):
                gap = prices[i+1] - prices[i]
                if gap > (vah - val) * 0.05:  # Gap > 5% от value area
                    single_prints_above.append((prices[i], prices[i+1]))
        
        # Ниже VAL
        below_nodes = [n for n in nodes if n.price_level < val]
        single_prints_below = []
        
        if below_nodes:
            prices = sorted([n.price_level for n in below_nodes])
            for i in range(len(prices) - 1):
                gap = prices[i+1] - prices[i]
                if gap > (vah - val) * 0.05:
                    single_prints_below.append((prices[i], prices[i+1]))
        
        return single_prints_above, single_prints_below
    
    def find_sr_levels(self, nodes: List[VolumeNode], poc: float) -> Tuple[List[float], List[float]]:
        """Найти уровни поддержки и сопротивления"""
        
        # Сортируем по объёму
        nodes_by_volume = sorted(nodes, key=lambda n: n.volume, reverse=True)
        
        # Берём топ 3 уровня после POC
        top_nodes = [n for n in nodes_by_volume if abs(n.price_level - poc) > poc * 0.01][:3]
        
        support = [n.price_level for n in top_nodes if n.price_level < poc]
        resistance = [n.price_level for n in top_nodes if n.price_level > poc]
        
        return support, resistance
    
    def analyze(self, candles: List[Dict]) -> XVPResult:
        """
        Провести полный XVP анализ.
        
        Args:
            candles: Свечи с полями open, high, low, close, volume
        """
        
        if len(candles) < 10:
            return XVPResult(
                poc=0, poc_volume=0, vah=0, val=0,
                value_area_volume=0, total_volume=0,
                is_valid=False, reasons=["Need at least 10 candles"]
            )
        
        # Используем последние N свечей
        recent_candles = candles[-self.cfg.lookback_candles:]
        
        # Строим профиль
        nodes = self.build_profile(recent_candles)
        
        if not nodes:
            return XVPResult(
                poc=0, poc_volume=0, vah=0, val=0,
                value_area_volume=0, total_volume=0,
                is_valid=False, reasons=["Could not build volume profile"]
            )
        
        # Находим POC
        poc, poc_vol = self.find_poc(nodes)
        
        # Находим Value Area
        vah, val, va_volume = self.find_value_area(nodes, poc)
        
        # Находим Single Prints
        max_price = max(c["high"] for c in recent_candles)
        min_price = min(c["low"] for c in recent_candles)
        
        sp_above, sp_below = self.find_single_prints(nodes, vah, val, max_price, min_price)
        
        # Находим S/R уровни
        support, resistance = self.find_sr_levels(nodes, poc)
        
        total_vol = sum(n.volume for n in nodes)
        
        logger.info(f"[XVP] POC: {poc:.4f} | VA: {val:.4f}-{vah:.4f} | "
                   f"Nodes: {len(nodes)} | SP above: {len(sp_above)} below: {len(sp_below)}")
        
        return XVPResult(
            poc=poc,
            poc_volume=poc_vol,
            vah=vah,
            val=val,
            value_area_volume=va_volume,
            total_volume=total_vol,
            nodes=nodes,
            single_prints_above=sp_above,
            single_prints_below=sp_below,
            support_levels=support,
            resistance_levels=resistance,
            is_valid=True,
            reasons=[f"Profile built from {len(recent_candles)} candles"]
        )


# Singleton instance
_xvp: Optional[XVPAnalyzer] = None

def get_xvp_analyzer(config: Optional[XVPConfig] = None) -> XVPAnalyzer:
    """Get or create XVPAnalyzer singleton"""
    global _xvp
    if _xvp is None:
        _xvp = XVPAnalyzer(config)
    return _xvp
