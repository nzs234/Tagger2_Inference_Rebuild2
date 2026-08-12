"""Adapter to use Tagger2 VisionProvider as NlClient."""

import asyncio
import json
from ..providers.client import VisionProvider
from .stages.nl import NlRequest


class ProviderNlAdapter:
    """Wraps an async VisionProvider to provide sync NlClient interface."""

    def __init__(self, provider: VisionProvider):
        self._provider = provider

    def complete(self, request: NlRequest) -> bytes:
        """Synchronously call the async provider and return raw JSON bytes.
        
        The NL stage expects a raw bytes response that it will parse itself.
        This adapter converts NlRequest (which has system_prompt, payload, image_path)
        into the provider's generate call and returns the result as JSON bytes.
        """
        
        # Extract the user prompt from the payload
        # The NL stage builds a JSON payload with the annotation data
        user_prompt = request.system_prompt
        if request.payload:
            user_prompt += f"\n\nAnnotation data: {json.dumps(request.payload)}"
        
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
                    )
                )
            else:
                # For text-only, we still need to call generate but with no image
                # This may not work for all providers - may need enhancement
                result = loop.run_until_complete(
                    self._provider.generate(
                        image="",  # Empty image
                        prompt=user_prompt,
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
