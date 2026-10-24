"""Декодер VIN автомобилей Tesla."""

from __future__ import annotations

from dataclasses import dataclass, field

WMI = {
    "5YJ": "Tesla, Inc. (США, легковые)",
    "7SA": "Tesla, Inc. (США, MPV/SUV)",
    "7G2": "Tesla, Inc. (США, Cybertruck/Semi)",
    "LRW": "Tesla (Китай, Шанхай)",
    "XP7": "Tesla (Германия, Берлин)",
    "SFZ": "Tesla Roadster (Великобритания)",
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
    "F": "Фримонт, Калифорния (США)",
    "A": "Остин, Техас (США)",
    "P": "Пало-Альто, Калифорния (США)",
    "N": "Рино, Невада (США)",
    "C": "Шанхай (Китай)",
    "B": "Берлин (Германия)",
}

# Код модельного года (10-я позиция), цикл 30 лет; для Tesla берём 2010–2039.
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
        info.warnings.append(f"VIN должен содержать 17 символов, получено {len(vin)}")
        return info
    bad = sorted({ch for ch in vin if ch not in _TRANSLIT})
    if bad:
        info.warnings.append(f"Недопустимые символы в VIN: {', '.join(bad)} (буквы I, O, Q не используются)")
        return info

    info.manufacturer = WMI.get(vin[:3])
    if info.manufacturer is None:
        info.warnings.append(f"WMI «{vin[:3]}» не относится к Tesla")
    model = MODELS.get(vin[3])
    if model:
        info.model_code, info.model_name = model
    else:
        info.warnings.append(f"Неизвестный код модели «{vin[3]}» (4-я позиция)")
    info.year = YEARS.get(vin[9])
    if info.year is None:
        info.warnings.append(f"Неизвестный код модельного года «{vin[9]}»")
    info.plant = PLANTS.get(vin[10])
    if info.plant is None:
        info.warnings.append(f"Неизвестный код завода «{vin[10]}»")
    info.serial = vin[11:]

    # Контрольная цифра обязательна для рынка Северной Америки; у китайских и
    # европейских VIN на 9-й позиции может стоять другой символ.
    if vin[:3] in ("5YJ", "7SA", "7G2") and check_digit(vin) != vin[8]:
        info.warnings.append(
            f"Контрольная цифра не совпадает (ожидалась «{check_digit(vin)}», в VIN «{vin[8]}») — проверьте VIN"
        )

    info.valid = info.manufacturer is not None and info.model_code is not None
    return info
