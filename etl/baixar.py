r"""Download dos dados abertos do CNPJ direto do servidor da Receita (WebDAV).

O servidor da Receita é um compartilhamento público no estilo Nextcloud. Ele fala WebDAV,
uma "língua" que permite, além de baixar, pedir o INVENTÁRIO de uma pasta (PROPFIND).

Recursos de resiliência:
* Retomada: o arquivo é baixado como "Nome.zip.part". Se a conexão cair, a próxima
  tentativa pede só o que falta (cabeçalho Range), sem recomeçar do zero.
* Novas tentativas com espera crescente (2 s, 4 s, 8 s...), para não insistir em cima
  de um servidor que está com problemas.
* Conferência final: tamanho igual ao anunciado pelo servidor E .zip íntegro. Só então
  o ".part" vira o arquivo definitivo. Um arquivo com o nome final é sempre confiável.
* Um arquivo por vez: o servidor da Receita costuma recusar várias conexões simultâneas.

Uso (com o ambiente virtual ativado, dentro da pasta etl):
    python baixar.py --listar                            # mostra os meses disponíveis
    python baixar.py                                     # baixa o mês mais recente inteiro
    python baixar.py --mes 2026-10 --tipos cnaes motivos # só alguns tipos de um mês
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin
from xml.etree import ElementTree

import httpx
from pydantic import ValidationError

from config import RAIZ_PROJETO, obter_configuracoes
from layouts import LAYOUTS, tipo_pelo_nome

PASTA_RAW = RAIZ_PROJETO / "data" / "raw"
DAV = "{DAV:}"  # o "sobrenome" (namespace) das etiquetas XML do WebDAV
PADRAO_MES = re.compile(r"^\d{4}-\d{2}$")
MAX_TENTATIVAS = 6
ESPERA_BASE_SEGUNDOS = 2.0  # 2, 4, 8, 16, 32 s entre as tentativas

# Pedimos ao servidor só as duas informações que nos interessam de cada item.
CORPO_PROPFIND = (
    b'<?xml version="1.0"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/></d:prop>'
    b"</d:propfind>"
)

log = logging.getLogger("baixar")


class DownloadError(Exception):
    """Falha definitiva: não adianta tentar de novo agora."""


class FalhaTemporariaError(Exception):
    """Falha passageira do servidor (ex.: erro 503): vale tentar de novo daqui a pouco."""


@dataclass(frozen=True)
class ItemRemoto:
    nome: str
    eh_pasta: bool
    tamanho: int | None  # em bytes; None se o servidor não informar


# ---------------------------------------------------------------------------
# Inventário (PROPFIND)
# ---------------------------------------------------------------------------


def listar(cliente: httpx.Client, url: str) -> list[ItemRemoto]:
    """Pede ao servidor o inventário de uma pasta (só o primeiro nível)."""
    resposta = cliente.request(
        "PROPFIND",
        url,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        content=CORPO_PROPFIND,
    )
    if resposta.status_code != 207:  # 207 = "Multi-Status", a resposta normal do PROPFIND
        raise DownloadError(f"O servidor respondeu HTTP {resposta.status_code} ao listar {url}")

    raiz = ElementTree.fromstring(resposta.content)
    pasta_pedida = unquote(httpx.URL(url).path).rstrip("/")
    itens: list[ItemRemoto] = []
    for item in raiz.iter(f"{DAV}response"):
        caminho = unquote(item.findtext(f"{DAV}href", default="")).rstrip("/")
        if not caminho or caminho == pasta_pedida:
            continue  # o inventário inclui a própria pasta: pulamos
        texto_tamanho = item.findtext(f".//{DAV}getcontentlength")
        itens.append(
            ItemRemoto(
                nome=caminho.rsplit("/", 1)[-1],
                eh_pasta=item.find(f".//{DAV}collection") is not None,
                tamanho=int(texto_tamanho) if texto_tamanho and texto_tamanho.isdigit() else None,
            )
        )
    return itens


def listar_meses(cliente: httpx.Client, url_base: str) -> list[str]:
    """Devolve as pastas de mês (AAAA-MM) em ordem: a última é a mais recente."""
    meses = sorted(
        item.nome
        for item in listar(cliente, url_base)
        if item.eh_pasta and PADRAO_MES.match(item.nome)
    )
    if not meses:
        raise DownloadError("Nenhuma pasta de mês (AAAA-MM) encontrada. O servidor mudou?")
    return meses


def listar_zips(cliente: httpx.Client, url_base: str, mes: str) -> list[ItemRemoto]:
    """Lista os .zip de um mês, ignorando arquivos que não reconhecemos."""
    zips: list[ItemRemoto] = []
    for item in listar(cliente, urljoin(url_base, f"{mes}/")):
        if item.eh_pasta or not item.nome.lower().endswith(".zip"):
            continue
        try:
            tipo_pelo_nome(Path(item.nome))
        except ValueError:
            log.warning("Ignorando arquivo desconhecido no servidor: %s", item.nome)
            continue
        zips.append(item)
    return sorted(zips, key=lambda item: item.nome)


# ---------------------------------------------------------------------------
# Download com retomada
# ---------------------------------------------------------------------------


def zip_integro(caminho: Path) -> bool:
    """Lê o .zip inteiro conferindo a "assinatura" (CRC) de cada pedaço."""
    try:
        with zipfile.ZipFile(caminho) as arquivo_zip:
            return arquivo_zip.testzip() is None
    except (zipfile.BadZipFile, OSError):
        return False


def _transferir(cliente: httpx.Client, url: str, parcial: Path, tamanho: int | None) -> None:
    """Uma tentativa de download, continuando do ponto em que o .part parou."""
    ja_baixado = parcial.stat().st_size if parcial.exists() else 0
    if tamanho is not None and ja_baixado >= tamanho:
        if ja_baixado > tamanho:  # maior que o esperado: algo estranho, recomeçamos
            parcial.unlink()
        else:
            return  # já está completo

    # "Range: bytes=N-" pede ao servidor só a parte que ainda falta, a partir do byte N.
    cabecalhos = {"Range": f"bytes={ja_baixado}-"} if ja_baixado else {}
    with cliente.stream("GET", url, headers=cabecalhos) as resposta:
        if resposta.status_code == 206:  # "Partial Content": o servidor aceitou continuar
            modo = "ab"  # acrescenta ao final do .part
        elif resposta.status_code == 200:  # arquivo inteiro (ou o servidor ignorou o Range)
            modo = "wb"  # começa do zero
            ja_baixado = 0
        elif resposta.status_code == 429 or resposta.status_code >= 500:
            raise FalhaTemporariaError(f"HTTP {resposta.status_code}")
        else:
            raise DownloadError(f"HTTP {resposta.status_code} ao baixar {url}")

        recebido = ja_baixado
        proximo_aviso = 10
        with parcial.open(modo) as arquivo:
            # Gravamos cada pedaço assim que ele chega: se a conexão cair, o que já veio
            # fica salvo no .part e a próxima tentativa continua dali.
            for pedaco in resposta.iter_bytes():
                arquivo.write(pedaco)
                recebido += len(pedaco)
                if tamanho:
                    percentual = recebido * 100 // tamanho
                    if percentual >= proximo_aviso:
                        log.info("  %s: %d%%", parcial.name.removesuffix(".part"), percentual)
                        proximo_aviso = percentual // 10 * 10 + 10


def baixar_arquivo(cliente: httpx.Client, url: str, destino: Path, tamanho: int | None) -> bool:
    """Baixa um arquivo com retomada e conferência. Devolve False se ele já estava baixado."""
    # Um arquivo com o nome final só existe depois de conferido, então basta checar o tamanho.
    if destino.exists() and (tamanho is None or destino.stat().st_size == tamanho):
        return False

    parcial = destino.with_name(destino.name + ".part")
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            _transferir(cliente, url, parcial, tamanho)
            break
        except (httpx.TransportError, FalhaTemporariaError) as erro:
            # TransportError cobre conexão caída, tempo esgotado, servidor fora do ar...
            if tentativa == MAX_TENTATIVAS:
                raise DownloadError(
                    f"{destino.name}: {MAX_TENTATIVAS} tentativas falharam ({erro}). "
                    "Rode de novo mais tarde: o download continuará de onde parou."
                ) from erro
            espera = ESPERA_BASE_SEGUNDOS * 2 ** (tentativa - 1)
            log.warning(
                "%s: tentativa %d falhou (%s). Nova tentativa em %.0f s, de onde parou.",
                destino.name,
                tentativa,
                erro.__class__.__name__,
                espera,
            )
            time.sleep(espera)

    if tamanho is not None and parcial.stat().st_size != tamanho:
        raise DownloadError(
            f"{destino.name}: tamanho {parcial.stat().st_size:,} diferente do anunciado "
            f"({tamanho:,}). Rode de novo para completar."
        )
    if not zip_integro(parcial):
        parcial.unlink()
        raise DownloadError(f"{destino.name}: o .zip veio corrompido e foi descartado.")

    parcial.replace(destino)  # só agora o arquivo ganha o nome definitivo
    return True


def verificar_espaco(pasta: Path, itens: list[ItemRemoto]) -> None:
    """Confere, antes de começar, se há espaço em disco para o que falta baixar."""
    falta = 0
    for item in itens:
        destino = pasta / item.nome
        parcial = destino.with_name(destino.name + ".part")
        if item.tamanho is None or (destino.exists() and destino.stat().st_size == item.tamanho):
            continue
        falta += item.tamanho - (parcial.stat().st_size if parcial.exists() else 0)
    livre = shutil.disk_usage(pasta).free
    margem = 2 * 1024**3  # 2 GB de folga
    if falta + margem > livre:
        raise DownloadError(
            f"Espaço insuficiente: faltam baixar {falta / 1024**3:.1f} GB "
            f"e há {livre / 1024**3:.1f} GB livres (com 2 GB de folga)."
        )


def baixar_mes(
    cliente: httpx.Client, url_base: str, mes: str, tipos: list[str] | None, pasta_raw: Path
) -> Path:
    """Baixa os .zip de um mês para data/raw/AAAA-MM e devolve essa pasta."""
    itens = listar_zips(cliente, url_base, mes)
    if tipos:
        itens = [item for item in itens if tipo_pelo_nome(Path(item.nome)) in tipos]
    if not itens:
        raise DownloadError(f"Nenhum .zip para baixar em {mes} com os filtros escolhidos.")

    pasta = pasta_raw / mes
    pasta.mkdir(parents=True, exist_ok=True)
    verificar_espaco(pasta, itens)

    total_gb = sum(item.tamanho or 0 for item in itens) / 1024**3
    log.info("%s: %d arquivos, %.2f GB no total.", mes, len(itens), total_gb)
    for numero, item in enumerate(itens, start=1):
        inicio = time.perf_counter()
        baixou = baixar_arquivo(
            cliente, urljoin(url_base, f"{mes}/{item.nome}"), pasta / item.nome, item.tamanho
        )
        if baixou:
            segundos = time.perf_counter() - inicio
            mb = (item.tamanho or (pasta / item.nome).stat().st_size) / 1024**2
            log.info(
                "[%d/%d] %s: %.0f MB em %.0f s (%.1f MB/s)",
                numero,
                len(itens),
                item.nome,
                mb,
                segundos,
                mb / segundos if segundos else 0,
            )
        else:
            log.info("[%d/%d] %s: já estava baixado.", numero, len(itens), item.nome)
    return pasta


def criar_cliente() -> httpx.Client:
    """O "telefone" que usamos para falar com o servidor, com tempos limite sensatos."""
    return httpx.Client(
        # Até 30 s para conectar; até 120 s sem receber nenhum byte antes de desistir.
        timeout=httpx.Timeout(30.0, read=120.0),
        follow_redirects=True,
        headers={"User-Agent": "pipeline-cnpj/1.0"},
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # esconde o log de cada requisição
    parser = argparse.ArgumentParser(description="Baixa os dados abertos do CNPJ da Receita.")
    parser.add_argument("--listar", action="store_true", help="Só mostra os meses disponíveis")
    parser.add_argument("--mes", help="Mês a baixar (AAAA-MM). Padrão: o mais recente")
    parser.add_argument("--tipos", nargs="+", choices=sorted(LAYOUTS), help="Só estes tipos")
    args = parser.parse_args()

    if args.mes and not PADRAO_MES.match(args.mes):
        log.error("--mes deve ter o formato AAAA-MM (ex.: 2026-10).")
        return 1

    try:
        url_base = obter_configuracoes().rfb_dav_url
    except ValidationError as erro:
        log.error("Configuração inválida no arquivo .env:\n%s", erro)
        return 1

    try:
        with criar_cliente() as cliente:
            meses = listar_meses(cliente, url_base)
            if args.listar:
                print(f"{len(meses)} meses disponíveis, de {meses[0]} a {meses[-1]}.")
                print(f"Mais recente: {meses[-1]}")
                return 0
            mes = args.mes or meses[-1]
            if mes not in meses:
                log.error(
                    "O mês %s não existe no servidor. Disponíveis: %s ... %s",
                    mes,
                    meses[0],
                    meses[-1],
                )
                return 1
            pasta = baixar_mes(cliente, url_base, mes, args.tipos, PASTA_RAW)
    except (DownloadError, httpx.HTTPError, ElementTree.ParseError) as erro:
        log.error("Download interrompido: %s", erro)
        return 1
    except KeyboardInterrupt:
        log.warning("Cancelado. Rode de novo para continuar de onde parou.")
        return 130

    log.info("Pronto! Arquivos em: %s", pasta)
    return 0


if __name__ == "__main__":
    sys.exit(main())