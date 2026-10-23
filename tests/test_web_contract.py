"""Contrato estatico da interface web (web/ + docker-compose.web.yml + Dockerfile).

Sem dependencias novas: le os arquivos como texto. Os comportamentos dinamicos (nginx real,
navegador) sao validados a parte -- ver docs/ARQUITETURA.md §13.5. Estes testes impedem regressao
de configuracao e derivas de contrato entre o agente e a interface.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.app import MESSAGE_ID_PATTERN
from agent.fsm import State
from agent.handoff import Reason
from agent.quote_client import QuoteClientConfig

ROOT = Path(__file__).resolve().parent.parent


def _strip_comments(text: str) -> str:
    return re.sub(r"(?m)#.*$", "", text)


RAW_NGINX = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
NGINX = _strip_comments(RAW_NGINX)
MAIN = _strip_comments((ROOT / "web" / "nginx.main.conf").read_text(encoding="utf-8"))
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
COMPOSE_RAW = (ROOT / "docker-compose.web.yml").read_text(encoding="utf-8")
COMPOSE = _strip_comments(COMPOSE_RAW)
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
SCRIPT = HTML[HTML.index("<script>") + 8 : HTML.index("</script>")]


def _locations() -> dict[str, str]:
    """`location [=] /path { corpo }` -> corpo, com contagem de chaves (aceita uma linha)."""
    result: dict[str, str] = {}
    for m in re.finditer(r"location\s+(=\s+)?(\S+)\s*\{", NGINX):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(NGINX[i], 0)
            i += 1
        key = m.group(2) if m.group(1) else "prefix:" + m.group(2)  # exata x prefixo
        result[key] = NGINX[m.end() : i - 1]
    return result


def _js_object(name: str) -> dict[str, str]:
    """Pares chave: valor de um objeto literal `const NAME = { ... };` do script."""
    block = re.search(rf"const {name}\s*=\s*\{{(.*?)\n\s*\}};", SCRIPT, re.S)
    assert block, f"{name} nao encontrado no script"
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r'\b([A-Za-z0-9_]+):\s*"((?:[^"\\]|\\.)*)"', block.group(1))
    }


# ================================================ proxy: superficie minima
def test_only_the_chat_route_is_proxied_and_only_for_post() -> None:
    locs = _locations()
    proxied = {p for p, body in locs.items() if "proxy_pass" in body}
    assert proxied == {"/autoseguro/api/chat"}
    assert re.findall(r"proxy_pass\s+http://agent_api(/\S*);", NGINX) == ["/chat"]
    assert "limit_except POST" in locs["/autoseguro/api/chat"]
    assert locs["prefix:/"].strip() == "return 404;"  # tudo o mais e 404
    assert set(locs) == {"/", "/autoseguro", "/autoseguro/", "/autoseguro/api/chat", "prefix:/"}


def test_the_health_route_and_its_rate_limit_zone_are_gone() -> None:
    assert "api/health" not in NGINX and "zone=health" not in NGINX
    assert "api/health" not in HTML and "deep=true" not in HTML and "deep=true" not in NGINX
    assert not re.search(r"fetch\([^)]*health", SCRIPT)
    assert not re.search(r"setInterval", SCRIPT)  # sem polling


def test_the_web_healthcheck_points_to_a_route_that_exists() -> None:
    url = re.search(r'wget",\s*"-qO-",\s*"http://127\.0\.0\.1(/[^"]*)"', COMPOSE)
    assert url, "healthcheck do web nao encontrado"
    assert url.group(1) == "/autoseguro/"
    assert url.group(1) in _locations()  # location exata existente
    assert "health" not in url.group(1)


def test_security_headers_use_always_and_no_location_resets_inheritance() -> None:
    adds = re.findall(r"^\s*add_header\s+(.+);\s*$", NGINX, re.M)
    assert len(adds) >= 5 and all(a.rstrip().endswith("always") for a in adds)
    assert not any("add_header" in body for body in _locations().values())
    csp = next(a for a in adds if a.startswith("Content-Security-Policy"))
    for needle in (
        "default-src 'none'",
        "frame-ancestors 'none'",
        "connect-src 'self'",
        "base-uri 'none'",
    ):
        assert needle in csp


# ================================================ R1: upstream que re-resolve
def test_upstream_resolves_dns_dynamically_and_keeps_keepalive() -> None:
    upstream = re.search(r"upstream agent_api\s*\{(.*?)\n\}", NGINX, re.S)
    assert upstream
    body = upstream.group(1)
    assert re.search(r"zone\s+agent_api\s+\S+;", body)  # `resolve` exige zona compartilhada
    assert re.search(r"server\s+agent:8080\s+resolve;", body)
    assert "keepalive" in body
    assert re.search(r"resolver\s+127\.0\.0\.11\s+valid=10s", NGINX)


def test_compose_pins_a_compatible_nginx_and_waits_for_a_healthy_agent() -> None:
    image = re.search(r"image:\s*nginx:(\d+)\.(\d+)[\w.-]*", COMPOSE)
    assert image, "imagem do nginx deve estar fixada (nada de latest)"
    assert (int(image.group(1)), int(image.group(2))) >= (1, 27)  # `resolve` em upstream: >= 1.27.3
    assert "latest" not in COMPOSE
    assert re.search(r"depends_on:\s*\n\s*agent:\s*\n\s*condition:\s*service_healthy", COMPOSE)
    assert "HEALTHCHECK" in DOCKERFILE  # o service_healthy depende dele


# ================================================ R3: exposicao
def test_compose_override_binds_every_published_port_to_loopback() -> None:
    for service in ("agent", "quote-api"):
        block = re.search(
            rf"  {service}:\s*\n\s*ports: !override\s*\n((?:\s*-\s*\"[^\"]+\"\s*\n)+)", COMPOSE
        )
        assert block, f"{service} sem `ports: !override`"
        assert all(p.startswith("127.0.0.1:") for p in re.findall(r'"([^"]+)"', block.group(1)))
    web_ports = re.search(r"  web:.*?ports:\s*\n((?:\s*-\s*\"[^\"]+\"\s*\n)+)", COMPOSE, re.S)
    assert web_ports and all(
        p.startswith("127.0.0.1:") for p in re.findall(r'"([^"]+)"', web_ports.group(1))
    )


def test_the_compose_documents_the_minimum_compose_version_for_override() -> None:
    assert "2.24.4" in COMPOSE_RAW


# ================================================ R6: IP real
def test_real_ip_is_trusted_only_from_the_fixed_compose_subnet() -> None:
    subnet = re.search(r"subnet:\s*(\d+\.\d+\.\d+\.\d+/\d+)", COMPOSE)
    assert subnet, "sub-rede do compose deve ser fixa"
    assert re.search(rf"set_real_ip_from\s+{re.escape(subnet.group(1))};", NGINX)
    assert re.search(r"real_ip_header\s+CF-Connecting-IP;", NGINX)
    assert re.search(r"limit_req_zone\s+\$binary_remote_addr\s+zone=chat:", NGINX)
    assert "$http_cf_connecting_ip" not in NGINX  # a chave nunca e o header cru (forjavel)


# ================================================ LGPD: logs sem IP
def test_access_log_format_has_no_visitor_identifiers_or_query_string() -> None:
    fmt = re.search(r"log_format\s+privacy\s+(.*?);", NGINX, re.S)
    assert fmt
    for forbidden in (
        "$remote_addr", "$binary_remote_addr", "$realip_remote_addr", "$http_", "$request ",
        "$request\"", "$args", "$query_string", "$request_uri", "$uri", "$remote_user",
        "$proxy_add_x_forwarded_for",
    ):  # fmt: skip
        assert forbidden not in fmt.group(1) + " ", forbidden
    assert re.search(r"access_log\s+/dev/stdout\s+privacy;", NGINX)
    assert re.search(r"map\s+\$request_uri\s+\$log_route", NGINX)  # rotulo, nunca o caminho


def test_error_log_threshold_hides_client_ips_and_limit_req_is_below_it() -> None:
    assert re.search(r"error_log\s+/dev/stderr\s+crit;", MAIN)
    assert re.search(r"access_log\s+off;", MAIN)
    assert re.search(r"limit_req_log_level\s+warn;", NGINX)  # warn e menos severo que crit
    assert "./web/nginx.main.conf:/etc/nginx/nginx.conf:ro" in COMPOSE


def test_proxy_strips_every_header_that_carries_the_visitor_ip() -> None:
    chat = _locations()["/autoseguro/api/chat"]
    for header in (
        "CF-Connecting-IP", "CF-Connecting-IPv6", "CF-IPCountry", "True-Client-IP",
        "X-Forwarded-For", "X-Real-IP", "Forwarded",
    ):  # fmt: skip
        assert re.search(rf'proxy_set_header\s+{header}\s+"";', chat), header
    assert "X-Forwarded-For $proxy_add" not in chat


def test_uvicorn_access_log_is_disabled_in_the_image() -> None:
    assert "--no-access-log" in DOCKERFILE


# ================================================ endurecimento e timeouts
def test_timeout_chain_is_ordered_quote_deadline_lt_nginx_lt_browser() -> None:
    nginx_read = int(re.search(r"proxy_read_timeout\s+(\d+)s;", NGINX).group(1))  # type: ignore[union-attr]
    browser = int(re.search(r"REQUEST_TIMEOUT_MS\s*=\s*(\d+)", SCRIPT).group(1)) / 1000  # type: ignore[union-attr]
    assert QuoteClientConfig().deadline < nginx_read < browser


def test_slow_client_and_dead_upstream_limits() -> None:
    def seconds(directive: str) -> int:
        return int(re.search(rf"{directive}\s+(\d+)s;", NGINX).group(1))  # type: ignore[union-attr]

    assert seconds("proxy_connect_timeout") <= 5
    assert seconds("client_header_timeout") <= 15 and seconds("client_body_timeout") <= 15
    assert "proxy_next_upstream off;" in NGINX  # nunca repete POST: o replay e do message_id
    maxlen = int(re.search(r'maxlength="(\d+)"', HTML).group(1))  # type: ignore[union-attr]
    limit = int(re.search(r"client_max_body_size\s+(\d+)k;", NGINX).group(1)) * 1024  # type: ignore[union-attr]
    assert maxlen * 4 + 512 <= limit <= 64 * 1024  # pior caso UTF-8 + envelope JSON


# ================================================ interface: contrato com o agente
def test_every_handoff_reason_has_a_human_text() -> None:
    texts = _js_object("HANDOFF_REASON")
    for reason in Reason:
        assert reason.value in texts, f"{reason.value} sem texto humano na interface"
        assert len(texts[reason.value]) >= 25 and texts[reason.value] != reason.value
    assert "HANDOFF_REASON[h.reason] ||" in SCRIPT  # fallback para codigo novo/desconhecido


def test_the_ui_knows_every_state_and_quote_status_the_agent_can_emit() -> None:
    states = set(_js_object("STATE_LABEL"))
    for state in State:
        assert state.value in states, state
    emitted = set(
        re.findall(
            r'quote_status\s*=\s*"([a-z_]+)"', (ROOT / "agent" / "conversation.py").read_text()
        )
    )
    emitted |= {"none"}  # valor inicial de Session.quote_status
    known = set(_js_object("QUOTE_STATUS"))
    assert emitted <= known, f"quote_status sem rotulo na UI: {emitted - known}"
    alerts = set(_js_object("QUOTE_ALERT"))
    assert {
        "unavailable",
        "circuit_open",
        "refused",
        "invalid_payload",
        "restored_without_quote",
    } <= alerts
    assert "ok" not in alerts and "none" not in alerts


def test_message_id_contract_between_ui_and_agent() -> None:
    assert "message_id: entry.id" in SCRIPT  # enviado no corpo
    assert re.search(r"if \(c && typeof c\.randomUUID === \"function\"\)", SCRIPT)
    assert "getRandomValues" in SCRIPT  # fallback sem randomUUID (contexto nao seguro)
    # o id gerado pelo UUID v4 (36) e pelo fallback hex (32) respeitam o padrao do agente
    pattern = re.compile(MESSAGE_ID_PATTERN)
    assert pattern.match("123e4567-e89b-42d3-a456-426614174000") and pattern.match("a" * 32)
    # 409: UMA nova tentativa, com NOVO id (e aviso ao usuario), nunca laco
    assert re.search(
        r"if \(res\.status === 409 && attempt === 0\) \{"
        r"[^}]*entry\.id = newId\(\);[^}]*addRow\(\"system\"",
        SCRIPT,
    )
    assert "X-Idempotent-Replay" in SCRIPT


def test_retry_reuses_the_same_entry_and_never_forces_busy_false() -> None:
    handler = re.search(
        r"btn\.addEventListener\(\"click\", \(\) => \{(.*?)\}, \{ once: true \}\);", SCRIPT, re.S
    )
    assert handler, "handler do Reenviar nao encontrado"
    body = handler.group(1)
    assert "if (busy) return;" in body and "deliver(entry)" in body
    assert (
        "busy = false" not in body and "transcript" not in body
    )  # nada de mexer no transcript/busy
    assert "clearErrorRows();" in SCRIPT  # Reenviar antigo nao sobrevive a uma nova mensagem


def test_orphan_message_is_resumed_on_boot_and_terminal_transcript_is_not_persisted() -> None:
    assert "resumePending();" in SCRIPT and 'last.status === "pending"' in SCRIPT
    assert "ended: true" in SCRIPT and "state.ended" in SCRIPT  # R7
    assert 'STORE_KEY = "autoseguro.chat.v2"' in SCRIPT  # estado antigo (sem ids) e descartado


def test_ui_loads_nothing_from_the_network_except_its_own_api() -> None:
    assert not re.findall(r"""(?:src|href)=["'](?:https?:)?//""", HTML)
    assert not re.findall(r"""url\(["']?https?:""", HTML)
    assert re.findall(r"fetch\(([^,)]+)", SCRIPT) == ["API_CHAT"]
    assert 'API_CHAT = "api/chat"' in SCRIPT


@pytest.mark.skipif(shutil.which("node") is None, reason="node nao instalado")
def test_the_inline_script_is_syntactically_valid_javascript() -> None:
    result = subprocess.run(
        ["node", "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"],
        input=SCRIPT, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
