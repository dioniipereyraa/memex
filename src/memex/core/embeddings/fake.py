"""Embedder determinístico para tests.

`FakeEmbedder` produce vectores derivados del hash del texto. Mismo texto siempre
devuelve el mismo vector. No tiene relación semántica con el contenido (textos
parecidos NO producen vectores parecidos), pero es suficiente para:

- Tests que prueban el pipeline de ingest sin hablar con Ollama.
- Tests del repo que necesitan embeddings válidos pero no realistas.
- Smoke tests rápidos sin depender de un servicio externo.

NO usar en producción. La distancia L2 entre vectores generados acá no tiene
significado semántico, así que el retrieval va a ser basura.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence

from memex.core.embeddings.base import Embedder, l2_normalize


class FakeEmbedder(Embedder):
    """Embedder determinístico basado en hash SHA-256 del texto.

    Cada texto se hashea, los bytes se interpretan como floats, y el vector
    se recorta o rellena hasta `dim`. Después se L2-normaliza.
    """

    def __init__(self, dim: int = 768, model_name: str = "fake") -> None:
        if dim <= 0:
            raise ValueError("dim debe ser > 0")
        self._dim = dim
        self._model_name = model_name

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._fake_vector(t) for t in texts]

    def _fake_vector(self, text: str) -> list[float]:
        # Generar suficientes bytes: cada float ocupa 4 bytes, necesitamos `dim` floats.
        # SHA-256 da 32 bytes. Iteramos hashes con sufijos numéricos hasta llenar.
        needed_bytes = self._dim * 4
        buf = bytearray()
        counter = 0
        while len(buf) < needed_bytes:
            h = hashlib.sha256(f"{text}|{counter}".encode()).digest()
            buf.extend(h)
            counter += 1
        # Convertir bytes a floats en [-1, 1].
        floats: list[float] = []
        for i in range(self._dim):
            # struct.unpack devuelve int32; lo normalizamos al rango [-1, 1].
            (raw,) = struct.unpack_from(">i", buf, i * 4)
            floats.append(raw / 2_147_483_647.0)
        return l2_normalize(floats)
