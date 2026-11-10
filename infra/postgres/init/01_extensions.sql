-- Executado UMA vez, no nascimento do banco (quando o volume de dados ainda está vazio).

-- pg_trgm: busca por semelhança de texto. Quebra as palavras em pedaços de 3 letras
-- e permite achar "padaria pao quente" mesmo que o registro seja "PADARIA PAO QUENTE LTDA ME".
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- btree_gin: permite misturar colunas comuns (uf, cnae) dentro de índices GIN,
-- combinando filtros exatos e busca de texto num único índice.
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- unaccent: remove acentos, para que "Joao Pessoa" encontre "João Pessoa".
CREATE EXTENSION IF NOT EXISTS unaccent;

-- pg_stat_statements: um "radar" que mede quanto tempo cada consulta leva.
-- Vamos usá-lo para garantir a meta de menos de 300 ms.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;