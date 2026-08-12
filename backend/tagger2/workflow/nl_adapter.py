"""Adapter to use Tagger2 VisionProvider as NlClient."""

import asyncio
import json
from ..providers.client import VisionProvider
from .stages.nl import NlRequest


class ProviderNlAdapter:
    """Wraps an async VisionProvider to provide sync NlClient interface."""

    def __init__(self, provider: VisionProvider, *, model: str | None = None):
        self._provider = provider
        self._model = model.strip() if isinstance(model, str) and model.strip() else None

    def complete(self, request: NlRequest) -> bytes:
        """Synchronously call the async provider and return raw JSON bytes.
        
        The NL stage expects a raw bytes response that it will parse itself.
        This adapter converts NlRequest (which has system_prompt, payload, image_path)
        into the provider's generate call and returns the result as JSON bytes.
        """
        
        # Keep the frozen prompt in the provider's system channel and send the
        # sample projection as data in the user channel.  The model override is
        # part of the immutable NL contract; an empty value deliberately keeps
        # the provider profile's primary/fallback model selection.
        user_prompt = json.dumps(request.payload, ensure_ascii=False, sort_keys=True)
        
        # Run the async provider in a new event loop
        # (The NL stage runs in a worker thread, so there's no active event loop)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            
            # Use the generate method with image path if available
            if request.image_path and request.image_path.is_file():
                result = loop.run_until_complete(
                    self._provider.generate(
                        image=str(request.image_path),
                        prompt=user_prompt,
                        model=self._model,
                        system_prompt=request.system_prompt,
                    )
                )
            else:
                result = loop.run_until_complete(
                    self._provider.generate(
                        image=None,
                        prompt=user_prompt,
                        model=self._model,
                        system_prompt=request.system_prompt,
                    )
                )
            
            # Return as JSON bytes in OpenAI-compatible format
            # The NL stage will parse this and extract choices[0].message.content
            response_data = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": result
                        }
                    }
                ]
            }
            return json.dumps(response_data).encode("utf-8")
        finally:
            loop.close()
