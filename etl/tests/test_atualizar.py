"""Testes do portão de qualidade da atualização mensal.

O portão é uma função "pura": recebe só números e devolve a lista de problemas.
Por isso dá para testá-lo sem banco de dados, em milissegundos.
"""

from atualizar import avaliar_portao

ATUAIS = {"empresas": 1_000, "estabelecimentos": 2_000, "socios": 500}


def test_carga_parecida_com_a_atual_e_aprovada() -> None:
    novos = {"empresas": 1_010, "estabelecimentos": 1_990, "socios": 505}
    assert avaliar_portao(novos, ATUAIS, tolerancia=0.9, permitir_incompleto=False) == []


def test_carga_com_muito_menos_linhas_e_reprovada() -> None:
    novos = {"empresas": 1_000, "estabelecimentos": 1_000, "socios": 500}
    problemas = avaliar_portao(novos, ATUAIS, tolerancia=0.9, permitir_incompleto=False)
    assert len(problemas) == 1
    assert problemas[0].startswith("estabelecimentos:")


def test_tabela_vazia_e_reprovada_mesmo_sem_vitrine_anterior() -> None:
    problemas = avaliar_portao({"empresas": 0}, {}, tolerancia=0.9, permitir_incompleto=True)
    assert problemas == ["empresas: a carga nova está vazia."]


def test_primeira_carga_sem_vitrine_anterior_e_aprovada() -> None:
    novos = {"empresas": 10, "estabelecimentos": 20}
    assert avaliar_portao(novos, {}, tolerancia=0.9, permitir_incompleto=False) == []


def test_tipo_ausente_reprova_a_menos_que_seja_permitido() -> None:
    novos = {"empresas": 1_000, "estabelecimentos": 2_000}  # faltou "socios"
    assert avaliar_portao(novos, ATUAIS, tolerancia=0.9, permitir_incompleto=False) != []
    assert avaliar_portao(novos, ATUAIS, tolerancia=0.9, permitir_incompleto=True) == []