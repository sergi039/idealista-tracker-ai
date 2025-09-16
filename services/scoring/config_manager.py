import yaml
import os
from typing import Dict, Any
from dataclasses import dataclass

@dataclass
class ScoringThreshold:
    min_value: float
    max_value: float
    score: float
    
@dataclass
class InvestmentYieldConfig:
    excellent: ScoringThreshold  # 6.0%+
    good: ScoringThreshold       # 4.0-6.0%
    fair: ScoringThreshold       # 2.0-4.0%
    poor: ScoringThreshold       # 0-2.0%

class ScoringConfigManager:
    """Manages scoring configuration and thresholds"""
    
    def __init__(self):
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load scoring configuration from files or defaults"""
        try:
            config_path = os.path.join('config', 'scoring_rules.yml')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    return yaml.safe_load(f)
        except Exception:
            pass
        
        # Default configuration
        return {
            'investment_yield': {
                'excellent': {'min': 6.0, 'max': 100.0, 'base_score': 90, 'multiplier': 2.5},
                'good': {'min': 4.0, 'max': 6.0, 'score': 75},
                'fair': {'min': 2.0, 'max': 4.0, 'score': 50},
                'poor': {'min': 0.0, 'max': 2.0, 'score': 20}
            },
            'distance_scoring': {
                'excellent': {'max_distance': 2000, 'multiplier': 1.0},
                'good': {'max_distance': 5000, 'multiplier': 0.7},
                'fair': {'max_distance': 10000, 'multiplier': 0.4},
                'poor': {'max_distance': float('inf'), 'multiplier': 0.2}
            },
            'infrastructure_keywords': {
                'electricity': ['electricidad', 'luz', 'eléctrico', 'corriente'],
                'water': ['agua', 'suministro agua', 'abastecimiento', 'red agua'],
                'internet': ['internet', 'fibra', 'adsl', 'wifi', 'banda ancha'],
                'gas': ['gas', 'butano', 'propano', 'gas natural']
            }
        }
    
    def get_investment_yield_score(self, yield_value: float) -> float:
        """Calculate investment yield score based on configuration"""
        config = self.config['investment_yield']
        
        if yield_value >= config['excellent']['min']:
            base = config['excellent']['base_score']
            multiplier = config['excellent']['multiplier']
            return min(100, base + (yield_value - config['excellent']['min']) * multiplier)
        elif yield_value >= config['good']['min']:
            return config['good']['score']
        elif yield_value >= config['fair']['min']:
            return config['fair']['score']
        else:
            return config['poor']['score']
    
    def get_distance_score(self, distance: float, max_points: float) -> float:
        """Calculate distance-based score"""
        config = self.config['distance_scoring']
        
        for level, params in config.items():
            if distance <= params['max_distance']:
                return max_points * params['multiplier']
        
        return max_points * config['poor']['multiplier']
    
    def get_infrastructure_keywords(self, utility: str) -> list:
        """Get keywords for infrastructure detection"""
        return self.config['infrastructure_keywords'].get(utility, [])