-- =============================================================================
-- diag_arquivos_orfaos.sql
-- Diagnóstico: arquivos de dados que NÃO pertencem a nenhuma tabela ou índice.
--
-- Como surgem: quando o banco cai no meio da criação de uma tabela ou índice, o
-- arquivo já existe no disco, mas a transação nunca terminou. O PostgreSQL desfaz a
-- transação ao religar, mas não apaga esse arquivo: ele vira "entulho de obra".
--
-- Só LÊ informações (não apaga nada). Precisa de superusuário (o cnpj_admin é).
-- ATENÇÃO: rode com o ETL parado. Um índice sendo criado neste momento ainda não
-- aparece no catálogo e seria contado, por engano, como órfão.
-- =============================================================================
SELECT count(*) AS arquivos_orfaos,
       pg_size_pretty(coalesce(sum((pg_stat_file(pasta || '/' || arquivo)).size), 0)) AS espaco_orfao
FROM (SELECT 'base/' || oid AS pasta FROM pg_database WHERE datname = current_database()) d,
     LATERAL pg_ls_dir(d.pasta) AS arquivo
WHERE arquivo ~ '^[0-9]'  -- só arquivos de dados (os nomes são números)
  -- "16385.1" e "16385_vm" pertencem ao mesmo objeto "16385": tiramos o sufixo para comparar.
  AND regexp_replace(arquivo, '[._].*$', '') NOT IN (
      SELECT pg_relation_filenode(oid)::text
      FROM pg_class
      WHERE pg_relation_filenode(oid) IS NOT NULL
  );