"""Configurações do ETL, lidas do arquivo .env da raiz do projeto (o "cofre").

O Pydantic confere cada valor ao carregar: se faltar a senha, ou se a porta não for um
número, o programa para logo no início com uma mensagem clara, em vez de falhar no meio
de uma carga de milhões de linhas.
"""

from functools import lru_cache
from pathlib import Path

from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# etl/config.py -> etl/ -> raiz do projeto
RAIZ_PROJETO = Path(__file__).resolve().parent.parent


class Configuracoes(BaseSettings):
    """Os nomes dos campos correspondem às variáveis do .env (maiúsculas ou minúsculas)."""

    model_config = SettingsConfigDict(
        env_file=RAIZ_PROJETO / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # o .env pode ter outras variáveis que não interessam ao ETL
    )

    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str
    postgres_user: str
    # SecretStr esconde a senha se a configuração for impressa por engano (aparece '**********').
    postgres_password: SecretStr

    # Endereço WebDAV do compartilhamento público da Receita (descoberto na Aula 8).
    # Se a Receita trocar o link, basta definir RFB_DAV_URL no .env, sem mexer no código.
    rfb_dav_url: str = (
        "https://arquivos.receitafederal.gov.br/public.php/dav/files/YggdBLfdninEJX9/"
    )

    @field_validator("rfb_dav_url")
    @classmethod
    def _url_valida(cls, valor: str) -> str:
        if not valor.startswith("https://"):
            raise ValueError("RFB_DAV_URL precisa começar com https://")
        # A barra final importa: sem ela, "juntar" o nome de uma pasta ao endereço daria errado.
        return valor if valor.endswith("/") else valor + "/"

    def conninfo(self) -> str:
        """Monta o "endereço completo" de conexão que o psycopg entende."""
        return make_conninfo(
            host=self.postgres_host,
            port=self.postgres_port,
            dbname=self.postgres_db,
            user=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            application_name="etl-cnpj",  # aparece no PostgreSQL, ajuda a identificar quem conectou
        )


@lru_cache
def obter_configuracoes() -> Configuracoes:
    """Lê o .env uma única vez e reaproveita o resultado nas próximas chamadas."""
    return Configuracoes()  # os valores obrigatórios vêm do .env