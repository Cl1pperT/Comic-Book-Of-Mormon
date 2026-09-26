"""Actionable API diagnostics without exposing credentials or request URLs."""
import re


class ProviderError(RuntimeError):
    pass


def api_error(exc, api_key=""):
    code = getattr(exc, "code", None)
    message = str(getattr(exc, "message", "") or "Request rejected by Gemini")
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(r"AIza[\w-]+", "[REDACTED]", message)
    message = re.sub(r"https?://\S+", "[URL omitted]", message)
    message = re.sub(r"(?i)((?:api[_-]?key|authorization|token)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", message)
    hints = {
        400: "Check the model, request schema, and API-key validity.",
        401: "Check GEMINI_API_KEY in .env.",
        402: "Check API billing and available credits in Google AI Studio.",
        403: "Check API-key restrictions and project access in Google AI Studio.",
        404: "Check that the configured model exists and is available to your API project.",
        429: "Check API quota, billing, and rate limits before retrying.",
    }
    return f"Gemini API {code or 'error'}: {message[:2000]}\n{hints.get(code, 'Check provider availability before retrying.')}"
