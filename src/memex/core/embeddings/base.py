"""Interfaz Embedder abstracta.

Todo lo que entre al pipeline de retrieval pasa por un `Embedder`. Esta
interfaz permite cambiar de modelo (Ollama local, API remota, fake en tests)
sin tocar el resto del core.

Cualquier implementación nueva debe:
- Exponer `dim` (dimensión del vector que produce; debe coincidir con el de
  la tabla `vec_chunks`).
- Exponer `model_name` (string informativo para logging y schema_meta).
- Implementar `embed(texts)` que devuelva una lista paralela de vectores.

Convención: los vectores se devuelven L2-normalizados cuando es posible,
para que la distancia L2 de sqlite-vec coincida con el ranking por similitud
coseno. Las implementaciones pueden saltearse la normalización si su modelo
ya devuelve unit vectors (raro), o si el llamador la desactiva explícitamente.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence


class Embedder(ABC):
    """Convierte textos a vectores de dimensión fija."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Dimensión del vector. Constante para una instancia dada."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identificador del modelo. Usado en logging y stats."""

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embebe una secuencia de textos. Devuelve una lista paralela.

        - Para `texts = []` debe devolver `[]`.
        - El largo de la lista resultante debe ser igual al de `texts`.
        - Cada vector debe tener largo `self.dim`.
        """

    def embed_one(self, text: str) -> list[float]:
        """Atajo: embebe un solo texto."""
        return self.embed([text])[0]


def l2_normalize(vec: Sequence[float]) -> list[float]:
    """Devuelve el vector escalado para que su norma L2 sea 1.

    Si el vector es todo ceros (norma 0), lo devuelve tal cual.
    """
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return list(vec)
    return [x / norm for x in vec]
