"""Match B3 corporate names to CVM filing entities.

B3 and CVM publish the same company under different legal-name spellings
(``WEG S.A.`` vs ``WEG SA``, ``BANCO BRADESCO`` vs ``BCO BRADESCO``). Neither
source exposes a shared identifier for a ticker, so names are normalized and
matched conservatively: an ambiguous match is rejected rather than guessed,
because attributing another company's statements to a ticker would silently
corrupt every derived score.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Legal forms and connectives carry no identifying information, but business
# qualifiers remain useful for distinguishing a listed operating company from
# a historical holding with the same short name.
_LEGAL_NOISE_TOKENS = frozenset(
    {
        "SA",
        "S",
        "A",
        "LTDA",
        "ME",
        "EPP",
        "CIA",
        "COMPANHIA",
        "DO",
        "DA",
        "DE",
        "DOS",
        "DAS",
        "E",
        "EM",
    }
)

_NOISE_TOKENS = _LEGAL_NOISE_TOKENS | frozenset(
    {
        "PARTICIPACOES",
        "PARTICIPACAO",
        "PART",
        "HOLDING",
        "HOLDINGS",
        "GRUPO",
        "BRASILEIRA",
        "BRASILEIRO",
        "NACIONAL",
        "INDUSTRIA",
        "INDUSTRIAS",
        "COMERCIO",
        "EMPREENDIMENTOS",
        "INVESTIMENTOS",
        "NOVA",
        "NOVO",
    }
)

# Abbreviations CVM uses consistently in filing names.
_SYNONYMS = {
    "BCO": "BANCO",
    "TELEC": "TELECOMUNICACOES",
    "TELECOM": "TELECOMUNICACOES",
    "DISTRIB": "DISTRIBUIDORA",
    "ELETR": "ELETRICA",
    "CENT": "CENTRAIS",
    "CElG": "CELG",
    "EMPR": "EMPRESA",
    "SERV": "SERVICOS",
    "TRANSM": "TRANSMISSAO",
    "ENERG": "ENERGIA",
    "SID": "SIDERURGICA",
    "CONSTR": "CONSTRUTORA",
}

_MINIMUM_OVERLAP = 2


@dataclass(frozen=True)
class CompanyMatch:
    cnpj: str
    company_name: str
    confidence: str


def normalize_company_name(name: str) -> str:
    """Reduce a corporate name to its identifying tokens."""
    return _normalize_company_name(name, _NOISE_TOKENS)


def _normalize_legal_name(name: str) -> str:
    return _normalize_company_name(name, _LEGAL_NOISE_TOKENS)


def _normalize_company_name(name: str, noise_tokens: frozenset[str]) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").upper()
    folded = folded.replace("S/A", " SA ").replace("S.A.", " SA ")
    folded = re.sub(r"[^A-Z0-9]+", " ", folded)
    tokens = [_SYNONYMS.get(token, token) for token in folded.split() if token]
    meaningful = [token for token in tokens if token not in noise_tokens]
    # Fall back to the raw tokens when a name is made entirely of noise words.
    return " ".join(meaningful or tokens)


def match_company(
    corporate_name: str,
    candidates: dict[str, str],
) -> CompanyMatch | None:
    """Resolve a B3 corporate name against ``{cnpj: filing_name}`` candidates.

    Returns ``None`` when no candidate is convincing or when several candidates
    are equally plausible.
    """
    target = normalize_company_name(corporate_name)
    if not target:
        return None

    legal_target = _normalize_legal_name(corporate_name)
    legal_names = {cnpj: _normalize_legal_name(name) for cnpj, name in candidates.items()}
    legal_exact = [cnpj for cnpj, name in legal_names.items() if name == legal_target]
    if len(legal_exact) == 1:
        cnpj = legal_exact[0]
        return CompanyMatch(cnpj, candidates[cnpj], "high")
    if len(legal_exact) > 1:
        return None

    normalized = {cnpj: normalize_company_name(name) for cnpj, name in candidates.items()}

    exact = [cnpj for cnpj, name in normalized.items() if name == target]
    if len(exact) == 1:
        return CompanyMatch(exact[0], candidates[exact[0]], "high")
    if len(exact) > 1:
        return None

    target_tokens = set(target.split())
    scored: list[tuple[int, str]] = []
    for cnpj, name in normalized.items():
        tokens = set(name.split())
        if not tokens:
            continue
        overlap = len(target_tokens & tokens)
        # Require the shorter name to be fully contained in the longer one so
        # that unrelated companies sharing a common word do not match.
        contained = tokens <= target_tokens or target_tokens <= tokens
        if overlap >= _MINIMUM_OVERLAP and contained:
            scored.append((overlap, cnpj))

    if not scored:
        return None
    best = max(score for score, _ in scored)
    winners = [cnpj for score, cnpj in scored if score == best]
    if len(winners) != 1:
        return None
    return CompanyMatch(winners[0], candidates[winners[0]], "medium")
