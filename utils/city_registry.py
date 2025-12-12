from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class City:
    name: str
    lat: float
    lon: float


def _norm(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


# Curated registry (Spain + Asturias focus; extend as needed)
_CITIES: List[City] = [
    City("Oviedo", 43.3614, -5.8593),
    City("Gijón", 43.5322, -5.6611),
    City("Avilés", 43.5561, -5.9248),
    City("Siero", 43.3867, -5.6626),
    City("Villaviciosa", 43.4815, -5.4357),
    City("Llanes", 43.4192, -4.7525),
    City("Cangas de Onís", 43.3510, -5.1264),
    City("Ribadesella", 43.4621, -5.0593),
    City("Cudillero", 43.5627, -6.1453),
    City("Luarca", 43.5427, -6.5360),
    City("Mieres", 43.2482, -5.7780),
    City("Langreo", 43.2967, -5.6914),
    City("Pola de Siero", 43.3920, -5.6634),
    City("Santander", 43.4623, -3.8099),
    City("Bilbao", 43.2630, -2.9350),
    City("A Coruña", 43.3623, -8.4115),
    City("Vigo", 42.2406, -8.7207),
    City("Gasteiz / Vitoria", 42.8467, -2.6716),
    City("Donostia / San Sebastián", 43.3183, -1.9812),
    City("Pamplona", 42.8125, -1.6458),
    City("Logroño", 42.4627, -2.4449),
    City("León", 42.5987, -5.5671),
    City("Burgos", 42.3439, -3.6969),
    City("Valladolid", 41.6523, -4.7245),
    City("Madrid", 40.4168, -3.7038),
    City("Barcelona", 41.3851, 2.1734),
    City("Valencia", 39.4699, -0.3763),
    City("Sevilla", 37.3891, -5.9845),
    City("Málaga", 36.7213, -4.4214),
    City("Granada", 37.1773, -3.5986),
    City("Alicante", 38.3452, -0.4810),
    City("Zaragoza", 41.6488, -0.8891),
    City("Murcia", 37.9922, -1.1307),
    City("Palma", 39.5696, 2.6502),
    City("Las Palmas", 28.1235, -15.4363),
    City("Santa Cruz de Tenerife", 28.4636, -16.2518),
    City("Lisboa", 38.7223, -9.1393),
    City("Porto", 41.1579, -8.6291),
]

_BY_NAME: Dict[str, City] = {_norm(c.name): c for c in _CITIES}


def all_city_names() -> List[str]:
    return sorted((c.name for c in _CITIES), key=lambda s: s.lower())


def resolve_city(name: str) -> Optional[City]:
    if not name:
        return None
    return _BY_NAME.get(_norm(name))


def suggest(query: str, limit: int = 10) -> List[City]:
    q = _norm(query)
    if not q:
        return _CITIES[:limit]

    starts = [c for c in _CITIES if _norm(c.name).startswith(q)]
    if len(starts) >= limit:
        return starts[:limit]

    contains = [c for c in _CITIES if q in _norm(c.name) and c not in starts]
    return (starts + contains)[:limit]

