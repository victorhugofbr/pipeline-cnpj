"""Testes da leitura em blocos.

Cada teste cria um pequeno .zip falso (no formato da Receita) numa pasta temporária,
lê com ler_blocos() e confere o resultado. Usamos a tabela de CNAEs (2 colunas) por
ser a mais simples.
"""

import zipfile
from pathlib import Path

import pytest

from leitura import ArquivoSuspeitoError, LayoutDivergenteError, ler_blocos


def criar_zip(pasta: Path, linhas: list[str], nome: str = "Cnaes.zip") -> Path:
    """Grava as linhas em Latin-1 dentro de um .zip, como a Receita faz."""
    caminho = pasta / nome
    conteudo = ("\n".join(linhas) + "\n").encode("latin-1")
    with zipfile.ZipFile(caminho, "w") as arquivo_zip:
        arquivo_zip.writestr("F.K03200$Z.D61011.CNAECSV", conteudo)
    return caminho


def test_acentos_latin1_sao_lidos_corretamente(tmp_path: Path) -> None:
    caminho = criar_zip(tmp_path, ['"4721102";"Padaria com predominância de revenda"'])
    bloco = next(ler_blocos(caminho, linhas_por_bloco=1000))
    assert bloco.dados["descricao"][0] == "Padaria com predominância de revenda"


def test_campo_com_quebra_de_linha_nao_e_cortado_entre_blocos(tmp_path: Path) -> None:
    # A 2ª linha tem uma quebra de linha DENTRO das aspas, bem na fronteira do bloco.
    linhas = ['"0000001";"A"', '"0000002";"PRIMEIRA PARTE\nSEGUNDA PARTE"', '"0000003";"C"']
    blocos = list(ler_blocos(criar_zip(tmp_path, linhas), linhas_por_bloco=2))
    descricoes = [d for bloco in blocos for d in bloco.dados["descricao"].to_list()]
    assert descricoes == ["A", "PRIMEIRA PARTE\nSEGUNDA PARTE", "C"]


def test_linha_curta_vai_para_malformadas(tmp_path: Path) -> None:
    linhas = ['"0000001";"A"', '"0000002"', '"0000003";"C"']
    bloco = next(ler_blocos(criar_zip(tmp_path, linhas), linhas_por_bloco=1000))
    assert bloco.dados["codigo"].to_list() == ["0000001", "0000003"]
    assert bloco.malformadas == ["0000002"]


def test_caractere_nulo_e_removido(tmp_path: Path) -> None:
    caminho = criar_zip(tmp_path, ['"0000001";"TEXTO\x00COM NULO"'])
    bloco = next(ler_blocos(caminho, linhas_por_bloco=1000))
    assert "\x00" not in bloco.dados["descricao"][0]


def test_layout_diferente_no_primeiro_bloco_gera_erro(tmp_path: Path) -> None:
    caminho = criar_zip(tmp_path, ['"0000001";"A";"COLUNA NOVA"'])
    with pytest.raises(LayoutDivergenteError):
        next(ler_blocos(caminho, linhas_por_bloco=1000))


def test_disjuntor_para_a_carga_com_muitas_linhas_quebradas(tmp_path: Path) -> None:
    linhas = ['"0000001";"A"'] + ['"SÓ UM CAMPO"'] * 50
    with pytest.raises(ArquivoSuspeitoError):
        next(ler_blocos(criar_zip(tmp_path, linhas), linhas_por_bloco=1000))