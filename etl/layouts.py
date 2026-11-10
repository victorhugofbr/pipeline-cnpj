"""Layouts (nomes e ordem das colunas) dos arquivos de dados abertos do CNPJ.

Os arquivos da Receita Federal NÃO têm linha de cabeçalho: a primeira linha já é um registro.
Por isso, somos nós que damos nome às colunas, seguindo a ordem exata do PDF oficial
"Novo Layout para os Dados Abertos do CNPJ".

Este módulo é a "legenda" compartilhada por todo o ETL: o script de exploração usa agora,
e o carregador do banco de dados vai usar nas próximas aulas.
"""

from pathlib import Path
from typing import Final

# Separador e codificação de texto usados pela Receita em TODOS os arquivos.
SEPARADOR: Final[str] = ";"
ENCODING_RECEITA: Final[str] = "latin-1"  # ISO-8859-1: o "alfabeto" antigo dos arquivos

EMPRESAS: Final[tuple[str, ...]] = (
    "cnpj_basico",
    "razao_social",
    "natureza_juridica",
    "qualificacao_responsavel",
    "capital_social",
    "porte_empresa",
    "ente_federativo_responsavel",
)

ESTABELECIMENTOS: Final[tuple[str, ...]] = (
    "cnpj_basico",
    "cnpj_ordem",
    "cnpj_dv",
    "identificador_matriz_filial",
    "nome_fantasia",
    "situacao_cadastral",
    "data_situacao_cadastral",
    "motivo_situacao_cadastral",
    "nome_cidade_exterior",
    "pais",
    "data_inicio_atividade",
    "cnae_fiscal_principal",
    "cnae_fiscal_secundaria",
    "tipo_logradouro",
    "logradouro",
    "numero",
    "complemento",
    "bairro",
    "cep",
    "uf",
    "municipio",
    "ddd_1",
    "telefone_1",
    "ddd_2",
    "telefone_2",
    "ddd_fax",
    "fax",
    "correio_eletronico",
    "situacao_especial",
    "data_situacao_especial",
)

SOCIOS: Final[tuple[str, ...]] = (
    "cnpj_basico",
    "identificador_socio",
    "nome_socio_razao_social",
    "cnpj_cpf_socio",
    "qualificacao_socio",
    "data_entrada_sociedade",
    "pais",
    "representante_legal",
    "nome_representante",
    "qualificacao_representante_legal",
    "faixa_etaria",
)

SIMPLES: Final[tuple[str, ...]] = (
    "cnpj_basico",
    "opcao_simples",
    "data_opcao_simples",
    "data_exclusao_simples",
    "opcao_mei",
    "data_opcao_mei",
    "data_exclusao_mei",
)

# As tabelas de domínio ("legendas") têm sempre o mesmo formato: código + descrição.
DOMINIO: Final[tuple[str, ...]] = ("codigo", "descricao")

# Dicionário que liga o "tipo" do arquivo ao seu layout.
LAYOUTS: Final[dict[str, tuple[str, ...]]] = {
    "empresas": EMPRESAS,
    "estabelecimentos": ESTABELECIMENTOS,
    "socios": SOCIOS,
    "simples": SIMPLES,
    "cnaes": DOMINIO,
    "municipios": DOMINIO,
    "naturezas": DOMINIO,
    "qualificacoes": DOMINIO,
    "paises": DOMINIO,
    "motivos": DOMINIO,
}


def tipo_pelo_nome(caminho: Path) -> str:
    """Descobre o tipo do arquivo pelo nome do .zip.

    Exemplos: "Estabelecimentos1.zip" -> "estabelecimentos"; "Cnaes.zip" -> "cnaes".
    """
    # .stem tira a extensão (".zip"); rstrip remove os números do fim; lower() padroniza.
    tipo = caminho.stem.rstrip("0123456789").lower()
    if tipo not in LAYOUTS:
        conhecidos = ", ".join(sorted(LAYOUTS))
        raise ValueError(
            f"Não reconheço o tipo do arquivo '{caminho.name}'. Tipos conhecidos: {conhecidos}."
        )
    return tipo