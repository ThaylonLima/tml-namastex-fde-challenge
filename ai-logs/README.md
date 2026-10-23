# ai-logs

Registro do trabalho assistido por IA (entregável do desafio). Sem segredos nem dados pessoais.

- `01-diretrizes-e-arquitetura.jsonl`: um registro por linha (`seq`, `phase`, `role`, `summary`).

## Varredura antes de commitar

    uv run python scripts/scan_ai_logs.py ai-logs/ --hostnames-file /caminho/privado/hostnames.txt

Categorias: TOKEN (JWT/chaves), BASE64 (blobs), IP_PRIVADO (RFC 1918), HOME_PATH (caminho de home do SO), EMAIL
e HOSTNAME. A lista de hostnames é **privada**, fica fora do repositório (ou em
`AI_LOGS_HOSTNAMES_FILE`); sem ela o script avisa que HOSTNAME não foi verificado. Saída 0 = limpo.

## Exportar um transcript bruto com redação

    uv run python scripts/scan_ai_logs.py --export SESSAO.jsonl --out ai-logs/sessao.jsonl \
        --user-email SEU@EMAIL --hostnames-file /caminho/privado/hostnames.txt

Só grava se a varredura do arquivo gerado não achar nada.

| Original | Substituído por |
|---|---|
| raiz do repositório | `<repo>` |
| diretório temporário da sessão | `<tmp>` |
| caminho de home do usuário, home do root | `<home>`, `<root-home>` |
| JWT e chaves conhecidas | `<token-redigido>` |
| blobs base64 longos | `<blob-redigido>` |
| IPs RFC 1918 | `<ip-privado>` |
| e-mails (exceto noreply do git/Anthropic) | `<email-redigido>` / `<user-email>` |

Ids públicos `claude.ai/code/session_*` (já nos trailers dos commits) são preservados.
