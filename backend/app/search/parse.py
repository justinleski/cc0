"""Quote handling for hybrid search.

A balanced quoted span is a required phrase, so reciprocal rank fusion cannot
substitute a visual neighbor for an explicit name or keyword. An unpaired quote
is dropped and the search runs on what remains; the caller returns that text
as applied_query so a client can show what was actually searched.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedQuery:
    applied_query: str
    phrases: tuple[str, ...]
    unquoted: str

    @property
    def quotes_only(self) -> bool:
        return bool(self.phrases) and not self.unquoted


def parse_search_text(raw: str) -> ParsedQuery:
    phrases: list[str] = []
    unquoted_chunks: list[str] = []
    applied_chunks: list[str] = []
    index = 0
    while index < len(raw):
        if raw[index] != '"':
            start = index
            while index < len(raw) and raw[index] != '"':
                index += 1
            chunk = raw[start:index]
            unquoted_chunks.append(chunk)
            applied_chunks.append(chunk)
            continue
        end = raw.find('"', index + 1)
        if end == -1:
            index += 1
            continue
        phrase = " ".join(raw[index + 1 : end].split())
        if phrase:
            phrases.append(phrase)
            applied_chunks.append(f'"{phrase}"')
        index = end + 1
    unquoted = " ".join("".join(unquoted_chunks).split())
    applied = " ".join(part.strip() for part in applied_chunks if part.strip())
    return ParsedQuery(applied_query=applied, phrases=tuple(phrases), unquoted=unquoted)
