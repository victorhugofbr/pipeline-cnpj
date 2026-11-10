r"""Prova real: aplica a transformação numa amostra de um .zip da Receita e mostra o resultado.

Uso (com o ambiente virtual ativado, dentro da pasta etl):
    python provar_transformacao.py ..\data\raw\2026-10\Estabelecimentos1.zip
    python provar_transformacao.py ..\data\raw\2026-10\Empresas1.zip --linhas 200000
"""

from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

import polars as pl

from explorar_amostra import LINHAS_PADRAO, LayoutDivergenteError, ler_amostra
from transformar import transformar


def main() -> int:
    parser = argparse.ArgumentParser(description="Testa a transformação numa amostra real.")
    parser.add_argument("arquivo", type=Path, help="Caminho do .zip (ex.: Empresas1.zip)")
    parser.add_argument("--linhas", type=int, default=LINHAS_PADRAO, help="Linhas a ler")
    args = parser.parse_args()

    if not args.arquivo.is_file():
        print(f"ERRO: arquivo não encontrado: {args.arquivo}", file=sys.stderr)
        return 1

    try:
        tipo, bruto = ler_amostra(args.arquivo, args.linhas)
    except (zipfile.BadZipFile, ValueError, LayoutDivergenteError, pl.exceptions.PolarsError) as e:
        print(f"ERRO ao ler a amostra: {e}", file=sys.stderr)
        return 1

    # Medimos SÓ a transformação, para saber quanto ela custa por linha.
    inicio = time.perf_counter()
    resultado = transformar(tipo, bruto)
    duracao = time.perf_counter() - inicio
    por_segundo = bruto.height / duracao if duracao > 0 else float("inf")

    print(f"Arquivo: {args.arquivo.name} | tipo: {tipo}")
    print(
        f"Linhas: {bruto.height:,} | válidas: {resultado.validos.height:,} | "
        f"rejeitadas: {resultado.rejeitados.height:,}"
    )
    print(f"Tempo da transformação: {duracao:.3f}s (~{por_segundo:,.0f} linhas por segundo)")

    print("\n=== Tipos depois da transformação (compare com 001_tabelas_rfb.sql) ===")
    for nome, tipo_polars in resultado.validos.schema.items():
        print(f"  {nome:<34} {tipo_polars}")

    print("\n=== Valores inválidos que viraram NULL (por coluna) ===")
    if resultado.anulados:
        for nome, total in sorted(resultado.anulados.items(), key=lambda item: -item[1]):
            print(f"  {nome:<34} {total:,}")
    else:
        print("  nenhum")

    if resultado.rejeitados.height:
        print("\n=== Motivos de rejeição ===")
        print(resultado.rejeitados["motivo_rejeicao"].value_counts(sort=True))

    return 0


if __name__ == "__main__":
    sys.exit(main())