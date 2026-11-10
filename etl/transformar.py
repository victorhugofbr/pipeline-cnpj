"""Transformação: limpa, padroniza e valida os dados brutos da Receita Federal.

Recebe uma tabela com TODAS as colunas como texto (exatamente como vieram do arquivo)
e devolve:
  * validos: linhas prontas para o banco, já com os tipos certos (datas, booleanos, listas...)
  * rejeitados: linhas cuja IDENTIDADE (o CNPJ ou o código) é inválida, com o motivo
  * anulados: quantos valores tortos viraram NULL em cada coluna (para o relatório da carga)

Regra de ouro: só rejeitamos a linha inteira quando a identidade é inválida. Se apenas uma
data ou um e-mail estiver torto, guardamos a empresa e deixamos aquele campo vazio (NULL).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

from layouts import LAYOUTS

# Valores que a Receita usa para dizer "campo vazio".
VAZIOS_DA_RECEITA: list[str] = ["", "0", "00000000"]

# Uma "receita" recebe o nome de uma coluna e devolve a expressão do Polars que a limpa.
Receita = Callable[[str], pl.Expr]


# ---------------------------------------------------------------------------
# Receitas de limpeza: uma para cada tipo de ingrediente
# ---------------------------------------------------------------------------


def texto(coluna: str) -> pl.Expr:
    """Tira espaços das pontas; texto vazio vira NULL."""
    limpo = pl.col(coluna).str.strip_chars()
    return pl.when(limpo == "").then(None).otherwise(limpo)


def codigo(tamanho: int) -> Receita:
    """Código numérico de tamanho fixo. Completa zeros à esquerda: '2' -> '02'."""

    def receita(coluna: str) -> pl.Expr:
        completo = texto(coluna).str.zfill(tamanho)
        valido = completo.str.contains(rf"^\d{{{tamanho}}}$")
        return pl.when(valido).then(completo).otherwise(None)

    return receita


def parte_cnpj(tamanho: int, so_numeros: bool = False) -> Receita:
    """Pedaço do CNPJ. Aceita letras (CNPJ alfanumérico), exceto no dígito verificador."""
    padrao = rf"^\d{{{tamanho}}}$" if so_numeros else rf"^[0-9A-Z]{{{tamanho}}}$"

    def receita(coluna: str) -> pl.Expr:
        # Não completamos com zeros aqui: um CNPJ com tamanho errado é suspeito e deve ser
        # rejeitado, não "consertado" por adivinhação.
        maiusculo = texto(coluna).str.to_uppercase()
        return pl.when(maiusculo.str.contains(padrao)).then(maiusculo).otherwise(None)

    return receita


def data(coluna: str) -> pl.Expr:
    """'20190315' -> 2019-03-15. '0', '00000000', vazio ou data impossível -> NULL."""
    limpo = texto(coluna)
    # Trava: exigimos exatamente 8 dígitos ANTES de converter. Sem ela, o conversor aceitaria
    # '2019031' (7 dígitos) como 01/03/2019, inventando uma data que não existe no arquivo.
    oito_digitos = limpo.str.contains(r"^\d{8}$")
    convertido = limpo.str.to_date(format="%Y%m%d", strict=False)
    return pl.when(oito_digitos).then(convertido).otherwise(None)


def dinheiro(coluna: str) -> pl.Expr:
    """'1000,50' -> 1000.50 como Decimal (número EXATO, sem os erros de arredondamento do float)."""
    com_ponto = texto(coluna).str.replace(",", ".", literal=True)
    return com_ponto.cast(pl.Decimal(20, 2), strict=False)


def sim_nao(coluna: str) -> pl.Expr:
    """'S' -> True, 'N' -> False, qualquer outra coisa -> NULL."""
    maiusculo = texto(coluna).str.to_uppercase()
    return pl.when(maiusculo == "S").then(True).when(maiusculo == "N").then(False).otherwise(None)


def lista_cnae(coluna: str) -> pl.Expr:
    """'4721102,4729699' -> ['4721102', '4729699']. Descarta itens que não são CNAEs válidos."""
    itens = texto(coluna).str.split(",").list.eval(pl.element().str.strip_chars())
    validos = itens.list.eval(pl.element().filter(pl.element().str.contains(r"^\d{7}$")))
    # Uma lista vazia não diz nada: guardamos NULL no lugar.
    return pl.when(validos.list.len() > 0).then(validos).otherwise(None)


def cep(coluna: str) -> pl.Expr:
    """Mantém só os dígitos. Um CEP válido tem exatamente 8."""
    digitos = texto(coluna).str.replace_all(r"\D", "")
    return pl.when(digitos.str.len_chars() == 8).then(digitos).otherwise(None)


def apenas_digitos(coluna: str) -> pl.Expr:
    """Para DDD e telefones: remove tudo o que não é dígito (traços, espaços, parênteses)."""
    digitos = texto(coluna).str.replace_all(r"\D", "")
    return pl.when(digitos == "").then(None).otherwise(digitos)


def email(coluna: str) -> pl.Expr:
    """Minúsculas; precisa ter o formato básico algo@dominio.ext."""
    minusculo = texto(coluna).str.to_lowercase()
    valido = minusculo.str.contains(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    return pl.when(valido).then(minusculo).otherwise(None)


def sigla_uf(coluna: str) -> pl.Expr:
    """Duas letras maiúsculas: 'pb' -> 'PB'. ('EX' indica estabelecimento no exterior.)"""
    maiusculo = texto(coluna).str.to_uppercase()
    return pl.when(maiusculo.str.contains(r"^[A-Z]{2}$")).then(maiusculo).otherwise(None)


# ---------------------------------------------------------------------------
# O "cardápio": qual receita usar em cada coluna de cada tabela
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegraTabela:
    receitas: dict[str, Receita]
    identidade: tuple[str, ...]  # colunas sem as quais a linha não faz sentido


def _dominio(tamanho_codigo: int | None) -> RegraTabela:
    receita_codigo = codigo(tamanho_codigo) if tamanho_codigo else texto
    return RegraTabela({"codigo": receita_codigo, "descricao": texto}, identidade=("codigo",))


REGRAS: dict[str, RegraTabela] = {
    "empresas": RegraTabela(
        receitas={
            "cnpj_basico": parte_cnpj(8),
            "razao_social": texto,
            "natureza_juridica": codigo(4),
            "qualificacao_responsavel": texto,
            "capital_social": dinheiro,
            "porte_empresa": codigo(2),
            "ente_federativo_responsavel": texto,
        },
        identidade=("cnpj_basico",),
    ),
    "estabelecimentos": RegraTabela(
        receitas={
            "cnpj_basico": parte_cnpj(8),
            "cnpj_ordem": parte_cnpj(4),
            "cnpj_dv": parte_cnpj(2, so_numeros=True),
            "identificador_matriz_filial": codigo(1),
            "nome_fantasia": texto,
            "situacao_cadastral": codigo(2),
            "data_situacao_cadastral": data,
            "motivo_situacao_cadastral": texto,
            "nome_cidade_exterior": texto,
            "pais": texto,
            "data_inicio_atividade": data,
            "cnae_fiscal_principal": codigo(7),
            "cnae_fiscal_secundaria": lista_cnae,
            "tipo_logradouro": texto,
            "logradouro": texto,
            "numero": texto,
            "complemento": texto,
            "bairro": texto,
            "cep": cep,
            "uf": sigla_uf,
            "municipio": codigo(4),
            "ddd_1": apenas_digitos,
            "telefone_1": apenas_digitos,
            "ddd_2": apenas_digitos,
            "telefone_2": apenas_digitos,
            "ddd_fax": apenas_digitos,
            "fax": apenas_digitos,
            "correio_eletronico": email,
            "situacao_especial": texto,
            "data_situacao_especial": data,
        },
        identidade=("cnpj_basico", "cnpj_ordem", "cnpj_dv"),
    ),
    "socios": RegraTabela(
        receitas={
            "cnpj_basico": parte_cnpj(8),
            "identificador_socio": codigo(1),
            "nome_socio_razao_social": texto,
            "cnpj_cpf_socio": texto,
            "qualificacao_socio": texto,
            "data_entrada_sociedade": data,
            "pais": texto,
            "representante_legal": texto,
            "nome_representante": texto,
            "qualificacao_representante_legal": texto,
            "faixa_etaria": codigo(1),
        },
        identidade=("cnpj_basico",),
    ),
    "simples": RegraTabela(
        receitas={
            "cnpj_basico": parte_cnpj(8),
            "opcao_simples": sim_nao,
            "data_opcao_simples": data,
            "data_exclusao_simples": data,
            "opcao_mei": sim_nao,
            "data_opcao_mei": data,
            "data_exclusao_mei": data,
        },
        identidade=("cnpj_basico",),
    ),
    "cnaes": _dominio(7),
    "municipios": _dominio(4),
    "naturezas": _dominio(4),
    "qualificacoes": _dominio(None),
    "paises": _dominio(None),
    "motivos": _dominio(None),
}

# Trava de consistência: todo tipo tem regra, e as regras cobrem exatamente as colunas do layout.
for _tipo, _colunas in LAYOUTS.items():
    if tuple(REGRAS[_tipo].receitas) != _colunas:
        raise RuntimeError(f"As regras de '{_tipo}' não batem com o layout em layouts.py.")


# ---------------------------------------------------------------------------
# A transformação em si
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoTransformacao:
    validos: pl.DataFrame
    rejeitados: pl.DataFrame
    anulados: dict[str, int]


def transformar(tipo: str, bruto: pl.DataFrame) -> ResultadoTransformacao:
    """Aplica as receitas de `tipo` a uma tabela bruta (só texto) e separa válidos e rejeitados."""
    if tipo not in REGRAS:
        raise ValueError(f"Tipo desconhecido: '{tipo}'.")
    regra = REGRAS[tipo]
    if tuple(bruto.columns) != LAYOUTS[tipo]:
        raise ValueError(f"As colunas recebidas não são as do layout de '{tipo}'.")

    # Aplica TODAS as receitas de uma vez. O Polars executa as colunas em paralelo,
    # usando todos os núcleos do processador.
    limpo = bruto.select([receita(nome).alias(nome) for nome, receita in regra.receitas.items()])

    # Motivo da rejeição: a lista das colunas de identidade que ficaram vazias/inválidas.
    motivo = pl.concat_str(
        [pl.when(pl.col(c).is_null()).then(pl.lit(c)) for c in regra.identidade],
        separator=",",
        ignore_nulls=True,
    )
    motivos = limpo.select(motivo.alias("motivo_rejeicao")).to_series()
    identidade_ok = motivos == ""

    validos = limpo.filter(identidade_ok)
    # Para os rejeitados guardamos a linha ORIGINAL (bruta), para podermos investigar depois.
    rejeitados = bruto.with_columns(motivos).filter(~identidade_ok)

    # Os anulados só são contados nas linhas aceitas (as rejeitadas já têm o seu próprio relatório).
    anulados = _contar_anulados(bruto.filter(identidade_ok), validos)
    return ResultadoTransformacao(validos, rejeitados, anulados)

def _contar_anulados(bruto: pl.DataFrame, limpo: pl.DataFrame) -> dict[str, int]:
    """Conta, por coluna, valores que existiam no arquivo mas foram descartados por inválidos."""
    anulados: dict[str, int] = {}
    for nome in bruto.columns:
        valor_original = bruto[nome].str.strip_chars().fill_null("")
        # '0' e '00000000' são o jeito da Receita dizer "sem data": não contam como anomalia.
        tinha_valor = ~valor_original.is_in(VAZIOS_DA_RECEITA)
        total = int((tinha_valor & limpo[nome].is_null()).sum())
        if total:
            anulados[nome] = total
    return anulados