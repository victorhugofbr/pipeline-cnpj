-- =============================================================================
-- 001_tabelas_rfb.sql
-- Tabelas que recebem os dados abertos do CNPJ, já com os tipos corretos.
--
-- REGRA 1: este arquivo NÃO diz em qual schema ("gaveta") criar as tabelas.
--   Quem executa define isso antes, com SET search_path. Assim, o mesmo arquivo
--   serve para a gaveta definitiva (rfb) e para a gaveta temporária de carga
--   (rfb_carga), que usaremos para atualizar os dados sem tirar o site do ar.
--
-- REGRA 2: este arquivo NÃO cria chaves primárias nem índices.
--   Eles serão criados DEPOIS da carga (arquivo 002): montar o índice uma vez,
--   no final, é muito mais rápido do que atualizá-lo a cada uma das ~70 milhões
--   de linhas inseridas.
--
-- REGRA 3: os nomes e a ORDEM das colunas são idênticos aos de etl/layouts.py,
--   para que o carregador possa copiar os dados coluna a coluna, sem tradução.
--
-- Tipos escolhidos:
--   * Códigos (CNAE, situação, porte...) são TEXTO: são rótulos, não quantidades.
--     Ninguém soma dois CNAEs, e texto preserva zeros à esquerda ('02', '0111301').
--   * varchar(n) quando o tamanho é oficial e conhecido; text quando é livre ou
--     ainda não foi medido.
--   * Datas viram DATE (4 bytes, permitem filtros como "abertas este ano").
--   * Dinheiro vira NUMERIC, nunca float (float erra centavos).
--   * NOT NULL apenas na identidade da linha (o CNPJ). O resto pode faltar.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Tabelas de domínio ("legendas"): código -> descrição
-- Os nomes das tabelas são iguais aos "tipos" de arquivo em etl/layouts.py.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cnaes (
    codigo    varchar(7) NOT NULL,  -- ex.: '5611201'
    descricao text       NOT NULL
);

CREATE TABLE IF NOT EXISTS municipios (
    codigo    varchar(4) NOT NULL,  -- código próprio da Receita (não é o do IBGE), ex.: '2051'
    descricao text       NOT NULL   -- a Receita grava os nomes SEM acento: 'NOVA UNIAO'
);

CREATE TABLE IF NOT EXISTS naturezas (
    codigo    varchar(4) NOT NULL,  -- ex.: '2062' (Sociedade Empresária Limitada)
    descricao text       NOT NULL
);

CREATE TABLE IF NOT EXISTS qualificacoes (
    codigo    text NOT NULL,
    descricao text NOT NULL
);

CREATE TABLE IF NOT EXISTS paises (
    codigo    text NOT NULL,
    descricao text NOT NULL
);

CREATE TABLE IF NOT EXISTS motivos (
    codigo    text NOT NULL,
    descricao text NOT NULL
);


-- -----------------------------------------------------------------------------
-- EMPRESAS: 1 linha por empresa (o "prédio", identificado pelo CNPJ básico)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS empresas (
    cnpj_basico                 varchar(8)     NOT NULL,  -- texto: zeros à esquerda e letras
    razao_social                text,
    natureza_juridica           varchar(4),               -- -> naturezas.codigo
    qualificacao_responsavel    text,                     -- -> qualificacoes.codigo
    capital_social              numeric(20, 2),           -- já vimos valores de R$ 120 bilhões
    porte_empresa               varchar(2),               -- '00', '01', '03', '05'
    ente_federativo_responsavel text                      -- só órgãos públicos (natureza 1xxx)
);


-- -----------------------------------------------------------------------------
-- ESTABELECIMENTOS: matriz e filiais (os "apartamentos"). É aqui que ficam
-- endereço, atividade, situação e contatos: o coração da geração de leads.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS estabelecimentos (
    cnpj_basico                 varchar(8) NOT NULL,
    cnpj_ordem                  varchar(4) NOT NULL,
    cnpj_dv                     varchar(2) NOT NULL,
    identificador_matriz_filial varchar(1),   -- '1' matriz, '2' filial
    nome_fantasia               text,
    situacao_cadastral          varchar(2),   -- '01','02','03','04','08' (sempre 2 dígitos)
    data_situacao_cadastral     date,
    motivo_situacao_cadastral   text,         -- -> motivos.codigo
    nome_cidade_exterior        text,
    pais                        text,         -- -> paises.codigo
    data_inicio_atividade       date,
    cnae_fiscal_principal       varchar(7),   -- -> cnaes.codigo
    cnae_fiscal_secundaria      text[],       -- LISTA: o arquivo traz 'a,b,c' (vimos até 99!)
    tipo_logradouro             text,
    logradouro                  text,
    numero                      text,         -- texto: pode ser 'S/N'
    complemento                 text,
    bairro                      text,
    cep                         varchar(8),
    uf                          varchar(2),   -- 'EX' para estabelecimentos no exterior
    municipio                   varchar(4),   -- -> municipios.codigo
    ddd_1                       text,
    telefone_1                  text,
    ddd_2                       text,
    telefone_2                  text,
    ddd_fax                     text,
    fax                         text,
    correio_eletronico          text,
    situacao_especial           text,
    data_situacao_especial      date
);


-- -----------------------------------------------------------------------------
-- SÓCIOS: quadro de sócios e administradores. Pertence à EMPRESA (cnpj_basico),
-- não a cada filial. Não existe uma chave única "natural" aqui.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS socios (
    cnpj_basico                      varchar(8) NOT NULL,
    identificador_socio              varchar(1),  -- '1' PJ, '2' PF, '3' estrangeiro
    nome_socio_razao_social          text,
    cnpj_cpf_socio                   text,        -- CPF chega mascarado: '***123456**'
    qualificacao_socio               text,        -- -> qualificacoes.codigo
    data_entrada_sociedade           date,
    pais                             text,        -- -> paises.codigo
    representante_legal              text,        -- CPF mascarado do representante
    nome_representante               text,
    qualificacao_representante_legal text,        -- -> qualificacoes.codigo
    faixa_etaria                     varchar(1)   -- '0' a '9'
);


-- -----------------------------------------------------------------------------
-- SIMPLES: opção pelo Simples Nacional e pelo MEI (no máximo 1 linha por empresa)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS simples (
    cnpj_basico           varchar(8) NOT NULL,
    opcao_simples         boolean,  -- 'S' -> true, 'N' -> false, vazio -> NULL
    data_opcao_simples    date,
    data_exclusao_simples date,
    opcao_mei             boolean,
    data_opcao_mei        date,
    data_exclusao_mei     date
);