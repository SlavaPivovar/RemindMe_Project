"""Декодер VIN автомобілів Tesla."""

from __future__ import annotations

from dataclasses import dataclass, field

WMI = {
    "5YJ": "Tesla, Inc. (США, легкові)",
    "7SA": "Tesla, Inc. (США, MPV/SUV)",
    "7G2": "Tesla, Inc. (США, Cybertruck/Semi)",
    "LRW": "Tesla (Китай, Шанхай)",
    "XP7": "Tesla (Німеччина, Берлін)",
    "SFZ": "Tesla Roadster (Велика Британія)",
}

MODELS = {
    "S": ("MODEL_S", "Model S"),
    "3": ("MODEL_3", "Model 3"),
    "X": ("MODEL_X", "Model X"),
    "Y": ("MODEL_Y", "Model Y"),
    "C": ("CYBERTRUCK", "Cybertruck"),
    "R": ("ROADSTER", "Roadster"),
    "T": ("SEMI", "Semi"),
}

PLANTS = {
    "F": "Фрімонт, Каліфорнія (США)",
    "A": "Остін, Техас (США)",
    "P": "Пало-Альто, Каліфорнія (США)",
    "N": "Ріно, Невада (США)",
    "C": "Шанхай (Китай)",
    "B": "Берлін (Німеччина)",
}

# Код модельного року (10-та позиція), цикл 30 років; для Tesla беремо 2010–2039.
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"
YEARS = {c: 2010 + i for i, c in enumerate(_YEAR_CODES)}

_TRANSLIT = {
    **{str(d): d for d in range(10)},
    **dict(zip("ABCDEFGH", range(1, 9))),
    **dict(zip("JKLMN", range(1, 6))),
    "P": 7, "R": 9,
    **dict(zip("STUVWXYZ", range(2, 10))),
}
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


@dataclass
class VinInfo:
    vin: str
    valid: bool
    manufacturer: str | None = None
    model_code: str | None = None
    model_name: str | None = None
    year: int | None = None
    plant: str | None = None
    serial: str | None = None
    warnings: list[str] = field(default_factory=list)


def check_digit(vin: str) -> str:
    total = sum(_TRANSLIT[ch] * w for ch, w in zip(vin, _WEIGHTS))
    rem = total % 11
    return "X" if rem == 10 else str(rem)


def decode_vin(raw: str) -> VinInfo:
    vin = raw.strip().upper().replace(" ", "")
    info = VinInfo(vin=vin, valid=False)
    if len(vin) != 17:
        info.warnings.append(f"VIN має містити 17 символів, отримано {len(vin)}")
        return info
    bad = sorted({ch for ch in vin if ch not in _TRANSLIT})
    if bad:
        info.warnings.append(f"Неприпустимі символи у VIN: {', '.join(bad)} (літери I, O, Q не використовуються)")
        return info

    info.manufacturer = WMI.get(vin[:3])
    if info.manufacturer is None:
        info.warnings.append(f"WMI «{vin[:3]}» не належить Tesla")
    model = MODELS.get(vin[3])
    if model:
        info.model_code, info.model_name = model
    else:
        info.warnings.append(f"Невідомий код моделі «{vin[3]}» (4-та позиція)")
    info.year = YEARS.get(vin[9])
    if info.year is None:
        info.warnings.append(f"Невідомий код модельного року «{vin[9]}»")
    info.plant = PLANTS.get(vin[10])
    if info.plant is None:
        info.warnings.append(f"Невідомий код заводу «{vin[10]}»")
    info.serial = vin[11:]

    # Контрольна цифра обов'язкова для ринку Північної Америки; у китайських і
    # європейських VIN на 9-й позиції може стояти інший символ.
    if vin[:3] in ("5YJ", "7SA", "7G2") and check_digit(vin) != vin[8]:
        info.warnings.append(
            f"Контрольна цифра не збігається (очікувалась «{check_digit(vin)}», у VIN «{vin[8]}») — перевірте VIN"
        )

    info.valid = info.manufacturer is not None and info.model_code is not None
    return info
