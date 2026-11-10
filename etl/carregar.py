r"""Carregador: lê os .zip da Receita em blocos, transforma e envia ao PostgreSQL com COPY.

Uso (com o ambiente virtual ativado, dentro da pasta etl):
    python carregar.py ..\data\raw\2026-10
    python carregar.py ..\data\raw\2026-10 --tipos cnaes municipios
    python carregar.py ..\data\raw\2026-10 --schema rfb --linhas-por-bloco 100000

Para cada TIPO (empresas, estabelecimentos...), tudo acontece numa única transação:
    esvazia a tabela -> carrega todos os arquivos daquele tipo -> confirma.
Se algo der errado no meio, o PostgreSQL desfaz tudo e a tabela volta a ser como era antes.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
import psycopg
from psycopg import sql
from pydantic import ValidationError

from config import RAIZ_PROJETO, obter_configuracoes
from layouts import tipo_pelo_nome
from leitura import ArquivoSuspeitoError, LayoutDivergenteError, ler_blocos
from transformar import transformar

ARQUIVO_TABELAS = Path(__file__).resolve().parent / "sql" / "001_tabelas_rfb.sql"
PASTA_REJEITADOS = RAIZ_PROJETO / "data" / "rejeitados"
LINHAS_POR_BLOCO_PADRAO = 200_000

# Ordem de carga: primeiro as legendas (pequenas, dão um retorno rápido), depois as grandes.
ORDEM_TIPOS = (
    "cnaes",
    "municipios",
    "naturezas",
    "qualificacoes",
    "paises",
    "motivos",
    "empresas",
    "estabelecimentos",
    "socios",
    "simples",
)

log = logging.getLogger("carregar")


@dataclass
class Placar:
    """Contadores de uma tabela durante a carga (o "placar do jogo")."""

    lidas: int = 0
    carregadas: int = 0
    rejeitadas: int = 0
    malformadas: int = 0
    anulados: dict[str, int] = field(default_factory=dict)
    segundos: float = 0.0


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------


def preparar_schema(conexao: psycopg.Connection, schema: str) -> None:
    """Cria a gaveta (schema) e as tabelas, se ainda não existirem, e passa a trabalhar nela."""
    conexao.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    # A partir daqui, "empresas" significa "<schema>.empresas" nesta conexão.
    conexao.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    # O mesmo arquivo SQL da Aula 3: é idempotente (CREATE TABLE IF NOT EXISTS).
    conexao.execute(ARQUIVO_TABELAS.read_text(encoding="utf-8"))


def para_formato_copy(tabela: pl.DataFrame) -> pl.DataFrame:
    """Converte listas para o formato de array do PostgreSQL: ['a', 'b'] -> '{a,b}'."""
    conversoes = [
        pl.concat_str([pl.lit("{"), pl.col(nome).list.join(","), pl.lit("}")]).alias(nome)
        for nome, tipo in tabela.schema.items()
        if isinstance(tipo, pl.List)
    ]
    return tabela.with_columns(conversoes) if conversoes else tabela


def copiar(cursor: psycopg.Cursor, nome_tabela: str, tabela: pl.DataFrame) -> None:
    """Envia a tabela ao PostgreSQL pela "esteira expressa" (COPY), em formato CSV."""
    # FREEZE: as linhas já entram "congeladas" e marcadas como visíveis para todos. Assim o
    # faxineiro automático (autovacuum) não precisa reescrever a tabela inteira depois da carga.
    # Só é permitido porque a tabela foi esvaziada (TRUNCATE) nesta mesma transação.
    comando = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, FREEZE)").format(
        sql.Identifier(nome_tabela),
        sql.SQL(", ").join(sql.Identifier(c) for c in tabela.columns),
    )
    # O Polars escreve o CSV (nulos viram campo vazio, que o COPY entende como NULL).
    conteudo = para_formato_copy(tabela).write_csv(include_header=False)
    with cursor.copy(comando) as esteira:
        esteira.write(conteudo.encode("utf-8"))


# ---------------------------------------------------------------------------
# Rejeitados (a "caixa de devolução")
# ---------------------------------------------------------------------------


def guardar_rejeitados(
    pasta: Path, tipo: str, rejeitados: pl.DataFrame, quebradas: list[str]
) -> None:
    """Acrescenta as linhas recusadas em arquivos à parte, para investigação posterior."""
    if rejeitados.height:
        destino = pasta / f"{tipo}_rejeitados.csv"
        cabecalho = not destino.exists()
        with destino.open("a", encoding="utf-8", newline="") as arquivo:
            rejeitados.write_csv(arquivo, include_header=cabecalho)
    if quebradas:
        destino = pasta / f"{tipo}_malformadas.txt"
        with destino.open("a", encoding="utf-8") as arquivo:
            arquivo.writelines(linha + "\n" for linha in quebradas)


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------


def agrupar_arquivos(pasta: Path, tipos_pedidos: list[str] | None) -> dict[str, list[Path]]:
    """Organiza os .zip da pasta por tipo, na ordem de carga. Ex.: estabelecimentos -> [0..9]."""
    grupos: dict[str, list[Path]] = {}
    for caminho in sorted(pasta.glob("*.zip")):
        try:
            tipo = tipo_pelo_nome(caminho)
        except ValueError:
            log.warning("Ignorando arquivo desconhecido: %s", caminho.name)
            continue
        grupos.setdefault(tipo, []).append(caminho)

    escolhidos = tipos_pedidos or list(ORDEM_TIPOS)
    return {tipo: grupos[tipo] for tipo in ORDEM_TIPOS if tipo in escolhidos and tipo in grupos}


def carregar_tipo(
    conexao: psycopg.Connection,
    tipo: str,
    arquivos: list[Path],
    linhas_por_bloco: int,
    pasta_rejeitados: Path,
) -> Placar:
    """Carrega todos os arquivos de um tipo numa única transação (tudo ou nada)."""
    placar = Placar()
    inicio = time.perf_counter()

    # Remove sobras de uma carga anterior deste tipo.
    for sobra in pasta_rejeitados.glob(f"{tipo}_*"):
        sobra.unlink()

    with conexao.transaction(), conexao.cursor() as cursor:
        # TRUNCATE esvazia a tabela num instante. Dentro da transação, ele também é desfeito
        # se algo falhar: os dados antigos só somem se a carga nova der certo.
        cursor.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(tipo)))

        for arquivo in arquivos:
            log.info("%s: lendo %s", tipo, arquivo.name)
            for bloco in ler_blocos(arquivo, linhas_por_bloco):
                resultado = transformar(tipo, bloco.dados)
                if resultado.validos.height:
                    copiar(cursor, tipo, resultado.validos)
                guardar_rejeitados(pasta_rejeitados, tipo, resultado.rejeitados, bloco.malformadas)

                placar.lidas += bloco.total_linhas
                placar.carregadas += resultado.validos.height
                placar.rejeitadas += resultado.rejeitados.height
                placar.malformadas += len(bloco.malformadas)
                for coluna, total in resultado.anulados.items():
                    placar.anulados[coluna] = placar.anulados.get(coluna, 0) + total

                velocidade = placar.lidas / (time.perf_counter() - inicio)
                log.info(
                    "%s: bloco %d | %s linhas lidas | ~%s linhas/s",
                    tipo,
                    bloco.numero,
                    f"{placar.lidas:,}",
                    f"{velocidade:,.0f}",
                )

    # ANALYZE atualiza as estatísticas que o PostgreSQL usa para escolher o caminho mais rápido
    # de cada consulta. Depois de trocar milhões de linhas, é obrigatório.
    conexao.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(tipo)))

    placar.segundos = time.perf_counter() - inicio
    return placar


def imprimir_relatorio(placares: dict[str, Placar]) -> None:
    print("\n" + "=" * 92)
    print(
        f"{'tabela':<18}{'lidas':>14}{'carregadas':>14}{'rejeitadas':>12}"
        f"{'quebradas':>11}{'tempo':>10}{'linhas/s':>13}"
    )
    print("-" * 92)
    for tipo, p in placares.items():
        velocidade = p.lidas / p.segundos if p.segundos else 0
        print(
            f"{tipo:<18}{p.lidas:>14,}{p.carregadas:>14,}{p.rejeitadas:>12,}"
            f"{p.malformadas:>11,}{p.segundos:>9.1f}s{velocidade:>13,.0f}"
        )
    print("=" * 92)
    for tipo, p in placares.items():
        if p.anulados:
            detalhes = ", ".join(f"{coluna}: {total:,}" for coluna, total in p.anulados.items())
            print(f"Valores inválidos que viraram NULL em {tipo} -> {detalhes}")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    parser = argparse.ArgumentParser(description="Carrega os dados abertos do CNPJ no PostgreSQL.")
    parser.add_argument("pasta", type=Path, help="Pasta com os .zip do mês (ex.: data/raw/2026-10)")
    parser.add_argument("--schema", default="rfb", help="Gaveta de destino (padrão: rfb)")
    parser.add_argument("--tipos", nargs="+", choices=ORDEM_TIPOS, help="Carregar só estes tipos")
    parser.add_argument("--linhas-por-bloco", type=int, default=LINHAS_POR_BLOCO_PADRAO)
    args = parser.parse_args()

    if not args.pasta.is_dir():
        log.error("Pasta não encontrada: %s", args.pasta)
        return 1
    if args.linhas_por_bloco < 1_000:
        log.error("Use pelo menos 1.000 linhas por bloco.")
        return 1

    grupos = agrupar_arquivos(args.pasta, args.tipos)
    if not grupos:
        log.error("Nenhum .zip reconhecido em %s.", args.pasta)
        return 1

    # Os rejeitados ficam numa pasta com o nome do mês: data/rejeitados/2026-10/
    pasta_rejeitados = PASTA_REJEITADOS / args.pasta.name
    pasta_rejeitados.mkdir(parents=True, exist_ok=True)

    try:
        configuracoes = obter_configuracoes()
    except ValidationError as erro:
        log.error("Configuração inválida no arquivo .env:\n%s", erro)
        return 1

    placares: dict[str, Placar] = {}
    try:
        # autocommit=True: cada comando vale na hora, EXCETO dentro de conexao.transaction(),
        # onde tudo só é confirmado no final do bloco.
        with psycopg.connect(configuracoes.conninfo(), autocommit=True) as conexao:
            preparar_schema(conexao, args.schema)
            for tipo, arquivos in grupos.items():
                placares[tipo] = carregar_tipo(
                    conexao, tipo, arquivos, args.linhas_por_bloco, pasta_rejeitados
                )
    except psycopg.OperationalError as erro:
        log.error(
            "Não foi possível falar com o banco. Ele está ligado (docker compose ps)?\n%s", erro
        )
        return 1
    except (LayoutDivergenteError, ArquivoSuspeitoError, zipfile.BadZipFile) as erro:
        log.error("Carga interrompida (a tabela em andamento foi restaurada): %s", erro)
        return 1
    except KeyboardInterrupt:
        log.warning("Carga cancelada pelo usuário (a tabela em andamento foi restaurada).")
        return 130
    finally:
        if placares:
            imprimir_relatorio(placares)

    log.info("Carga concluída. Rejeitados (se houver) em: %s", pasta_rejeitados)
    return 0


if __name__ == "__main__":
    sys.exit(main())