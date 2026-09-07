"""Bounded, redacted diagnostics. Timing records never include CLI output."""
import re


def safe_diagnostic(value, limit=1000):
    text=str(value)
    text=re.sub(r'(?i)("(?:access[_-]?token|refresh[_-]?token|token|password|secret|authorization|cookie)"\s*:\s*")[^"]*(")',r'\1[redacted]\2',text)
    text=re.sub(r'(?i)(bearer\s+)[^\s,;]+',r'\1[redacted]',text)
    text=re.sub(r'(?i)((?:access[_-]?token|refresh[_-]?token|token|password|secret|authorization|cookie)\s*[=:]\s*)[^\s,;]+',r'\1[redacted]',text)
    text=''.join(c for c in text if c in '\n\t' or ord(c)>=32)
    return text[:limit]
