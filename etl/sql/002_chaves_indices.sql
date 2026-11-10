-- =============================================================================
-- 002_chaves_indices.sql
-- Chaves primárias e índices das tabelas da Receita. Roda DEPOIS da carga.
--
-- Assim como o 001, este arquivo NÃO fixa o schema: quem executa define antes,
-- com SET search_path (rfb ou rfb_carga).
--
-- Idempotente: pode rodar várias vezes. O que já existe é mantido.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. CHAVES PRIMÁRIAS: a "promessa" de que não existem duas linhas com a mesma
--    identidade. O PostgreSQL cria automaticamente um índice para cumpri-la, que
--    também deixa instantânea a busca de UM CNPJ (a página /cnpj/[cnpj]).
--
--    A Receita às vezes publica a MESMA chave mais de uma vez (descobrimos isso na
--    primeira carga completa). Estratégia "tenta; se falhar, limpa e tenta de novo":
--      a) Tentamos criar a chave. Se não há repetidos (o normal), acabou. Custo zero.
--      b) Se falhar por repetição (erro unique_violation):
--         - guardamos TODAS as versões das chaves repetidas em <tabela>_duplicados,
--           para podermos investigar depois (primeiro as evidências, depois a regra);
--         - mantemos a versão MAIS COMPLETA (a que tem mais campos com informação); em
--           caso de empate, a carregada por último. As demais são removidas. (Na carga
--           de 2026-10, a única empresa repetida tinha uma versão "casca vazia", com
--           natureza '0000' e sem razão social, e outra completa: manter "a última"
--           poderia ficar justamente com a casca.)
--         - se as cópias passarem de 1% da tabela, PARAMOS: isso indica um problema
--           maior (ex.: o mesmo arquivo carregado duas vezes), não um ruído da Receita;
--         - criamos a chave de novo.
--
--    "ADD PRIMARY KEY" não tem "IF NOT EXISTS", por isso conferimos antes se a chave
--    já existe (o arquivo continua podendo rodar várias vezes).
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    -- tabela -> colunas da chave
    chaves CONSTANT jsonb := '{
        "empresas":         "cnpj_basico",
        "estabelecimentos": "cnpj_basico, cnpj_ordem, cnpj_dv",
        "simples":          "cnpj_basico",
        "cnaes":            "codigo",
        "municipios":       "codigo",
        "naturezas":        "codigo",
        "qualificacoes":    "codigo",
        "paises":           "codigo",
        "motivos":          "codigo"
    }';
    limite_copias CONSTANT numeric := 0.01;  -- 1% da tabela
    tabela      text;
    colunas     text;
    total       bigint;
    removidas   bigint;
BEGIN
    FOR tabela, colunas IN SELECT * FROM jsonb_each_text(chaves) LOOP
        -- to_regclass respeita o search_path: encontra a tabela na gaveta escolhida.
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = to_regclass(tabela) AND contype = 'p'
        );

        RAISE NOTICE 'Criando chave primária de % (%)', tabela, colunas;
        BEGIN
            EXECUTE format('ALTER TABLE %I ADD PRIMARY KEY (%s)', tabela, colunas);
        EXCEPTION WHEN unique_violation THEN
            -- (a) Evidências: todas as versões de cada chave repetida.
            EXECUTE format('DROP TABLE IF EXISTS %I', tabela || '_duplicados');
            EXECUTE format(
                'CREATE TABLE %I AS SELECT * FROM %I WHERE (%s) IN '
                '(SELECT %s FROM %I GROUP BY %s HAVING count(*) > 1)',
                tabela || '_duplicados', tabela, colunas, colunas, tabela, colunas
            );

            -- (b) Mantém a versão mais completa e apaga as outras.
            --     * completude: quantos campos da linha têm informação de verdade.
            --       to_jsonb transforma a linha num "dicionário" (coluna -> valor) e
            --       jsonb_each_text percorre os valores. Não contam: vazios (NULL) e valores
            --       feitos só de zeros ('0000', '00', 0.00), o jeito da Receita dizer
            --       "sem informação". A chave em si é igual em todas as versões, então não
            --       influencia a comparação.
            --     * desempate: maior ctid (o "endereço físico" da linha) = carregada por último.
            EXECUTE format(
                'DELETE FROM %1$I alvo USING ('
                '  SELECT linha, row_number() OVER ('
                '    PARTITION BY %2$s ORDER BY completude DESC, linha DESC'
                '  ) AS posicao'
                '  FROM ('
                '    SELECT t.*, t.ctid AS linha,'
                '      (SELECT count(*) FROM jsonb_each_text(to_jsonb(t)) campo'
                '        WHERE campo.value IS NOT NULL AND campo.value !~ ''^[0.]*$'')'
                '        AS completude'
                '    FROM %1$I t WHERE (%2$s) IN (SELECT %2$s FROM %3$I)'
                '  ) candidatas'
                ') repetidas WHERE alvo.ctid = repetidas.linha AND repetidas.posicao > 1',
                tabela, colunas, tabela || '_duplicados'
            );
            GET DIAGNOSTICS removidas = ROW_COUNT;

            -- (c) Disjuntor: cópia demais não é ruído, é sinal de problema.
            EXECUTE format('SELECT count(*) FROM %I', tabela) INTO total;
            IF removidas > total * limite_copias THEN
                RAISE EXCEPTION '%: % cópias repetidas (mais de 1%% de % linhas). Carga suspeita.',
                    tabela, removidas, total;
            END IF;

            RAISE WARNING '%: % linhas repetidas removidas (todas as versões guardadas em %).',
                tabela, removidas, tabela || '_duplicados';

            -- (d) Agora sim.
            EXECUTE format('ALTER TABLE %I ADD PRIMARY KEY (%s)', tabela, colunas);
        END;
    END LOOP;
END
$$;


-- -----------------------------------------------------------------------------
-- 2. SÓCIOS: não há chave única natural, mas sempre buscamos "os sócios da
--    empresa X". Um índice comum em cnpj_basico resolve.
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_socios_cnpj_basico ON socios (cnpj_basico);


-- -----------------------------------------------------------------------------
-- 3. BUSCA POR SEGMENTO E REGIÃO (as páginas /empresas/[uf]/[cnae] e a busca):
--
--    * Índice COMPOSTO (uf, cnae_fiscal_principal): como uma lista telefônica
--      ordenada primeiro por estado e, dentro de cada estado, por atividade.
--      Achar "padarias da PB" vira abrir direto na página certa.
--
--    * Índice PARCIAL (WHERE situacao_cadastral = '02'): só guarda as empresas
--      ATIVAS. Como a maioria da base é de empresas baixadas, o índice fica
--      muito menor e mais rápido. Ele é usado sempre que a consulta pede
--      situacao_cadastral = '02' (o filtro padrão do nosso produto).
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_estab_ativos_uf_cnae
    ON estabelecimentos (uf, cnae_fiscal_principal)
    WHERE situacao_cadastral = '02';


-- -----------------------------------------------------------------------------
-- 4. Estatísticas atualizadas para o planejador (o "mapa do GPS").
-- -----------------------------------------------------------------------------
ANALYZE empresas;
ANALYZE estabelecimentos;
ANALYZE socios;
ANALYZE simples;