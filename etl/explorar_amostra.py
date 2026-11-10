r"""Exploração de uma amostra dos arquivos de dados abertos do CNPJ.

O que este script faz:
1. Abre um .zip da Receita e lê SÓ as primeiras N linhas, sem descompactar o arquivo inteiro.
2. Traduz o texto de Latin-1 (codificação antiga da Receita) para UTF-8 (a que o Polars entende).
3. Confere se o número de colunas bate com o layout oficial.
4. Mostra um diagnóstico das "pegadinhas" que o ETL de verdade vai precisar tratar.

Uso (com o ambiente virtual ativado, dentro da pasta etl):
    python explorar_amostra.py ..\data\raw\2026-10\Estabelecimentos1.zip
    python explorar_amostra.py ..\data\raw\2026-10\Empresas1.zip --linhas 20000
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import zipfile
from itertools import islice
from pathlib import Path

import polars as pl

from layouts import ENCODING_RECEITA, LAYOUTS, SEPARADOR, tipo_pelo_nome

LINHAS_PADRAO = 50_000


class LayoutDivergenteError(Exception):
    """O arquivo tem um número de colunas diferente do layout oficial."""


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------


def ler_amostra(caminho_zip: Path, linhas: int) -> tuple[str, pl.DataFrame]:
    """Lê as primeiras `linhas` linhas de um .zip da Receita e devolve (tipo, tabela)."""
    tipo = tipo_pelo_nome(caminho_zip)
    colunas = LAYOUTS[tipo]

    with zipfile.ZipFile(caminho_zip) as arquivo_zip:
        # Cada .zip da Receita guarda exatamente UM arquivo de dados dentro.
        membros = [m for m in arquivo_zip.infolist() if not m.is_dir()]
        if len(membros) != 1:
            raise ValueError(f"Esperava 1 arquivo dentro do zip, encontrei {len(membros)}.")

        # zf.open() devolve um "cano" de bytes: os dados vão sendo descompactados aos poucos,
        # conforme pedimos. Por isso não precisamos extrair os vários GB para o disco.
        with arquivo_zip.open(membros[0]) as bytes_brutos:
            # TextIOWrapper é o "tradutor": transforma bytes Latin-1 em texto Python.
            texto = io.TextIOWrapper(bytes_brutos, encoding=ENCODING_RECEITA)
            # islice pega só as N primeiras linhas e para de ler (economiza tempo e memória).
            amostra = "".join(islice(texto, linhas))

    if not amostra:
        raise ValueError("O arquivo está vazio.")

    # O Polars só entende UTF-8, então re-codificamos o texto antes de entregar a ele.
    tabela = pl.read_csv(
        io.BytesIO(amostra.encode("utf-8")),
        has_header=False,  # a primeira linha já é dado, não cabeçalho
        separator=SEPARADOR,
        quote_char='"',  # os campos vêm entre aspas: "MINHA EMPRESA LTDA"
        infer_schema=False,  # tudo como TEXTO: preserva zeros à esquerda ("00000000")
    )

    # Trava de segurança: a Receita já mudou layouts sem aviso. Se o número de colunas
    # não bater, é melhor parar com um erro claro do que carregar dados embaralhados.
    if tabela.width != len(colunas):
        raise LayoutDivergenteError(
            f"O arquivo tem {tabela.width} colunas, mas o layout de '{tipo}' prevê "
            f"{len(colunas)}. A Receita pode ter mudado o formato."
        )

    # Troca os nomes genéricos (column_1, column_2...) pelos nomes do layout.
    return tipo, tabela.rename(dict(zip(tabela.columns, colunas, strict=True)))


# ---------------------------------------------------------------------------
# Diagnóstico
# ---------------------------------------------------------------------------


def titulo(texto: str) -> None:
    print(f"\n=== {texto} ===")


def preenchido(coluna: str) -> pl.Expr:
    """Verdadeiro quando a célula tem conteúdo (nem nula, nem só espaços)."""
    return pl.col(coluna).is_not_null() & (pl.col(coluna).str.strip_chars() != "")


def contagem(tabela: pl.DataFrame, coluna: str, limite: int = 10) -> None:
    """Mostra os valores mais frequentes de uma coluna (como um "contador de votos")."""
    titulo(f"Valores mais frequentes de '{coluna}'")
    print(tabela[coluna].value_counts(sort=True).head(limite))


def taxa_preenchimento(tabela: pl.DataFrame, colunas: list[str]) -> None:
    """Mostra quantos % das linhas têm cada coluna preenchida, SEM exibir os valores."""
    titulo("Preenchimento (% de linhas com valor)")
    resultado = tabela.select([(preenchido(c).mean() * 100).round(1).alias(c) for c in colunas])
    print(resultado)


def checar_cnpj_basico(tabela: pl.DataFrame) -> None:
    titulo("CNPJ básico: tamanho e presença de letras (CNPJ alfanumérico)")
    resumo = tabela.select(
        pl.col("cnpj_basico").str.len_chars().min().alias("menor_tamanho"),
        pl.col("cnpj_basico").str.len_chars().max().alias("maior_tamanho"),
        pl.col("cnpj_basico").str.contains(r"[A-Za-z]").sum().alias("com_letras"),
        pl.col("cnpj_basico").str.starts_with("0").sum().alias("comecam_com_zero"),
    )
    print(resumo)


def checar_datas(tabela: pl.DataFrame, coluna: str) -> None:
    titulo(f"Datas em '{coluna}' (formato esperado: AAAAMMDD)")
    validas = pl.col(coluna).str.contains(r"^(19|20)\d{6}$")
    print(
        tabela.select(
            validas.sum().alias("formato_valido"),
            (~validas & preenchido(coluna)).sum().alias("formato_estranho"),
            (~preenchido(coluna)).sum().alias("vazias"),
        )
    )
    estranhos = tabela.filter(~validas & preenchido(coluna))[coluna].unique().head(5)
    if estranhos.len() > 0:
        print(f"Exemplos de valores estranhos: {estranhos.to_list()}")


def diagnosticar_estabelecimentos(tabela: pl.DataFrame) -> None:
    checar_cnpj_basico(tabela)
    contagem(tabela, "situacao_cadastral")  # vamos ver: "02" ou "2"?
    contagem(tabela, "identificador_matriz_filial")
    contagem(tabela, "uf")
    checar_datas(tabela, "data_inicio_atividade")

    titulo("Código de município da Receita (compare: o código do IBGE tem 7 dígitos)")
    print(tabela["municipio"].unique().head(5).to_list())

    titulo("CNAEs secundários: vários códigos num único campo, separados por vírgula")
    com_secundario = tabela.filter(preenchido("cnae_fiscal_secundaria"))
    print(com_secundario["cnae_fiscal_secundaria"].head(3).to_list())
    maior_lista = com_secundario.select(
        pl.col("cnae_fiscal_secundaria").str.split(",").list.len().max()
    ).item()
    print(f"Maior quantidade de CNAEs secundários num só estabelecimento: {maior_lista}")

    taxa_preenchimento(
        tabela, ["nome_fantasia", "telefone_1", "telefone_2", "correio_eletronico", "cep"]
    )


def diagnosticar_empresas(tabela: pl.DataFrame) -> None:
    checar_cnpj_basico(tabela)
    contagem(tabela, "porte_empresa")
    contagem(tabela, "natureza_juridica", limite=5)

    titulo("Capital social: vem com VÍRGULA decimal (padrão brasileiro)")
    print(tabela["capital_social"].head(5).to_list())
    # Teste de conversão: trocamos a vírgula por ponto e tentamos virar número.
    convertido = (
        pl.col("capital_social").str.replace(",", ".", literal=True).cast(pl.Float64, strict=False)
    )
    falhas = tabela.select((convertido.is_null() & preenchido("capital_social")).sum()).item()
    print(f"Valores que NÃO viraram número após trocar vírgula por ponto: {falhas}")


def diagnosticar_socios(tabela: pl.DataFrame) -> None:
    checar_cnpj_basico(tabela)
    contagem(tabela, "identificador_socio")  # 1=PJ, 2=PF, 3=estrangeiro
    contagem(tabela, "faixa_etaria")

    titulo("CPF dos sócios: vem mascarado pela Receita (***123456**)")
    mascarado = pl.col("cnpj_cpf_socio").str.contains(r"^\*{3}\d{6}\*{2}$")
    # Mostramos só contagens: não precisamos exibir dados de pessoas físicas na tela.
    print(tabela.select(mascarado.sum().alias("cpfs_mascarados")))
    checar_datas(tabela, "data_entrada_sociedade")


def diagnosticar_simples(tabela: pl.DataFrame) -> None:
    checar_cnpj_basico(tabela)
    contagem(tabela, "opcao_simples")
    contagem(tabela, "opcao_mei")
    checar_datas(tabela, "data_opcao_simples")


def diagnosticar_dominio(tabela: pl.DataFrame) -> None:
    titulo("Primeiras linhas da tabela de domínio (a 'legenda')")
    print(tabela.head(10))


DIAGNOSTICOS = {
    "estabelecimentos": diagnosticar_estabelecimentos,
    "empresas": diagnosticar_empresas,
    "socios": diagnosticar_socios,
    "simples": diagnosticar_simples,
}


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Explora uma amostra de um .zip da Receita.")
    parser.add_argument("arquivo", type=Path, help="Caminho do .zip (ex.: Empresas1.zip)")
    parser.add_argument(
        "--linhas", type=int, default=LINHAS_PADRAO, help="Quantas linhas ler (padrão: 50000)"
    )
    args = parser.parse_args()

    if not args.arquivo.is_file():
        print(f"ERRO: arquivo não encontrado: {args.arquivo}", file=sys.stderr)
        return 1
    if args.linhas <= 0:
        print("ERRO: --linhas precisa ser maior que zero.", file=sys.stderr)
        return 1

    # Deixa as tabelas impressas mais largas e legíveis no terminal.
    pl.Config.set_tbl_rows(15)
    pl.Config.set_tbl_cols(12)
    pl.Config.set_fmt_str_lengths(60)

    inicio = time.perf_counter()
    try:
        tipo, tabela = ler_amostra(args.arquivo, args.linhas)
    except zipfile.BadZipFile:
        print("ERRO: o .zip está corrompido ou incompleto. Baixe novamente.", file=sys.stderr)
        return 1
    except (ValueError, LayoutDivergenteError, pl.exceptions.PolarsError) as erro:
        print(f"ERRO ao ler a amostra: {erro}", file=sys.stderr)
        return 1
    duracao = time.perf_counter() - inicio

    print(f"Arquivo: {args.arquivo.name} | tipo: {tipo}")
    print(f"Linhas lidas: {tabela.height:,} | colunas: {tabela.width} | tempo: {duracao:.2f}s")

    DIAGNOSTICOS.get(tipo, diagnosticar_dominio)(tabela)
    return 0


if __name__ == "__main__":
    sys.exit(main())