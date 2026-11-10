"""Testes do downloader, SEM internet.

O httpx permite trocar o "cabo de rede" por um servidor de mentira (MockTransport):
uma função nossa recebe cada requisição e decide a resposta. Assim simulamos quedas de
conexão, servidores que ignoram a retomada e arquivos corrompidos, tudo em milissegundos.
"""

import io
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

import baixar
from baixar import DownloadError, baixar_arquivo, listar_meses, listar_zips

BASE = "https://servidor.teste/public.php/dav/files/TOKEN/"
URL_ZIP = BASE + "2026-10/Cnaes.zip"


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nos testes, as novas tentativas acontecem na hora (sem esperar 2, 4, 8 s...)."""
    monkeypatch.setattr(baixar, "ESPERA_BASE_SEGUNDOS", 0.0)


def cliente_falso(responder: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(responder))


def zip_de_teste() -> bytes:
    """Um .zip de verdade, com conteúdo suficiente para ser baixado "em pedaços"."""
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as arquivo_zip:
        arquivo_zip.writestr(
            "CNAECSV", "".join(f'"{i:07d}";"Atividade {i}"\n' for i in range(20_000))
        )
    return memoria.getvalue()


def inventario(*itens: tuple[str, bool, int | None]) -> bytes:
    """Monta uma resposta PROPFIND no formato do Nextcloud."""
    respostas = [f"<d:response><d:href>{httpx.URL(BASE).path}</d:href></d:response>"]
    for caminho, eh_pasta, tamanho in itens:
        tipo = "<d:collection/>" if eh_pasta else ""
        tam = f"<d:getcontentlength>{tamanho}</d:getcontentlength>" if tamanho else ""
        respostas.append(
            f"<d:response><d:href>{httpx.URL(BASE).path}{caminho}</d:href>"
            f"<d:propstat><d:prop><d:resourcetype>{tipo}</d:resourcetype>{tam}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    xml = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + "".join(respostas)
    return (xml + "</d:multistatus>").encode()


class ConexaoQueCai(httpx.SyncByteStream):
    """Entrega os primeiros bytes e então "derruba a conexão"."""

    def __init__(self, dados: bytes) -> None:
        self.dados = dados

    def __iter__(self) -> Iterator[bytes]:
        yield self.dados
        raise httpx.ReadError("conexão caiu")


# --- Inventário ----------------------------------------------------------------


def test_listar_meses_ordena_e_ignora_o_que_nao_e_mes() -> None:
    corpo = inventario(
        ("2026-10/", True, None), ("2023-05/", True, None), ("cnpj.tar.gz", False, 9)
    )
    with cliente_falso(lambda requisicao: httpx.Response(207, content=corpo)) as cliente:
        assert listar_meses(cliente, BASE) == ["2023-05", "2026-10"]


def test_listar_zips_le_tamanhos_e_ignora_desconhecidos() -> None:
    corpo = inventario(
        ("2026-10/Empresas0.zip", False, 417),
        ("2026-10/Cnaes.zip", False, 22),
        ("2026-10/LEIAME.txt", False, 5),
        ("2026-10/Novidade.zip", False, 7),
    )
    with cliente_falso(lambda requisicao: httpx.Response(207, content=corpo)) as cliente:
        itens = listar_zips(cliente, BASE, "2026-10")
    assert [(item.nome, item.tamanho) for item in itens] == [
        ("Cnaes.zip", 22),
        ("Empresas0.zip", 417),
    ]


# --- Download -------------------------------------------------------------------


def test_download_retoma_de_onde_parou_apos_queda(tmp_path: Path) -> None:
    conteudo = zip_de_teste()
    metade = len(conteudo) // 2
    pedidos_de_retomada: list[str | None] = []

    def responder(requisicao: httpx.Request) -> httpx.Response:
        intervalo = requisicao.headers.get("Range")
        pedidos_de_retomada.append(intervalo)
        if intervalo is None:  # 1ª tentativa: cai no meio do caminho
            return httpx.Response(200, stream=ConexaoQueCai(conteudo[:metade]))
        inicio = int(intervalo.removeprefix("bytes=").removesuffix("-"))
        return httpx.Response(206, content=conteudo[inicio:])

    destino = tmp_path / "Cnaes.zip"
    with cliente_falso(responder) as cliente:
        assert baixar_arquivo(cliente, URL_ZIP, destino, len(conteudo)) is True

    assert pedidos_de_retomada == [None, f"bytes={metade}-"]  # pediu SÓ o que faltava
    assert destino.read_bytes() == conteudo
    assert not (tmp_path / "Cnaes.zip.part").exists()


def test_servidor_que_ignora_retomada_faz_recomecar_do_zero(tmp_path: Path) -> None:
    conteudo = zip_de_teste()
    (tmp_path / "Cnaes.zip.part").write_bytes(b"lixo de uma tentativa antiga")

    with cliente_falso(lambda requisicao: httpx.Response(200, content=conteudo)) as cliente:
        baixar_arquivo(cliente, URL_ZIP, tmp_path / "Cnaes.zip", len(conteudo))

    assert (tmp_path / "Cnaes.zip").read_bytes() == conteudo


def test_zip_corrompido_e_descartado(tmp_path: Path) -> None:
    lixo = b"isto nao e um zip" * 100
    with cliente_falso(lambda requisicao: httpx.Response(200, content=lixo)) as cliente:
        with pytest.raises(DownloadError, match="corrompido"):
            baixar_arquivo(cliente, URL_ZIP, tmp_path / "Cnaes.zip", len(lixo))
    assert list(tmp_path.iterdir()) == []  # nem o final, nem o .part


def test_arquivo_ja_baixado_nao_e_baixado_de_novo(tmp_path: Path) -> None:
    conteudo = zip_de_teste()
    (tmp_path / "Cnaes.zip").write_bytes(conteudo)

    def nao_deveria_ser_chamado(requisicao: httpx.Request) -> httpx.Response:
        raise AssertionError("não deveria ter feito nenhuma requisição")

    with cliente_falso(nao_deveria_ser_chamado) as cliente:
        assert baixar_arquivo(cliente, URL_ZIP, tmp_path / "Cnaes.zip", len(conteudo)) is False


def test_servidor_fora_do_ar_desiste_apos_todas_as_tentativas(tmp_path: Path) -> None:
    chamadas = 0

    def responder(requisicao: httpx.Request) -> httpx.Response:
        nonlocal chamadas
        chamadas += 1
        return httpx.Response(503)

    with cliente_falso(responder) as cliente:
        with pytest.raises(DownloadError, match="tentativas falharam"):
            baixar_arquivo(cliente, URL_ZIP, tmp_path / "Cnaes.zip", 100)
    assert chamadas == baixar.MAX_TENTATIVAS