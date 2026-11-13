from types import SimpleNamespace

import aiohttp


class _Completions:
    def __init__(self, parent):
        self._p = parent

    async def create(self, *, model, messages, temperature=None, max_tokens=None, **kwargs):
        payload = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        payload.update(kwargs)

        session = await self._p._get_session()
        async with session.post(
            f"{self._p.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self._p.api_key}"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"AI API error {resp.status}: {body[:200]}")
            data = await resp.json()

        content = data["choices"][0]["message"].get("content") or ""
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class SimpleClient:
    """Tiny stand-in for AsyncOpenAI: client.chat.completions.create(...)"""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._session = None
        self.chat = SimpleNamespace(completions=_Completions(self))

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session
