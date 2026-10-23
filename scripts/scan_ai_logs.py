"""Varredura de seguranca de `ai-logs/` ANTES de commitar, e exportacao com redacao reprodutivel.

    uv run python scripts/scan_ai_logs.py ai-logs/                 # sai com codigo 1 se achar algo
    uv run python scripts/scan_ai_logs.py ai-logs/ --hostnames-file ~/privado/hostnames.txt
    uv run python scripts/scan_ai_logs.py --export SRC.jsonl --out ai-logs/sessao.jsonl

Categorias: TOKEN (JWT, chaves conhecidas), BASE64 (blobs longos), IP_PRIVADO (RFC 1918),
HOME_PATH (`/home/`), EMAIL e HOSTNAME (lista PRIVADA do owner, fora do repositorio:
--hostnames-file ou AI_LOGS_HOSTNAMES_FILE). O relatorio mostra arquivo:linha:categoria e um trecho
MASCARADO, nunca o valor inteiro.

`--export` copia um transcript JSONL aplicando redacao deterministica (mapa em ai-logs/README.md) e
so grava se a varredura do arquivo gerado nao achar nada. Sem a lista de hostnames a categoria
HOSTNAME nao e verificada, e o relatorio diz isso.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

JWT: Final = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
KNOWN_TOKEN: Final = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
BASE64_CANDIDATE: Final = re.compile(
    r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/=_-])"
)
RFC1918: Final = re.compile(
    r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2})(?![\d.])"
)
HOME_PATH: Final = re.compile(r"(?<![A-Za-z0-9_-])/home/[A-Za-z0-9_.-]*")
EMAIL: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Id de sessao do claude.ai: ja e publico nos trailers dos commits (Claude-Session:).
BENIGN: Final = re.compile(r"claude\.ai/code/session_[A-Za-z0-9]+")
ALLOWED_EMAILS: Final = frozenset(
    {"noreply@anthropic.com", "149634768+ThaylonLima@users.noreply.github.com"}
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    category: str
    excerpt: str  # mascarado


def _mask(value: str) -> str:
    return value if len(value) <= 6 else f"{value[:3]}…{value[-2:]} ({len(value)} chars)"


def _looks_like_base64(token: str) -> bool:
    if re.fullmatch(r"[0-9a-fA-F]{40,}", token):  # hash hex (git/sha256), nao e segredo
        return False
    classes = sum(bool(re.search(p, token)) for p in ("[a-z]", "[A-Z]", "[0-9]"))
    return classes == 3 and token.count("/") < 3 and not token.startswith(("-", "_"))


def scan_text(text: str, path: str = "<memoria>", hostnames: Iterable[str] = ()) -> list[Finding]:
    hosts = [h.strip().lower() for h in hostnames if h.strip() and not h.startswith("#")]
    findings: list[Finding] = []
    for n, line in enumerate(text.splitlines(), 1):
        line = BENIGN.sub("<sessao-claude>", line)
        for rx, cat in (
            (JWT, "TOKEN"),
            (KNOWN_TOKEN, "TOKEN"),
            (RFC1918, "IP_PRIVADO"),
            (HOME_PATH, "HOME_PATH"),
        ):
            findings += [Finding(path, n, cat, _mask(m.group(0))) for m in rx.finditer(line)]
        findings += [
            Finding(path, n, "BASE64", _mask(m.group(0)))
            for m in BASE64_CANDIDATE.finditer(line)
            if _looks_like_base64(m.group(0))
        ]
        findings += [
            Finding(path, n, "EMAIL", _mask(m.group(0)))
            for m in EMAIL.finditer(line)
            if m.group(0) not in ALLOWED_EMAILS
        ]
        low = line.lower()
        findings += [Finding(path, n, "HOSTNAME", _mask(h)) for h in hosts if h in low]
    return findings


def _strings(obj: object) -> Iterator[str]:
    """Todas as strings de um valor JSON, ja DECODIFICADAS (sem escapes \\n, \\", \\uXXXX)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def scan_jsonl(text: str, path: str, hostnames: Iterable[str] = ()) -> list[Finding]:
    """Varre cada string do JSONL decodificada; o numero da linha e o da linha do arquivo."""
    findings: list[Finding] = []
    for n, line in enumerate(text.splitlines(), 1):
        try:
            strings = list(_strings(json.loads(line)))
        except json.JSONDecodeError:
            strings = [line]
        for chunk in strings:
            findings += [
                Finding(path, n, f.category, f.excerpt) for f in scan_text(chunk, path, hostnames)
            ]
    return findings


def scan_path(root: Path, hostnames: Iterable[str] = ()) -> list[Finding]:
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    out: list[Finding] = []
    for f in files:
        if f.suffix not in {".jsonl", ".json", ".md", ".txt"}:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        out += (
            scan_jsonl(text, str(f), hostnames)
            if f.suffix == ".jsonl"
            else scan_text(text, str(f), hostnames)
        )
    return out


# --------------------------------------------------------------------------- redacao (export)
def redact(text: str, extra_user_emails: Iterable[str] = ()) -> str:
    """Redacao deterministica e segura para JSON (so troca por texto ASCII simples)."""
    keep = BENIGN.findall(text)  # ids de sessao ja publicos: protegidos da deteccao de blobs
    for i, value in enumerate(keep):
        text = text.replace(value, f"__KEEP{i}__")
    text = re.sub(r"/home/user/tml-namastex-fde-challenge", "<repo>", text)
    text = re.sub(r"/tmp/claude-0/[A-Za-z0-9_./-]*", "<tmp>", text)
    text = HOME_PATH.sub("<home>", text)
    text = re.sub(r"/root(?=[/\"\s]|$)", "<root-home>", text)
    text = JWT.sub("<token-redigido>", text)
    text = KNOWN_TOKEN.sub("<token-redigido>", text)
    text = RFC1918.sub("<ip-privado>", text)
    for email in extra_user_emails:
        text = text.replace(email, "<user-email>")
    text = EMAIL.sub(
        lambda m: m.group(0) if m.group(0) in ALLOWED_EMAILS else "<email-redigido>", text
    )
    text = BASE64_CANDIDATE.sub(
        lambda m: "<blob-redigido>" if _looks_like_base64(m.group(0)) else m.group(0), text
    )
    for i, value in enumerate(keep):
        text = text.replace(f"__KEEP{i}__", value)
    return text


def _map_strings(obj: object, fn: Callable[[str], str]) -> object:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {k: _map_strings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_map_strings(v, fn) for v in obj]
    return obj


def export_transcript(
    src: Path, out: Path, hostnames: Iterable[str] = (), user_emails: Iterable[str] = ()
) -> list[Finding]:
    """Redige as STRINGS decodificadas de cada linha e reserializa (nunca mexe no JSON escapado)."""
    redacted = [
        json.dumps(
            _map_strings(json.loads(line), lambda t: redact(t, user_emails)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for line in src.read_text(encoding="utf-8").splitlines()
    ]
    text = "\n".join(redacted) + "\n"
    findings = scan_jsonl(text, str(out), hostnames)
    if not findings:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return findings


def _load_hosts(path: str | None) -> list[str]:
    path = path or os.environ.get("AI_LOGS_HOSTNAMES_FILE")
    return Path(path).read_text(encoding="utf-8").splitlines() if path else []


def report(findings: list[Finding], hosts_checked: bool) -> None:
    by_cat: dict[str, int] = {}
    for f in findings:
        by_cat[f.category] = by_cat.get(f.category, 0) + 1
    for f in findings[:50]:
        sys.stdout.write(f"{f.path}:{f.line}: {f.category}: {f.excerpt}\n")
    if len(findings) > 50:
        sys.stdout.write(f"... e mais {len(findings) - 50} ocorrencias\n")
    sys.stdout.write(f"RESUMO: {by_cat or 'nenhuma ocorrencia'}\n")
    if not hosts_checked:
        sys.stdout.write(
            "AVISO: categoria HOSTNAME NAO verificada (lista privada do owner nao informada).\n"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("path", nargs="?", type=Path, help="arquivo ou diretorio a varrer")
    ap.add_argument(
        "--hostnames-file", help="lista PRIVADA de hostnames (um por linha); nunca commitar"
    )
    ap.add_argument("--export", type=Path, help="transcript JSONL de origem a exportar com redacao")
    ap.add_argument("--out", type=Path, help="destino do --export")
    ap.add_argument(
        "--user-email", action="append", default=[], help="e-mail do usuario a redigir (repetivel)"
    )
    args = ap.parse_args(argv)
    hosts = _load_hosts(args.hostnames_file)
    if args.export:
        if not args.out:
            ap.error("--export exige --out")
        findings = export_transcript(args.export, args.out, hosts, args.user_email)
        report(findings, bool(hosts))
        sys.stdout.write(
            "NAO GRAVADO: ainda ha ocorrencias.\n" if findings else f"GRAVADO: {args.out}\n"
        )
        return 1 if findings else 0
    if not args.path:
        ap.error("informe o caminho a varrer")
    findings = scan_path(args.path, hosts)
    report(findings, bool(hosts))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
