r"""Atualização mensal com "troca de vitrine": o site nunca fica sem dados.

Passo a passo:
1. Monta uma gaveta nova e vazia (rfb_carga), sem índices, e carrega o mês nela.
2. Cria as chaves e os índices UMA única vez, no final (002_chaves_indices.sql).
3. Portão de qualidade: compara a carga nova com a atual. Se algo parecer errado
   (ex.: metade das empresas sumiu por causa de um download incompleto), PARA.
4. Troca as gavetas num instante: rfb -> rfb_antigo e rfb_carga -> rfb.
Durante todo o processo, o site continua lendo a gaveta rfb antiga, normalmente.

Uso (com o ambiente virtual ativado, dentro da pasta etl):
    python atualizar.py ..\data\raw\2026-10
    python atualizar.py ..\data\raw\2026-10 --permitir-incompleto   # amostra com poucos arquivos
    python atualizar.py ..\data\raw\2026-10 --manter-antigo         # guarda o mês anterior
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import zipfile
from pathlib import Path

import psycopg
from psycopg import sql
from pydantic import ValidationError

from carregar import (
    ORDEM_TIPOS,
    PASTA_REJEITADOS,
    Placar,
    agrupar_arquivos,
    carregar_tipo,
    imprimir_relatorio,
    preparar_schema,
)
from config import obter_configuracoes
from leitura import ArquivoSuspeitoError, LayoutDivergenteError

ARQUIVO_INDICES = Path(__file__).resolve().parent / "sql" / "002_chaves_indices.sql"

# As três gavetas da troca de vitrine.
GAVETA_NO_AR = "rfb"
GAVETA_CARGA = "rfb_carga"
GAVETA_ANTIGA = "rfb_antigo"

# Portão de qualidade: a carga nova precisa ter pelo menos 90% das linhas da atual.
TOLERANCIA_PADRAO = 0.90

# Número fixo (escolhido livremente) que identifica a "chave da sala de atualização".
# Só uma atualização por vez consegue segurar essa chave no banco.
TRAVA_ATUALIZACAO = 20_261_109

# Diário de cargas. Fica numa gaveta própria (etl), que NUNCA é trocada, para guardar o
# histórico de todos os meses. TIMESTAMPTZ guarda o instante exato, independente de fuso.
SQL_CONTROLE = """
CREATE SCHEMA IF NOT EXISTS etl;
CREATE TABLE IF NOT EXISTS etl.cargas (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mes_referencia text        NOT NULL,
    iniciada_em    timestamptz NOT NULL DEFAULT now(),
    concluida_em   timestamptz,
    status         text        NOT NULL DEFAULT 'em_andamento'
                   CHECK (status IN ('em_andamento', 'concluida', 'falhou')),
    detalhes       jsonb
);
"""

log = logging.getLogger("atualizar")


class PortaoReprovadoError(Exception):
    """A carga nova não passou na conferência de qualidade."""


# ---------------------------------------------------------------------------
# Portão de qualidade (função "pura": só recebe números e devolve problemas)
# ---------------------------------------------------------------------------


def avaliar_portao(
    novos: dict[str, int],
    atuais: dict[str, int],
    tolerancia: float,
    permitir_incompleto: bool,
) -> list[str]:
    """Compara as contagens da carga nova com as da vitrine atual e lista os problemas."""
    problemas: list[str] = []
    for tipo in ORDEM_TIPOS:
        novo = novos.get(tipo)
        atual = atuais.get(tipo, 0)
        if novo is None:
            # Tipo não carregado neste mês: só é problema se a vitrine atual tem esses dados.
            if atual > 0 and not permitir_incompleto:
                problemas.append(f"{tipo}: não há arquivos deste tipo, mas hoje existem {atual:,}.")
            continue
        if novo == 0:
            problemas.append(f"{tipo}: a carga nova está vazia.")
        elif atual > 0 and novo < atual * tolerancia:
            problemas.append(
                f"{tipo}: a carga nova tem {novo:,} linhas, menos de {tolerancia:.0%} "
                f"das {atual:,} atuais. Download incompleto?"
            )
    return problemas


# ---------------------------------------------------------------------------
# Operações no banco
# ---------------------------------------------------------------------------


def gaveta_existe(conexao: psycopg.Connection, nome: str) -> bool:
    linha = conexao.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)", [nome]
    ).fetchone()
    return bool(linha and linha[0])


def contagens_atuais(conexao: psycopg.Connection) -> dict[str, int]:
    """Quantas linhas cada tabela da vitrine atual tem (estimativa instantânea do ANALYZE)."""
    if not gaveta_existe(conexao, GAVETA_NO_AR):
        return {}
    linhas = conexao.execute(
        """
        SELECT c.relname, greatest(c.reltuples, 0)::bigint
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind = 'r'
        """,
        [GAVETA_NO_AR],
    ).fetchall()
    return {nome: total for nome, total in linhas}


def trocar_vitrine(conexao: psycopg.Connection, manter_antigo: bool) -> None:
    """Troca as gavetas numa única transação: para quem consulta, a mudança é instantânea."""
    with conexao.transaction():
        # Se alguma consulta longa estiver segurando a gaveta, esperamos no máximo 30 s
        # em vez de travar o site. Se estourar, a troca falha e nada muda.
        conexao.execute("SET LOCAL lock_timeout = '30s'")
        conexao.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(GAVETA_ANTIGA))
        )
        if gaveta_existe(conexao, GAVETA_NO_AR):
            conexao.execute(
                sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                    sql.Identifier(GAVETA_NO_AR), sql.Identifier(GAVETA_ANTIGA)
                )
            )
        conexao.execute(
            sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                sql.Identifier(GAVETA_CARGA), sql.Identifier(GAVETA_NO_AR)
            )
        )
    log.info("Vitrine trocada: os dados novos estão no ar em '%s'.", GAVETA_NO_AR)

    if not manter_antigo:
        conexao.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(GAVETA_ANTIGA))
        )
        log.info("Gaveta antiga descartada para liberar espaço em disco.")


def descartar_carga(conexao: psycopg.Connection) -> None:
    """Remove a gaveta de carga de uma tentativa que não deu certo."""
    conexao.execute(
        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(GAVETA_CARGA))
    )


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------


def executar(
    conexao: psycopg.Connection,
    pasta: Path,
    linhas_por_bloco: int,
    tolerancia: float,
    permitir_incompleto: bool,
    manter_antigo: bool,
) -> dict[str, Placar]:
    grupos = agrupar_arquivos(pasta, None)
    faltando = [tipo for tipo in ORDEM_TIPOS if tipo not in grupos]
    if faltando and not permitir_incompleto:
        raise PortaoReprovadoError(
            f"Faltam arquivos destes tipos: {', '.join(faltando)}. "
            "Use --permitir-incompleto só para testes com amostras."
        )

    pasta_rejeitados = PASTA_REJEITADOS / pasta.name
    pasta_rejeitados.mkdir(parents=True, exist_ok=True)

    # 1. Gaveta nova e vazia (apaga restos de uma tentativa anterior que falhou).
    descartar_carga(conexao)
    preparar_schema(conexao, GAVETA_CARGA)

    # 2. Carga, sem índices (rápida).
    placares: dict[str, Placar] = {}
    for tipo, arquivos in grupos.items():
        placares[tipo] = carregar_tipo(conexao, tipo, arquivos, linhas_por_bloco, pasta_rejeitados)

    # 3. Chaves e índices, uma única vez. O search_path ainda aponta para rfb_carga.
    log.info("Criando chaves e índices em '%s'...", GAVETA_CARGA)
    inicio = time.perf_counter()
    conexao.execute(ARQUIVO_INDICES.read_text(encoding="utf-8"))
    log.info("Índices prontos em %.1f s.", time.perf_counter() - inicio)

    # 4. Portão de qualidade.
    novos = {tipo: placar.carregadas for tipo, placar in placares.items()}
    problemas = avaliar_portao(novos, contagens_atuais(conexao), tolerancia, permitir_incompleto)
    if problemas:
        raise PortaoReprovadoError(
            "Portão de qualidade reprovou a carga:\n  " + "\n  ".join(problemas)
        )
    log.info("Portão de qualidade aprovado.")

    # 5. Troca de vitrine.
    trocar_vitrine(conexao, manter_antigo)
    return placares


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    parser = argparse.ArgumentParser(description="Atualização mensal com troca de vitrine.")
    parser.add_argument("pasta", type=Path, help="Pasta com os .zip do mês (ex.: data/raw/2026-10)")
    parser.add_argument("--linhas-por-bloco", type=int, default=200_000)
    parser.add_argument("--tolerancia", type=float, default=TOLERANCIA_PADRAO)
    parser.add_argument("--permitir-incompleto", action="store_true")
    parser.add_argument("--manter-antigo", action="store_true")
    args = parser.parse_args()

    if not args.pasta.is_dir():
        log.error("Pasta não encontrada: %s", args.pasta)
        return 1
    if not 0 < args.tolerancia <= 1:
        log.error("--tolerancia deve estar entre 0 e 1 (ex.: 0.9 = 90%%).")
        return 1

    try:
        configuracoes = obter_configuracoes()
    except ValidationError as erro:
        log.error("Configuração inválida no arquivo .env:\n%s", erro)
        return 1

    try:
        conexao = psycopg.connect(configuracoes.conninfo(), autocommit=True)
    except psycopg.OperationalError as erro:
        log.error(
            "Não foi possível falar com o banco. Ele está ligado (docker compose ps)?\n%s", erro
        )
        return 1

    # Mostra no nosso log as mensagens que o banco envia (ex.: "Criando chave primária de..."),
    # ignorando os avisos inofensivos do tipo "não existe, pulando" (DROP ... IF EXISTS).
    def mostrar_aviso(aviso: psycopg.errors.Diagnostic) -> None:
        mensagem = aviso.message_primary or ""
        if not mensagem.endswith("skipping"):
            log.info("banco: %s", mensagem)

    conexao.add_notice_handler(mostrar_aviso)

    with conexao:
        # Trava de sessão (advisory lock): como a placa de "ocupado" na porta. Se outra
        # atualização já estiver rodando, saímos sem atrapalhar. O banco solta a trava
        # sozinho quando a conexão termina, mesmo que o programa morra de repente.
        trava = conexao.execute("SELECT pg_try_advisory_lock(%s)", [TRAVA_ATUALIZACAO]).fetchone()
        if not (trava and trava[0]):
            log.error("Outra atualização já está em andamento. Tente de novo mais tarde.")
            return 1

        conexao.execute(SQL_CONTROLE)
        # Se estamos com a trava, nenhuma outra atualização está rodando. Então qualquer
        # carga ainda marcada como "em_andamento" foi interrompida (queda de energia,
        # banco que caiu...) e ficou abandonada no diário. Registramos isso.
        motivo = {"erro": "interrompida: encontrada abandonada pela execução seguinte"}
        abandonadas = conexao.execute(
            "UPDATE etl.cargas SET status = 'falhou', concluida_em = now(), detalhes = %s "
            "WHERE status = 'em_andamento'",
            [json.dumps(motivo, ensure_ascii=False)],
        ).rowcount
        if abandonadas:
            log.warning(
                "Cargas interrompidas anteriormente, agora marcadas 'falhou': %d", abandonadas
            )

        id_carga = conexao.execute(
            "INSERT INTO etl.cargas (mes_referencia) VALUES (%s) RETURNING id", [args.pasta.name]
        ).fetchone()[0]
        inicio = time.perf_counter()

        try:
            placares = executar(
                conexao,
                args.pasta,
                args.linhas_por_bloco,
                args.tolerancia,
                args.permitir_incompleto,
                args.manter_antigo,
            )
        except (
            PortaoReprovadoError,
            LayoutDivergenteError,
            ArquivoSuspeitoError,
            zipfile.BadZipFile,
            psycopg.Error,
            KeyboardInterrupt,
        ) as erro:
            mensagem = str(erro) or erro.__class__.__name__
            log.error("Atualização cancelada. A vitrine atual continua no ar.\n%s", mensagem)
            try:
                descartar_carga(conexao)
                conexao.execute(
                    "UPDATE etl.cargas SET status = 'falhou', concluida_em = now(), detalhes = %s "
                    "WHERE id = %s",
                    [json.dumps({"erro": mensagem}, ensure_ascii=False), id_carga],
                )
            except psycopg.Error:
                log.warning("Não foi possível registrar a falha no diário de cargas.")
            return 1

        detalhes = {tipo: p.carregadas for tipo, p in placares.items()}
        detalhes["segundos"] = round(time.perf_counter() - inicio, 1)
        conexao.execute(
            "UPDATE etl.cargas SET status = 'concluida', concluida_em = now(), detalhes = %s "
            "WHERE id = %s",
            [json.dumps(detalhes), id_carga],
        )

    imprimir_relatorio(placares)
    log.info("Atualização de %s concluída.", args.pasta.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())