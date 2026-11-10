"""Testes da etapa de transformação.

Cada teste monta uma "linha de mentira" no formato bruto da Receita (tudo texto),
passa pela transformação e confere se o resultado é o esperado.
"""

from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from layouts import LAYOUTS
from transformar import transformar


def linha_bruta(tipo: str, **valores: str | None) -> pl.DataFrame:
    """Cria uma tabela de 1 linha com todas as colunas do layout; as não informadas ficam vazias."""
    return pl.DataFrame(
        {coluna: [valores.get(coluna, "")] for coluna in LAYOUTS[tipo]},
        schema={coluna: pl.String for coluna in LAYOUTS[tipo]},
    )


def estabelecimento_valido(**extras: str) -> pl.DataFrame:
    """Um estabelecimento com CNPJ válido, mais os campos que o teste quiser testar."""
    return linha_bruta(
        "estabelecimentos", cnpj_basico="12345678", cnpj_ordem="0001", cnpj_dv="95", **extras
    )


# --- Datas -------------------------------------------------------------------


def test_data_valida_vira_date() -> None:
    resultado = transformar(
        "estabelecimentos", estabelecimento_valido(data_inicio_atividade="20190315")
    )
    assert resultado.validos["data_inicio_atividade"][0] == date(2019, 3, 15)


@pytest.mark.parametrize("valor", ["0", "00000000", "", "20231399", "2019031"])
def test_data_invalida_vira_null(valor: str) -> None:
    resultado = transformar("estabelecimentos", estabelecimento_valido(data_inicio_atividade=valor))
    assert resultado.validos["data_inicio_atividade"][0] is None
    # A empresa NÃO é rejeitada por causa de uma data torta: só o campo fica vazio.
    assert resultado.rejeitados.height == 0


def test_datas_zeradas_nao_contam_como_anomalia() -> None:
    bruto = estabelecimento_valido(data_inicio_atividade="0", data_situacao_especial="00000000")
    assert transformar("estabelecimentos", bruto).anulados == {}


def test_data_impossivel_conta_como_anomalia() -> None:
    bruto = estabelecimento_valido(data_inicio_atividade="20231399")
    assert transformar("estabelecimentos", bruto).anulados == {"data_inicio_atividade": 1}


# --- Códigos -----------------------------------------------------------------


def test_situacao_ganha_zero_a_esquerda() -> None:
    resultado = transformar("estabelecimentos", estabelecimento_valido(situacao_cadastral="2"))
    assert resultado.validos["situacao_cadastral"][0] == "02"


def test_cnae_secundario_vira_lista_e_descarta_itens_invalidos() -> None:
    bruto = estabelecimento_valido(cnae_fiscal_secundaria="4721102, 4729699,XYZ")
    lista = transformar("estabelecimentos", bruto).validos["cnae_fiscal_secundaria"][0]
    assert lista.to_list() == ["4721102", "4729699"]


def test_cnae_secundario_vazio_vira_null() -> None:
    resultado = transformar("estabelecimentos", estabelecimento_valido(cnae_fiscal_secundaria=""))
    assert resultado.validos["cnae_fiscal_secundaria"][0] is None


# --- Contato -----------------------------------------------------------------


def test_contatos_sao_padronizados() -> None:
    bruto = estabelecimento_valido(
        cep="58.000-000", telefone_1="9999-8888", correio_eletronico=" Contato@Empresa.COM.BR "
    )
    validos = transformar("estabelecimentos", bruto).validos
    assert validos["cep"][0] == "58000000"
    assert validos["telefone_1"][0] == "99998888"
    assert validos["correio_eletronico"][0] == "contato@empresa.com.br"


def test_email_invalido_vira_null_e_e_contado() -> None:
    resultado = transformar(
        "estabelecimentos", estabelecimento_valido(correio_eletronico="sem-arroba")
    )
    assert resultado.validos["correio_eletronico"][0] is None
    assert resultado.anulados == {"correio_eletronico": 1}


# --- Empresas e Simples ------------------------------------------------------


def test_capital_social_com_virgula_vira_decimal_exato() -> None:
    bruto = linha_bruta("empresas", cnpj_basico="00000000", capital_social="120000000000,00")
    capital = transformar("empresas", bruto).validos["capital_social"][0]
    assert capital == Decimal("120000000000.00")


def test_simples_s_n_vira_verdadeiro_falso() -> None:
    bruto = pl.concat(
        [
            linha_bruta("simples", cnpj_basico="11111111", opcao_simples="S", opcao_mei="N"),
            linha_bruta("simples", cnpj_basico="22222222", opcao_simples="", opcao_mei="X"),
        ]
    )
    validos = transformar("simples", bruto).validos
    assert validos["opcao_simples"].to_list() == [True, None]
    assert validos["opcao_mei"].to_list() == [False, None]


# --- Identidade (CNPJ) -------------------------------------------------------


def test_cnpj_alfanumerico_e_aceito_e_vira_maiusculo() -> None:
    bruto = linha_bruta("estabelecimentos", cnpj_basico="12abc345", cnpj_ordem="0001", cnpj_dv="35")
    resultado = transformar("estabelecimentos", bruto)
    assert resultado.validos["cnpj_basico"][0] == "12ABC345"
    assert resultado.rejeitados.height == 0


def test_cnpj_invalido_e_rejeitado_com_motivo() -> None:
    bruto = linha_bruta("estabelecimentos", cnpj_basico="123", cnpj_ordem="0001", cnpj_dv="AB")
    resultado = transformar("estabelecimentos", bruto)
    assert resultado.validos.height == 0
    assert resultado.rejeitados["motivo_rejeicao"][0] == "cnpj_basico,cnpj_dv"
    # O rejeitado guarda o valor ORIGINAL, para investigarmos depois.
    assert resultado.rejeitados["cnpj_basico"][0] == "123"


def test_colunas_fora_do_layout_geram_erro() -> None:
    with pytest.raises(ValueError):
        transformar("empresas", pl.DataFrame({"coluna_errada": ["x"]}))