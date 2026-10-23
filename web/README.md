# Frontend de demonstração (extra, fora do escopo avaliado)

Chat web para conversar com o agente sem montar requisições à mão. **Não altera o agente**:
nenhum arquivo em `agent/`, `quote-service/`, `tests/` nem o `docker-compose.yml` oficial foi tocado.

| Arquivo | Papel |
|---|---|
| `index.html` | Chat em HTML/CSS/JS puro, sem dependências externas (sem CDN). Consome `POST /chat` e `GET /health?deep=true`. |
| `nginx.conf` | Serve o chat em `/autoseguro/` e faz proxy reverso de **apenas** `/autoseguro/api/chat` e `/autoseguro/api/health` para `agent:8080`. |
| `../docker-compose.web.yml` | Override que adiciona o serviço `web` (nginx alpine) em `127.0.0.1:8090`. |

```bash
docker compose -f docker-compose.yml -f docker-compose.web.yml up -d web
# http://localhost:8090/autoseguro/
```

## Decisões

- **Mesmo domínio, sem CORS**: o navegador fala só com o nginx; o agente continua sem CORS e sem rota nova.
- **Superfície mínima**: `/sessions/{id}` e `/maintenance/sweep` não são publicados; qualquer outro caminho retorna 404.
- **Demo pública**: rate limit por visitante (IP real via `CF-Connecting-IP` do Cloudflare Tunnel),
  corpo limitado a 16 KB, CSP restritiva e headers de segurança.
- **Regra de Ouro preservada**: o card de cotação exibe as strings Decimal recebidas da API, só com a mesma
  formatação de `render.format_brl` (separadores), sem nenhum cálculo no navegador.
- **LGPD**: o `session_id` é aleatório (`web-<uuid>`), guardado só no `sessionStorage` da aba; o CEP é exibido
  apenas pelo prefixo devolvido pela API (`slots.cep_prefix`).
