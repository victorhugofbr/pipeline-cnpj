r"""Pipeline mensal: um único comando que deixa o banco em dia com a Receita.

1. Pergunta ao servidor qual é o mês mais recente.
2. Consulta o diário de cargas (etl.cargas): se esse mês já foi carregado POR COMPLETO,
   encerra sem fazer nada. Por isso ele pode ser agendado para rodar todo dia: só
   trabalha quando a Receita publica um mês novo.
3. Baixa o mês (com retomada; se cair no meio, a próxima execução continua de onde parou).
4. Chama o atualizar.py, que carrega numa gaveta nova e faz a troca de vitrine.

Uso (com o ambiente virtual ativado, dentro da pasta etl):
    python mensal.py
    python mensal.py --forcar         # recarrega mesmo que o mês já esteja no banco
    python mensal.py --apagar-zips    # apaga os .zip do mês depois de uma carga bem-sucedida
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import psycopg
from pydantic import ValidationError

from baixar import PASTA_RAW, DownloadError, baixar_mes, criar_cliente, listar_meses
from carregar import ORDEM_TIPOS
from config import obter_configuracoes

ATUALIZAR = Path(__file__).resolve().parent / "atualizar.py"

log = logging.getLogger("mensal")


def mes_ja_carregado(conninfo: str, mes: str) -> bool:
    """True se o diário registra uma carga CONCLUÍDA e COMPLETA (todos os tipos) deste mês."""
    try:
        with psycopg.connect(conninfo) as conexao:
            linhas = conexao.execute(
                "SELECT detalhes FROM etl.cargas "
                "WHERE mes_referencia = %s AND status = 'concluida'",
                [mes],
            ).fetchall()
    except psycopg.errors.UndefinedTable:
        return False  # o diário ainda não existe: nunca houve carga
    # Uma carga de teste com --permitir-incompleto não conta: exigimos todos os tipos.
    return any(all(tipo in (detalhes or {}) for tipo in ORDEM_TIPOS) for (detalhes,) in linhas)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Baixa e carrega o mês mais recente da Receita.")
    parser.add_argument("--forcar", action="store_true", help="Recarrega mesmo se já carregado")
    parser.add_argument("--apagar-zips", action="store_true", help="Apaga os .zip após o sucesso")
    args = parser.parse_args()

    try:
        configuracoes = obter_configuracoes()
    except ValidationError as erro:
        log.error("Configuração inválida no arquivo .env:\n%s", erro)
        return 1

    try:
        with criar_cliente() as cliente:
            mes = listar_meses(cliente, configuracoes.rfb_dav_url)[-1]
            log.info("Mês mais recente na Receita: %s", mes)

            if not args.forcar and mes_ja_carregado(configuracoes.conninfo(), mes):
                log.info("O mês %s já está carregado por completo. Nada a fazer.", mes)
                return 0

            pasta = baixar_mes(cliente, configuracoes.rfb_dav_url, mes, None, PASTA_RAW)
    except (DownloadError, httpx.HTTPError) as erro:
        log.error("Download interrompido: %s", erro)
        return 1
    except psycopg.OperationalError as erro:
        log.error("Não foi possível consultar o diário de cargas no banco:\n%s", erro)
        return 1

    # Chamamos o atualizar.py como um programa separado (o "maestro" chama outro músico).
    # sys.executable garante que será usado o MESMO Python do ambiente virtual.
    log.info("Download completo. Iniciando a atualização do banco...")
    resultado = subprocess.run([sys.executable, str(ATUALIZAR), str(pasta)], check=False)
    if resultado.returncode != 0:
        log.error("A atualização falhou (código %d). Veja o log acima.", resultado.returncode)
        return resultado.returncode

    if args.apagar_zips:
        shutil.rmtree(pasta)
        log.info("Arquivos .zip de %s apagados para liberar espaço.", mes)

    log.info("Pipeline mensal concluído: %s está no ar.", mes)
    return 0


if __name__ == "__main__":
    sys.exit(main())