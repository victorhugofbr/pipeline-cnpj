"""Leitura em blocos dos .zip da Receita, sem estourar a memória.

Como funciona (pense numa esteira de mudança):
1. O .zip é aberto como um "cano": os dados são descompactados aos poucos, só quando pedidos.
2. Pegamos N linhas por vez (um "bloco"), traduzimos de Latin-1 e entregamos ao Polars.
3. O bloco é processado e descartado antes de o próximo chegar. A memória usada fica
   sempre do tamanho de UM bloco, não importa se o arquivo tem 1 mil ou 70 milhões de linhas.

Resiliência:
* Caminho rápido: o Polars interpreta o bloco inteiro de uma vez.
* Caminho de socorro: se o Polars falhar (ex.: aspas soltas no meio de um nome), o bloco é
  reinterpretado linha a linha; as linhas impossíveis de ler são separadas, não derrubam a carga.
* Disjuntor: se um bloco tiver linhas quebradas demais, paramos tudo. Isso indica arquivo
  corrompido ou layout novo, e carregar "no escuro" seria pior do que parar.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

import polars as pl

from layouts import ENCODING_RECEITA, LAYOUTS, SEPARADOR, tipo_pelo_nome

# Quantas linhas extras podemos "puxar" para não cortar um bloco no meio de um campo
# entre aspas que contenha uma quebra de linha.
MAX_LINHAS_EXTRAS = 20

# Disjuntor: proporção máxima de linhas quebradas aceitável num bloco (1%).
LIMITE_MALFORMADAS = 0.01


class LayoutDivergenteError(Exception):
    """O arquivo tem um número de colunas diferente do layout oficial."""


class ArquivoSuspeitoError(Exception):
    """Linhas quebradas demais: arquivo corrompido ou com formato inesperado."""


@dataclass(frozen=True)
class Bloco:
    numero: int
    dados: pl.DataFrame  # linhas interpretadas com sucesso (tudo texto, colunas do layout)
    malformadas: list[str]  # linhas originais que não foi possível interpretar

    @property
    def total_linhas(self) -> int:
        return self.dados.height + len(self.malformadas)


def ler_blocos(caminho_zip: Path, linhas_por_bloco: int) -> Iterator[Bloco]:
    """Entrega o conteúdo do .zip em blocos de até `linhas_por_bloco` linhas."""
    tipo = tipo_pelo_nome(caminho_zip)
    colunas = LAYOUTS[tipo]

    with zipfile.ZipFile(caminho_zip) as arquivo_zip:
        membros = [m for m in arquivo_zip.infolist() if not m.is_dir()]
        if len(membros) != 1:
            raise ValueError(f"Esperava 1 arquivo dentro de {caminho_zip.name}, há {len(membros)}.")

        with arquivo_zip.open(membros[0]) as bytes_brutos:
            texto = io.TextIOWrapper(bytes_brutos, encoding=ENCODING_RECEITA)
            numero = 0
            while True:
                conteudo = _proximo_bloco(texto, linhas_por_bloco)
                if not conteudo:
                    return  # fim do arquivo
                numero += 1
                dados, malformadas = _interpretar(conteudo, colunas, primeiro=numero == 1)
                _verificar_disjuntor(caminho_zip, numero, dados.height, len(malformadas))
                yield Bloco(numero, dados, malformadas)


def _proximo_bloco(texto: io.TextIOWrapper, linhas: int) -> str:
    """Junta as próximas `linhas` linhas num único texto, sem cortar um campo ao meio."""
    partes = list(islice(texto, linhas))
    if not partes:
        return ""
    conteudo = "".join(partes)

    # Se o número de aspas é ÍMPAR, algum campo entre aspas ainda não terminou (tinha uma
    # quebra de linha dentro). Puxamos mais linhas até fechar o campo.
    extras = 0
    while conteudo.count('"') % 2 == 1 and extras < MAX_LINHAS_EXTRAS:
        proxima = next(texto, "")
        if not proxima:
            break
        conteudo += proxima
        extras += 1

    # O caractere NULO (\x00) é proibido em textos do PostgreSQL. Se algum registro trouxer
    # esse caractere, removê-lo aqui custa quase nada e evita um erro no meio da carga.
    return conteudo.replace("\x00", "")


def _interpretar(
    conteudo: str, colunas: tuple[str, ...], primeiro: bool
) -> tuple[pl.DataFrame, list[str]]:
    """Transforma o texto do bloco em tabela. Tenta o caminho rápido; se falhar, o de socorro."""
    try:
        tabela = pl.read_csv(
            io.BytesIO(conteudo.encode("utf-8")),
            has_header=False,
            separator=SEPARADOR,
            quote_char='"',
            infer_schema=False,  # tudo como texto: converter tipos é trabalho do transformar.py
        )
    except pl.exceptions.PolarsError:
        return _interpretar_linha_a_linha(conteudo, colunas)

    if tabela.width == len(colunas):
        tabela = tabela.rename(dict(zip(tabela.columns, colunas, strict=True)))
        # A Receita coloca TODOS os campos entre aspas, até os vazios (""), que o Polars lê como
        # texto vazio. Um NULL na última coluna significa que a linha veio CURTA (faltam campos)
        # e o Polars completou sozinho. Para não aceitar dados incompletos, vamos linha a linha.
        if tabela[colunas[-1]].null_count() > 0:
            return _interpretar_linha_a_linha(conteudo, colunas)
        return tabela, []

    # Número de colunas errado logo no PRIMEIRO bloco: o layout mudou. Paramos aqui.
    if primeiro:
        raise LayoutDivergenteError(
            f"O arquivo tem {tabela.width} colunas, mas o layout prevê {len(colunas)}. "
            "A Receita pode ter mudado o formato."
        )
    # Em blocos seguintes, provavelmente há linhas quebradas: vamos linha a linha.
    return _interpretar_linha_a_linha(conteudo, colunas)


def _interpretar_linha_a_linha(
    conteudo: str, colunas: tuple[str, ...]
) -> tuple[pl.DataFrame, list[str]]:
    """Caminho de socorro: mais lento, mas tolerante a aspas soltas. Separa o que não tem jeito."""
    validas: list[list[str]] = []
    malformadas: list[str] = []
    # O leitor de CSV do próprio Python entende campos com quebra de linha e é tolerante
    # com aspas soltas no meio de um texto (ex.: PADARIA "BOM" PAO).
    leitor = csv.reader(io.StringIO(conteudo), delimiter=SEPARADOR, quotechar='"')
    while True:
        try:
            campos = next(leitor)
        except StopIteration:
            break
        except csv.Error as erro:
            malformadas.append(f"[erro de leitura: {erro}]")
            continue
        if not campos:
            continue  # linha em branco
        if len(campos) == len(colunas):
            validas.append(campos)
        else:
            # Guardamos a linha (reconstruída) para investigação.
            malformadas.append(SEPARADOR.join(campos))

    tabela = pl.DataFrame(validas, schema=dict.fromkeys(colunas, pl.String), orient="row")
    return tabela, malformadas


def _verificar_disjuntor(caminho_zip: Path, numero: int, boas: int, quebradas: int) -> None:
    total = boas + quebradas
    if quebradas > max(10, total * LIMITE_MALFORMADAS):
        raise ArquivoSuspeitoError(
            f"{caminho_zip.name}, bloco {numero}: {quebradas:,} de {total:,} linhas não puderam "
            "ser interpretadas. O arquivo pode estar corrompido ou com layout novo."
        )